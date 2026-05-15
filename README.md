# docker-suitecrm

[SuiteCRM 7](https://suitecrm.com) container image, built on
[`serversideup/php`](https://serversideup.net/open-source/docker-php/).

This image powers SuiteCRM on [PikaPods](https://www.pikapods.com) and is
maintained by the PikaPods team. It's published here for our users' reference
and the benefit of the wider community.

Published to both `ghcr.io/pikapods/docker-suitecrm:<suitecrm-version>` and
`pikapods/docker-suitecrm:<suitecrm-version>` (Docker Hub).

Source: https://github.com/pikapods/docker-suitecrm

## Why this image

A small, maintainable SuiteCRM 7 image: a short entrypoint, idempotent boot
that runs SuiteCRM's silent installer on first start, and a daily auto-rebuild
against upstream SuiteCRM releases. We do not fork Bitnami's image (now
abandoned) — this is a clean replacement built on the same conventions as
[`docker-freescout`](https://github.com/pikapods/docker-freescout).

## Quick start

The bundled `compose.yaml` brings up SuiteCRM plus a MySQL 8 sidecar:

```bash
git clone https://github.com/pikapods/docker-suitecrm.git
cd docker-suitecrm
docker compose up -d
# wait ~1-3 minutes for the silent installer to run on first boot
curl -sI 'http://localhost:8080/index.php?module=Users&action=Login'   # → HTTP/1.1 200 OK
```

Default credentials are `admin` / `changeme` — change them before any real
deployment.

Against an existing MySQL/MariaDB:

```bash
docker run -d --name suitecrm \
  -v suitecrm-data:/data \
  -e APP_URL="https://crm.example.com" \
  -e DB_HOST=db.internal \
  -e DB_NAME=suitecrm \
  -e DB_USER=suitecrm \
  -e DB_PASS=... \
  -e ADMIN_USER=admin \
  -e ADMIN_PASS=changeme \
  -p 8080:8080 \
  ghcr.io/pikapods/docker-suitecrm:latest
```

## Environment variables

### Core

| Var          | Required | Purpose                                                  |
|--------------|----------|----------------------------------------------------------|
| `APP_URL`    | yes      | Public URL (no trailing slash).                          |
| `DB_HOST`    | yes      | MySQL/MariaDB hostname.                                  |
| `DB_PORT`    | no       | DB port. Defaults to `3306`.                             |
| `DB_NAME`    | yes      | DB name. The database must already exist.                |
| `DB_USER`    | yes      | DB user. The user must already exist with rights on `DB_NAME`. |
| `DB_PASS`    | yes      | DB password.                                             |
| `ADMIN_USER` | yes      | SuiteCRM admin username (first-boot install only).       |
| `ADMIN_PASS` | yes      | SuiteCRM admin password (first-boot install only).       |
| `SITE_NAME`  | no       | Display name shown in the UI. Default `SuiteCRM`.        |

The silent installer does not accept an admin email — set the admin's email
address in the UI after first login.

### Cron

| Var                      | Default | Purpose                                                  |
|--------------------------|---------|----------------------------------------------------------|
| `ENABLE_SUITECRM_CRON`   | `TRUE`  | Set `FALSE` to disable the per-minute `cron.php` loop.   |

SuiteCRM's Schedulers (inbound email, workflows, reminders, AOR reports) are
non-functional without per-minute cron, so the default is `TRUE`.

## Boot behaviour

On first boot the bootstrap script:

1. Waits up to 30 s for MySQL on `${DB_HOST}:${DB_PORT}`.
2. Renders a `config_si.php` from environment variables.
3. Starts a transient `php -S 127.0.0.1:8765` and hits
   `install.php?goto=SilentInstall&cli=true`.
4. Verifies that `installer_locked => true` was written into `/data/config.php`
   and removes `config_si.php`.

On every subsequent boot the bootstrap reconciles the live env values for
`site_url`, `db_host_name`, `db_port`, `db_user_name`, `db_password`, and
`db_name` into `/data/config.php`. **Other keys are preserved untouched** —
the installer's `unique_key`, anything you've set via Studio or Admin Panel,
custom mail tuning, etc.

Silent install logs are streamed to the container's stderr; on failure the
response body is dumped (first 200 lines) before the container exits.

## Mounts

| Path             | Purpose                                                          |
|------------------|------------------------------------------------------------------|
| `/data`          | Persistent volume. See "What persists" below.                    |
| `/var/www/html`  | SuiteCRM source. Baked at build time — do **not** bind-mount.    |

### What persists

The image symlinks these paths into `/data` at build time:

| In-app path              | Persisted as                |
|--------------------------|-----------------------------|
| `config.php`             | `/data/config.php`          |
| `config_override.php`    | `/data/config_override.php` |
| `custom/`                | `/data/custom`              |
| `upload/`                | `/data/upload`              |
| `cache/`                 | `/data/cache`               |
| `data/` (runtime files)  | `/data/runtime-data`        |

### What does NOT persist — Module Loader caveat

`modules/` and `themes/` are **not** symlinked. Packages installed via the
Module Loader (under Admin → Module Loader) write into these trees and **will
disappear when you pull a new image tag**.

The supported customisation path is **Studio** (under Admin → Studio) — Studio
writes into `custom/`, which is persisted.

If you rely on community Module Loader packages, reinstall them after each
image upgrade, or fork this image and bake them in.

### User & permissions

Both nginx and php-fpm run as `www-data` (**UID 82 / GID 82** — Alpine's
default). How those writes surface on the host depends on your runtime:

| Setup                                  | What to do                                                                                                   |
|----------------------------------------|--------------------------------------------------------------------------------------------------------------|
| Named volume (docker or podman)        | Nothing — daemon manages ownership. Default in `compose.yaml`.                                               |
| Bind mount, rootful docker/podman      | `chown -R 82:82 <host-dir>` before first boot.                                                               |
| Bind mount, rootless podman            | Add `--userns=keep-id:uid=82,gid=82` to `podman run`.                                                        |
| Custom-UID rebuild                     | `docker build --build-arg WWW_DATA_UID=$(id -u) --build-arg WWW_DATA_GID=$(id -g) -t suitecrm:local .`       |

The bootstrap runs a preflight writability check on `/data` and refuses to
start with a readable error if ownership is wrong.

## Ports

| Port | Purpose                                                                  |
|------|--------------------------------------------------------------------------|
| 8080 | HTTP (serversideup's unprivileged default).                              |

## MySQL notes

The bundled `compose.yaml` uses `mysql:8` with
`--default-authentication-plugin=caching_sha2_password`. PHP 8.x supports
`caching_sha2_password` natively. If you point the image at an older client
toolchain or hit `Authentication plugin 'caching_sha2_password' cannot be
loaded`, swap to `mysql_native_password` on the user:

```sql
ALTER USER 'suitecrm'@'%' IDENTIFIED WITH mysql_native_password BY '...';
```

MariaDB is supported as a drop-in for MySQL — the image's `setup_db_type` is
hard-coded to `mysql`, which works for both. Pre-create the database and user
before pointing the image at it (the bootstrap sets
`setup_db_create_database=false`).

## Building locally

```bash
docker build \
  --build-arg SUITECRM_VERSION=7.15.1 \
  --build-arg PHP_VERSION=8.4 \
  -t suitecrm:test .
```

## Testing

```bash
pip install -r tests/requirements.txt

# Fast image-shape tests (no daemon-side runtime).
IMAGE=suitecrm:test pytest -v tests/ -m "not runtime"

# Full smoke (boots a real MySQL + container, ~3 minutes).
IMAGE=suitecrm:test pytest -v tests/test_runtime.py -m runtime
```

## License

SuiteCRM is AGPL-3.0; this image inherits that license.
