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
            "-p", "0:8080",
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


@pytest.mark.parametrize("subdir", ["upload", "cache", "custom", "data", "modules"])
def test_uploaded_php_not_executed(stack, subdir):
    """Drop a .php file under a mutable tree and verify nginx denies it
    rather than handing it to php-fpm. Without the deny rule, a user with
    write access to /upload (e.g. via the SuiteCRM file-attachment flow)
    gets RCE."""
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
    munge = (
        "php -r \"include '/data/config.php'; "
        "$sugar_config['site_url']='http://stale.invalid'; "
        "file_put_contents('/data/config.php', "
        "'<?php' . PHP_EOL . '$sugar_config = ' . var_export($sugar_config, true) . ';' . PHP_EOL);\""
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
