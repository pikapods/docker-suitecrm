import json
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.runtime

IMAGE = os.environ["IMAGE"]
READY_DEADLINE_S = 300
HEALTHY_DEADLINE_S = 90

LOGIN_URL_PATH = "/index.php?module=Users&action=Login"


def _sh(*args, check=True, capture=True):
    return subprocess.run(
        list(args),
        capture_output=capture, text=True, check=check,
    )


def _exec(container, *args, check=False):
    return subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, check=check,
    )


def _wait_mysql_ready(container, deadline_s=60):
    end = time.time() + deadline_s
    while time.time() < end:
        r = _exec(container, "mysqladmin", "ping", "-h", "127.0.0.1",
                  "-uroot", "-ptest", "--silent")
        if r.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"mysql container {container} not ready within {deadline_s}s")


def _http_get(url, timeout=10):
    req = urllib.request.Request(url)
    return urllib.request.urlopen(req, timeout=timeout)


def _wait_http_200(url, deadline_s):
    end = time.time() + deadline_s
    last_err = None
    while time.time() < end:
        try:
            with _http_get(url, timeout=10) as r:
                if r.status == 200:
                    return
                last_err = f"status={r.status}"
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = repr(e)
        time.sleep(3)
    raise RuntimeError(f"{url} did not return 200 within {deadline_s}s (last={last_err})")


def _host_port(container, container_port):
    r = _sh("docker", "port", container, container_port)
    line = r.stdout.splitlines()[0]
    return int(line.rsplit(":", 1)[1])


@pytest.fixture(scope="session")
def stack():
    suffix = secrets.token_hex(4)
    net = f"sc-net-{suffix}"
    db = f"db-{suffix}"
    sc = f"sc-{suffix}"

    _sh("docker", "network", "create", net)
    try:
        _sh(
            "docker", "run", "-d", "--name", db, "--network", net,
            "-e", "MYSQL_ROOT_PASSWORD=test",
            "-e", "MYSQL_DATABASE=suitecrm",
            "-e", "MYSQL_USER=suitecrm",
            "-e", "MYSQL_PASSWORD=suitepass",
            "mysql:8",
            "--character-set-server=utf8mb4",
            "--collation-server=utf8mb4_unicode_ci",
        )
        _wait_mysql_ready(db)

        _sh(
            "docker", "run", "-d", "--name", sc, "--network", net,
            "-e", "APP_URL=http://localhost:8080",
            "-e", f"DB_HOST={db}",
            "-e", "DB_PORT=3306",
            "-e", "DB_NAME=suitecrm",
            "-e", "DB_USER=suitecrm",
            "-e", "DB_PASS=suitepass",
            "-e", "ADMIN_USER=admin",
            "-e", "ADMIN_PASS=changeme",
            "-e", "SITE_NAME=SmokeTest",
            "-p", ":8080",
            IMAGE,
        )
        port = _host_port(sc, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}{LOGIN_URL_PATH}", READY_DEADLINE_S)
        except RuntimeError:
            print(_sh("docker", "logs", sc, check=False).stdout)
            print(_sh("docker", "logs", sc, check=False).stderr)
            raise

        yield {"sc": sc, "db": db, "net": net, "port": port}
    finally:
        for name in (sc, db):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


def test_login_responds_200(stack):
    with _http_get(f"http://127.0.0.1:{stack['port']}{LOGIN_URL_PATH}") as r:
        assert r.status == 200
        body = r.read().decode("utf-8", errors="replace")
    # Cheap content sanity — login page renders password + username fields.
    lower = body.lower()
    assert 'type="password"' in lower or 'name="user_password"' in lower, \
        f"login page missing password field (body head: {body[:400]!r})"


def test_admin_user_created(stack):
    """Prove the silent installer honored ADMIN_USER/ADMIN_PASS by seeding
    the admin row. We check the DB directly rather than round-tripping the
    login form because SuiteCRM 7.x supports several password-hash modes
    (plain/MD5/PHPass) and the correct one depends on the install profile —
    a brittle HTTP test would generate false negatives across versions.

    Verifies user_name matches the env, is_admin=1, status=Active, and the
    password hash field is non-empty (i.e. it was actually set, not left
    blank by a half-failed install).
    """
    r = _exec(
        stack["db"], "mysql", "-uroot", "-ptest", "-N", "-B",
        "-e",
        "SELECT user_name, is_admin, status, "
        "  CASE WHEN user_hash IS NULL OR user_hash='' THEN 'EMPTY' ELSE 'SET' END "
        "FROM suitecrm.users WHERE user_name='admin'",
    )
    assert r.returncode == 0, f"users query failed: stderr={r.stderr!r}"
    cols = r.stdout.split()
    assert cols[:4] == ["admin", "1", "Active", "SET"], (
        f"admin row not seeded correctly: got {cols!r} "
        "(expected ['admin','1','Active','SET'])"
    )


def test_db_schema_initialized(stack):
    """Prove the silent installer populated the schema, not just wrote a
    config file. The 4 tables checked are SuiteCRM core; names are stable
    across 7.x minor releases."""
    r = _exec(
        stack["db"],
        "mysql", "-uroot", "-ptest", "-N", "-B",
        "-e", "SHOW TABLES IN suitecrm",
    )
    assert r.returncode == 0, f"SHOW TABLES failed: stderr={r.stderr!r}"
    tables = set(r.stdout.split())
    core = {"users", "accounts", "contacts", "config"}
    missing = core - tables
    assert not missing, (
        f"silent install did not create core tables: missing={sorted(missing)} "
        f"(got {len(tables)} tables total)"
    )


def _http_status(url):
    try:
        with _http_get(url) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("path", [
    "/install.php",
    "/config.php",
    "/config_override.php",
    "/custom/blowfish/foo",
    "/upload/import/foo",
    "/upload/tmp/foo",
])
def test_blocked_paths_return_403(stack, path):
    code = _http_status(f"http://127.0.0.1:{stack['port']}{path}")
    assert code == 403, f"{path} returned {code}, expected 403"


@pytest.mark.parametrize("subdir", ["upload", "cache", "custom", "data"])
def test_uploaded_php_not_executed(stack, subdir):
    """Drop a .php file under a writable tree and verify nginx denies it
    rather than handing it to php-fpm. Without the deny rule, a user with
    write access to /upload (e.g. via the SuiteCRM file-attachment flow)
    gets RCE. modules/ is core app code (not user-writable) so it's
    omitted — the four writable trees prove the deny pattern works."""
    sc = stack["sc"]
    # /data/custom -> /var/www/html/custom (via symlink chain), and
    # /var/www/html/data -> /data/runtime-data; either way, writing through
    # the in-app path lands the file where nginx will see it under doc root.
    container_path = f"/var/www/html/{subdir}/pwn-test.php"
    placed = _exec(sc, "sh", "-c",
                   f"mkdir -p $(dirname {container_path}) && "
                   f"echo '<?php echo \"PWNED:\" . php_sapi_name();' > {container_path}")
    assert placed.returncode == 0, f"could not place test file: {placed.stderr!r}"
    try:
        code = _http_status(
            f"http://127.0.0.1:{stack['port']}/{subdir}/pwn-test.php"
        )
        assert code == 403, (
            f"/{subdir}/pwn-test.php returned {code}, expected 403 "
            "(would mean nginx executed the .php through php-fpm)"
        )
    finally:
        _exec(sc, "rm", "-f", container_path)


def test_installer_locked_in_config(stack):
    r = _exec(stack["sc"], "grep", "-E", "installer_locked.*=>.*true", "/data/config.php")
    assert r.returncode == 0, (
        f"installer_locked => true not present in /data/config.php "
        f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
    )


def test_logs_clean(stack):
    logs = _sh("docker", "logs", stack["sc"], check=False)
    combined = logs.stdout + logs.stderr
    # SuiteCRM legitimately emits "PHP Notice" and deprecation strings; we
    # only flag fatal classes.
    bad = re.findall(r"PHP Fatal|PHP Parse error|Stack trace", combined)
    assert not bad, f"bad patterns in container logs: {bad[:5]}"


def test_cron_longrun_alive(stack):
    # Read /proc cmdlines directly — busybox `ps` truncates shebang-launched
    # scripts. The run-script lives at /etc/s6-overlay/s6-rc.d/suitecrm-cron/run.
    r = _exec(
        stack["sc"], "sh", "-c",
        "cat /proc/[0-9]*/cmdline 2>/dev/null | tr '\\0' '\\n' "
        "| grep -qF suitecrm-cron/run",
    )
    assert r.returncode == 0, (
        "suitecrm-cron longrun process not present in /proc cmdlines "
        f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
    )


@pytest.mark.parametrize("path", [
    "/data/config.php",
    "/data/config_override.php",
    "/data/custom",
    "/data/upload",
    "/data/cache",
    "/data/runtime-data",
])
def test_bootstrap_populated_data(stack, path):
    flag = "-f" if path.endswith(".php") else "-d"
    r = _exec(stack["sc"], "test", flag, path)
    assert r.returncode == 0, f"bootstrap did not produce {path}"


def test_db_reconcile_on_restart(stack):
    """Mutate site_url in config.php on disk, restart, verify the env-supplied
    value is rewritten — and installer_locked is NOT cleared.
    """
    sc = stack["sc"]
    # Confirm pre-state.
    r = _exec(sc, "grep", "site_url", "/data/config.php")
    assert r.returncode == 0
    # Munge site_url so the reconciliation step has something to rewrite.
    # Use single-quoted sh -c body so neither $sugar_config nor $_ get
    # eaten by the shell before PHP sees them. Inside single quotes, escape
    # embedded single quotes via the '"'"' trick.
    munge = (
        'php -r \'include "/data/config.php"; '
        '$sugar_config["site_url"]="http://stale.invalid"; '
        'file_put_contents("/data/config.php", '
        '"<?php" . PHP_EOL . "\\$sugar_config = " . var_export($sugar_config, true) . ";" . PHP_EOL);\''
    )
    r = _exec(sc, "sh", "-c", munge)
    assert r.returncode == 0, f"munge failed: stderr={r.stderr!r}"
    r = _exec(sc, "grep", "http://stale.invalid", "/data/config.php")
    assert r.returncode == 0, "munge did not land"

    _sh("docker", "restart", sc)
    port = _host_port(sc, "8080")
    _wait_http_200(f"http://127.0.0.1:{port}{LOGIN_URL_PATH}", READY_DEADLINE_S)

    # After restart: site_url should be back to APP_URL, installer_locked still true.
    r = _exec(sc, "grep", "site_url", "/data/config.php")
    assert "http://localhost:8080" in r.stdout, \
        f"site_url not reconciled: {r.stdout!r}"
    assert "stale.invalid" not in r.stdout

    r = _exec(sc, "grep", "-E", "installer_locked.*=>.*true", "/data/config.php")
    assert r.returncode == 0, "installer_locked was cleared by restart"

    # Verify the boot path took the skip-install branch.
    logs = _sh("docker", "logs", sc, check=False).stderr + _sh("docker", "logs", sc, check=False).stdout
    assert "skipping silent install" in logs, \
        "restart boot did not log the skip-install message"


def test_healthcheck_reports_healthy(stack):
    end = time.time() + HEALTHY_DEADLINE_S
    last = None
    while time.time() < end:
        r = _sh("docker", "inspect", "--format", "{{json .State.Health}}", stack["sc"])
        health = json.loads(r.stdout)
        if not health:
            pytest.skip("image has no HEALTHCHECK or daemon does not surface health")
        last = health.get("Status")
        if last == "healthy":
            return
        if last == "unhealthy":
            pytest.fail(f"container went unhealthy: {health.get('Log', [])[-1:]!r}")
        time.sleep(3)
    pytest.fail(f"healthcheck still {last!r} after {HEALTHY_DEADLINE_S}s")


def test_bootstrap_fails_on_missing_env():
    """The bootstrap script (`rootfs/etc/entrypoint.d/20-suitecrm-bootstrap.sh`)
    uses POSIX `: "${VAR:?msg}"` to enforce required env. If that validation
    is weakened, the image would fall through to a confusing later failure
    (broken install, default credentials exposed, etc). Boot the image with
    none of the required env and assert it fails loudly.
    """
    name = f"sc-noenv-{secrets.token_hex(4)}"
    _sh("docker", "run", "-d", "--name", name, IMAGE)
    try:
        # Bootstrap should bail within a few seconds (no MySQL wait reached).
        deadline = time.time() + 30
        state = None
        while time.time() < deadline:
            r = _sh("docker", "inspect", "--format",
                    "{{.State.Status}} {{.State.ExitCode}}", name)
            state = r.stdout.strip()
            if state.startswith("exited"):
                break
            time.sleep(1)

        logs = _sh("docker", "logs", name, check=False)
        combined = logs.stdout + logs.stderr

        # Either the container has exited non-zero, or it's still running but
        # the bootstrap script has logged a "*is required*" error. Both are
        # acceptable evidence that validation fired (s6 supervisor behavior
        # on oneshot failure differs by version).
        validation_fired = re.search(r"\bis required\b|APP_URL|DB_HOST", combined)
        exited_nonzero = state and state.startswith("exited") and not state.endswith(" 0")

        assert validation_fired or exited_nonzero, (
            f"bootstrap did not fail loudly on missing env "
            f"(state={state!r}, logs head: {combined[:600]!r})"
        )
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def stack_persisted():
    """Bring up MySQL + SuiteCRM with a *named* volume bound at /data, then
    yield handles for a second-boot test. The session `stack` fixture uses
    an anonymous volume tied to the original container — it can't be reused
    for a rm + recreate scenario."""
    suffix = secrets.token_hex(4)
    net = f"sc-net-p-{suffix}"
    db = f"db-p-{suffix}"
    vol = f"sc-data-{suffix}"
    sc_initial = f"sc-p1-{suffix}"
    sc_recreated = f"sc-p2-{suffix}"
    created_containers = []

    _sh("docker", "network", "create", net)
    _sh("docker", "volume", "create", vol)
    try:
        _sh(
            "docker", "run", "-d", "--name", db, "--network", net,
            "-e", "MYSQL_ROOT_PASSWORD=test",
            "-e", "MYSQL_DATABASE=suitecrm",
            "-e", "MYSQL_USER=suitecrm",
            "-e", "MYSQL_PASSWORD=suitepass",
            "mysql:8",
            "--character-set-server=utf8mb4",
            "--collation-server=utf8mb4_unicode_ci",
        )
        created_containers.append(db)
        _wait_mysql_ready(db)

        def _run_app(name):
            _sh(
                "docker", "run", "-d", "--name", name, "--network", net,
                "--mount", f"type=volume,source={vol},target=/data",
                "-e", "APP_URL=http://localhost:8080",
                "-e", f"DB_HOST={db}",
                "-e", "DB_PORT=3306",
                "-e", "DB_NAME=suitecrm",
                "-e", "DB_USER=suitecrm",
                "-e", "DB_PASS=suitepass",
                "-e", "ADMIN_USER=admin",
                "-e", "ADMIN_PASS=changeme",
                "-e", "SITE_NAME=SmokeTestPersist",
                "-p", ":8080",
                IMAGE,
            )
            created_containers.append(name)
            port = _host_port(name, "8080")
            try:
                _wait_http_200(f"http://127.0.0.1:{port}{LOGIN_URL_PATH}", READY_DEADLINE_S)
            except RuntimeError:
                print(_sh("docker", "logs", name, check=False).stdout)
                print(_sh("docker", "logs", name, check=False).stderr)
                raise
            return port

        port_initial = _run_app(sc_initial)
        # Tear down only the app container; volume + DB persist.
        subprocess.run(["docker", "rm", "-f", sc_initial], capture_output=True)
        created_containers.remove(sc_initial)

        port_recreated = _run_app(sc_recreated)

        yield {
            "sc": sc_recreated,
            "db": db,
            "net": net,
            "vol": vol,
            "port": port_recreated,
        }
    finally:
        for name in created_containers:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", vol], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


def test_data_persists_across_container_recreate(stack_persisted):
    """Drop the app container, start a fresh one against the same /data
    volume + same DB, and verify the second boot:
      (a) serves login again (HTTP 200),
      (b) preserves installer_locked => true,
      (c) takes the skip-install branch (didn't re-run silent install
          against an already-populated DB).
    """
    sc = stack_persisted["sc"]
    port = stack_persisted["port"]

    with _http_get(f"http://127.0.0.1:{port}{LOGIN_URL_PATH}") as r:
        assert r.status == 200, f"second boot login returned {r.status}"

    r = _exec(sc, "grep", "-E", "installer_locked.*=>.*true", "/data/config.php")
    assert r.returncode == 0, (
        f"installer_locked not preserved across recreate "
        f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
    )

    logs = _sh("docker", "logs", sc, check=False)
    combined = logs.stdout + logs.stderr
    assert "skipping silent install" in combined, (
        "second-boot did not log the skip-install branch — installer may "
        "have re-run against an already-populated DB"
    )


@pytest.fixture
def stack_bindmount(tmp_path):
    """Boot MySQL + SuiteCRM with a HOST BIND MOUNT at /data.

    Bind mounts are not seeded by the container runtime (unlike named or
    anonymous volumes, which the other fixtures use), so the bootstrap must
    self-seed /data/runtime-data and /data/custom from the baked skeleton at
    /usr/local/share/suitecrm-skel. This fixture is the only path in the
    suite that exercises that branch — bind mounts are the typical pod-host
    deployment shape, so coverage matters.

    Cleanup: files inside the bind dir end up owned by the container UID
    (www-data, 82), which the host test user can't rm. We wipe via a
    throwaway container before letting tmp_path's cleanup run.
    """
    suffix = secrets.token_hex(4)
    net = f"sc-net-b-{suffix}"
    db = f"db-b-{suffix}"
    sc = f"sc-b-{suffix}"
    host_data = tmp_path / "data"
    host_data.mkdir()
    os.chmod(host_data, 0o777)  # container UID 82 must be able to write
    created = []

    _sh("docker", "network", "create", net)
    try:
        _sh(
            "docker", "run", "-d", "--name", db, "--network", net,
            "-e", "MYSQL_ROOT_PASSWORD=test",
            "-e", "MYSQL_DATABASE=suitecrm",
            "-e", "MYSQL_USER=suitecrm",
            "-e", "MYSQL_PASSWORD=suitepass",
            "mysql:8",
            "--character-set-server=utf8mb4",
            "--collation-server=utf8mb4_unicode_ci",
        )
        created.append(db)
        _wait_mysql_ready(db)

        _sh(
            "docker", "run", "-d", "--name", sc, "--network", net,
            "-v", f"{host_data}:/data:Z",
            "-e", "APP_URL=http://localhost:8080",
            "-e", f"DB_HOST={db}",
            "-e", "DB_PORT=3306",
            "-e", "DB_NAME=suitecrm",
            "-e", "DB_USER=suitecrm",
            "-e", "DB_PASS=suitepass",
            "-e", "ADMIN_USER=admin",
            "-e", "ADMIN_PASS=changeme",
            "-e", "SITE_NAME=SmokeTestBind",
            "-p", ":8080",
            IMAGE,
        )
        created.append(sc)
        port = _host_port(sc, "8080")
        try:
            _wait_http_200(f"http://127.0.0.1:{port}{LOGIN_URL_PATH}", READY_DEADLINE_S)
        except RuntimeError:
            print(_sh("docker", "logs", sc, check=False).stdout)
            print(_sh("docker", "logs", sc, check=False).stderr)
            raise

        yield {"sc": sc, "db": db, "net": net, "port": port,
               "host_data": str(host_data)}
    finally:
        for name in created:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)
        # Wipe container-owned files so tmp_path cleanup doesn't EACCES.
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{host_data}:/clean:Z",
             "alpine", "sh", "-c",
             "rm -rf /clean/* /clean/.[!.]* 2>/dev/null || true"],
            capture_output=True,
        )


def test_bindmount_first_boot_seeds_and_idempotent_on_restart(stack_bindmount):
    """End-to-end coverage of the bind-mount path that broke at pikapod:

      (a) First boot: bootstrap MUST log seeding /data/runtime-data and
          seeding /data/custom (the skeleton-copy step), HTTP 200, marker
          files SuiteCRM needs at runtime present in the bind-mounted dir.
      (b) Restart: seed steps MUST short-circuit (marker files present →
          guards skip the cp), install MUST skip (installer_locked → true
          persists), HTTP still 200.

    Combined into one test because each bind-mount fixture instance costs
    ~3 min (build + mysql + silent install); splitting would double it.
    """
    sc = stack_bindmount["sc"]
    port = stack_bindmount["port"]

    # --- (a) first boot ---
    with _http_get(f"http://127.0.0.1:{port}{LOGIN_URL_PATH}") as r:
        assert r.status == 200, f"first-boot login returned {r.status}"

    logs1 = _sh("docker", "logs", sc, check=False)
    combined1 = logs1.stdout + logs1.stderr
    assert "seeding /data/runtime-data" in combined1, (
        "first-boot bootstrap did not seed runtime-data — bind mount would "
        "be missing data/SugarBean.php and install.php would 500. "
        f"logs head: {combined1[:600]!r}"
    )
    assert "seeding /data/custom" in combined1, (
        "first-boot bootstrap did not seed custom — Extension/ would be "
        f"missing from /data/custom. logs head: {combined1[:600]!r}"
    )

    # Files the runtime actually opens — checked via container so we see them
    # through the same symlink chain SuiteCRM uses.
    for path, flag in [
        ("/data/runtime-data/SugarBean.php", "-f"),
        ("/data/runtime-data/Relationships", "-d"),
        ("/data/custom/Extension",           "-d"),
    ]:
        r = _exec(sc, "test", flag, path)
        assert r.returncode == 0, (
            f"seed did not produce {path} in the bind-mounted /data "
            "(install.php would fail on next boot from a fresh container)"
        )

    # --- (b) restart against the same bind mount ---
    _sh("docker", "restart", sc)
    port = _host_port(sc, "8080")  # may change across restart on some daemons
    _wait_http_200(f"http://127.0.0.1:{port}{LOGIN_URL_PATH}", READY_DEADLINE_S)

    with _http_get(f"http://127.0.0.1:{port}{LOGIN_URL_PATH}") as r:
        assert r.status == 200, f"post-restart login returned {r.status}"

    logs2 = _sh("docker", "logs", sc, check=False)
    combined2 = logs2.stdout + logs2.stderr

    # Seed lines from the first boot still sit in the log buffer (docker
    # restart preserves the stream); the idempotency claim is that the seed
    # step ran EXACTLY ONCE — i.e. it did not fire on the second boot.
    assert combined2.count("seeding /data/runtime-data") == 1, (
        "runtime-data seed ran more than once across restart — the marker-"
        "file guard isn't catching the second boot, which would silently "
        "overwrite user data on every restart."
    )
    assert combined2.count("seeding /data/custom") == 1, (
        "custom seed ran more than once across restart — see above."
    )
    assert "skipping silent install" in combined2, (
        "second boot did not log skip-install; installer may have re-run "
        "and dropped the populated tables."
    )
