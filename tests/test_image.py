import json
import os
import subprocess

import pytest

IMAGE = os.environ["IMAGE"]


def _inspect():
    out = subprocess.run(
        ["docker", "inspect", IMAGE],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)[0]


@pytest.fixture(scope="session")
def inspect():
    return _inspect()


def _run(*args, check=False):
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint=", IMAGE, *args],
        capture_output=True, text=True, check=check,
    )


class TestImageMetadata:
    def test_required_oci_labels(self, inspect):
        labels = inspect["Config"].get("Labels") or {}
        for key in (
            "org.opencontainers.image.source",
            "org.opencontainers.image.version",
            "org.opencontainers.image.licenses",
            "org.opencontainers.image.title",
        ):
            assert labels.get(key), f"missing OCI label: {key}"

    def test_runs_as_non_root(self, inspect):
        user = inspect["Config"].get("User", "")
        assert user in ("www-data", "82"), f"expected www-data/82, got {user!r}"

    def test_healthcheck_defined(self, inspect):
        # docker stores HEALTHCHECK at .Config.Healthcheck; podman surfaces it
        # at top-level .Healthcheck. Accept either so the suite is portable
        # across both daemons.
        hc = inspect["Config"].get("Healthcheck") or inspect.get("Healthcheck")
        assert hc, "no Healthcheck defined"

    def test_exposes_8080(self, inspect):
        ports = inspect["Config"].get("ExposedPorts") or {}
        assert "8080/tcp" in ports, f"8080/tcp not exposed; got {list(ports)}"

    def test_image_size_under_limit(self, inspect):
        size_mb = inspect["Size"] / (1024 * 1024)
        assert size_mb < 1500, f"image size {size_mb:.0f} MB exceeds 1500 MB guardrail"

    def test_default_env_present(self, inspect):
        env = dict(e.split("=", 1) for e in inspect["Config"].get("Env") or [])
        assert env.get("AUTORUN_ENABLED") == "false"
        assert env.get("SSL_MODE") == "off"
        assert env.get("ENABLE_SUITECRM_CRON") == "TRUE"
        assert env.get("APP_BASE_DIR") == "/var/www/html"


class TestImageFilesystem:
    @pytest.mark.parametrize("link,target", [
        ("/var/www/html/config.php",          "/data/config.php"),
        ("/var/www/html/config_override.php", "/data/config_override.php"),
        ("/var/www/html/custom",              "/data/custom"),
        ("/var/www/html/upload",              "/data/upload"),
        ("/var/www/html/cache",               "/data/cache"),
        ("/var/www/html/data",                "/data/runtime-data"),
    ])
    def test_data_symlinks(self, link, target):
        r = _run("readlink", link)
        assert r.returncode == 0, f"readlink {link} failed: {r.stderr}"
        assert r.stdout.strip() == target, f"{link} -> {r.stdout.strip()!r}, expected {target!r}"

    def test_data_dir_owned_by_www_data(self):
        r = _run("stat", "-c", "%U:%G", "/data")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "www-data:www-data"

    @pytest.mark.parametrize("binary", [
        "php", "nginx", "mysqladmin", "curl", "unzip",
    ])
    def test_runtime_binaries_present(self, binary):
        r = _run("which", binary)
        assert r.returncode == 0, f"{binary} not found on PATH"
        assert r.stdout.strip(), f"which {binary} returned empty"

    @pytest.mark.parametrize("binary", ["composer", "git"])
    def test_unneeded_binaries_absent(self, binary):
        # SuiteCRM ships vendor/; we don't install composer or git in the image.
        # Guards against accidentally pulling them back in.
        r = _run("which", binary)
        assert r.returncode != 0, f"{binary} unexpectedly present on PATH ({r.stdout.strip()!r})"

    @pytest.mark.parametrize("ext", [
        "mysqli", "pdo_mysql", "gd", "imap", "intl", "soap", "ldap",
        "bcmath", "zip", "gnupg", "exif", "Zend OPcache",
        "mbstring", "curl",
    ])
    def test_php_extensions_loaded(self, ext):
        r = _run("php", "-m")
        assert r.returncode == 0, r.stderr
        modules = {line.strip() for line in r.stdout.splitlines() if line.strip()}
        assert ext in modules, f"PHP module {ext!r} not loaded; got {sorted(modules)}"

    def test_s6_cron_run_executable(self):
        r = _run("test", "-x", "/etc/s6-overlay/s6-rc.d/suitecrm-cron/run")
        assert r.returncode == 0, "suitecrm-cron run script missing or not executable"

    def test_s6_cron_depends_on_bootstrap(self):
        r = _run(
            "test", "-f",
            "/etc/s6-overlay/s6-rc.d/suitecrm-cron/dependencies.d/20-suitecrm-bootstrap",
        )
        assert r.returncode == 0, "cron dependency marker on bootstrap missing"

    def test_s6_bootstrap_oneshot_installed(self):
        r = _run("sh", "-c", "ls /etc/s6-overlay/scripts/ | grep -E 'suitecrm-bootstrap'")
        assert r.returncode == 0, (
            "no suitecrm-bootstrap script in /etc/s6-overlay/scripts/ "
            f"(stdout={r.stdout!r}, stderr={r.stderr!r})"
        )

    @pytest.mark.parametrize("path", [
        "/var/www/html/install.php",
        "/var/www/html/cron.php",
        "/var/www/html/include/utils.php",
    ])
    def test_suitecrm_app_present(self, path):
        r = _run("test", "-f", path)
        assert r.returncode == 0, f"{path} missing"


@pytest.mark.runtime
class TestCustomUidRebuild:
    """Rebuild with --build-arg WWW_DATA_UID/GID and verify the new UID
    actually owns /data. Regression guard: set-file-permissions only touches
    a hardcoded path list, so /data needs an explicit chown in the Dockerfile
    or the rebuilt image's www-data can't write to its own volume.
    """

    UID = "1000"
    GID = "1000"

    @pytest.fixture(scope="class")
    def image(self):
        ctx = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tag = f"sc-uid{self.UID}-test"
        r = subprocess.run(
            ["docker", "build",
             "--build-arg", f"WWW_DATA_UID={self.UID}",
             "--build-arg", f"WWW_DATA_GID={self.GID}",
             "-t", tag, ctx],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            pytest.fail(
                f"docker build failed (rc={r.returncode})\n"
                f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
            )
        try:
            yield tag
        finally:
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)

    def test_www_data_user_remapped(self, image):
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint=", image,
             "id", "-u", "www-data"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == self.UID

    def test_data_dir_remapped(self, image):
        r = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint=", image,
             "stat", "-c", "%u:%g", "/data"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == f"{self.UID}:{self.GID}"
