#!/bin/sh
# SuiteCRM bootstrap — runs once before s6 starts nginx + php-fpm.
# POSIX sh.
set -eu

APP_DIR=/var/www/html
DATA_DIR=/data
CONFIG_PHP=${DATA_DIR}/config.php

log() { printf '[suitecrm-bootstrap] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# 0. Preflight: /data writability.
# ---------------------------------------------------------------------------
if ! ( : > "${DATA_DIR}/.write-test" ) 2>/dev/null; then
    cat >&2 <<EOF
ERROR: ${DATA_DIR} is not writable by the container (container UID:GID $(id -u):$(id -g)).
       Ownership of the bind-mount target must match how the container sees it.
       The right fix depends on your runtime — see README "User & permissions":
         - rootful docker/podman bind mount: chown the host dir to $(id -u):$(id -g)
         - rootless podman: add --userns=keep-id:uid=$(id -u),gid=$(id -g)
         - or use a named volume
         - or rebuild with --build-arg WWW_DATA_UID=...
EOF
    exit 1
fi
rm -f "${DATA_DIR}/.write-test"

# ---------------------------------------------------------------------------
# 1. Validate env.
# ---------------------------------------------------------------------------
: "${APP_URL:?APP_URL is required}"
: "${DB_HOST:?DB_HOST is required}"
: "${DB_NAME:?DB_NAME is required}"
: "${DB_USER:?DB_USER is required}"
: "${DB_PASS:?DB_PASS is required}"
: "${ADMIN_USER:?ADMIN_USER is required}"
: "${ADMIN_PASS:?ADMIN_PASS is required}"

DB_PORT=${DB_PORT:-3306}
case "$DB_PORT" in
    ''|*[!0-9]*)
        die "DB_PORT must be numeric, got '${DB_PORT}'"
        ;;
esac
SITE_NAME=${SITE_NAME:-SuiteCRM}

# ---------------------------------------------------------------------------
# 2. Provision /data tree. Idempotent.
# ---------------------------------------------------------------------------
mkdir -p \
    "${DATA_DIR}/custom" \
    "${DATA_DIR}/upload" \
    "${DATA_DIR}/cache" \
    "${DATA_DIR}/runtime-data"
touch "${DATA_DIR}/config.php" "${DATA_DIR}/config_override.php"

# ---------------------------------------------------------------------------
# 3. Wait for MySQL. 30s deadline.
# ---------------------------------------------------------------------------
log "waiting for mysql at ${DB_HOST}:${DB_PORT} (30s deadline)"
deadline=$(( $(date +%s) + 30 ))
while :; do
    if mysqladmin ping -h "$DB_HOST" -P "$DB_PORT" --silent >/dev/null 2>&1; then
        break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        die "mysql at $DB_HOST:$DB_PORT not reachable within 30s"
    fi
    sleep 1
done
log "mysql is reachable"

# ---------------------------------------------------------------------------
# 4. First-boot detection: silent install if installer_locked is not true.
# ---------------------------------------------------------------------------
needs_install=1
if [ -s "$CONFIG_PHP" ] && grep -qE "installer_locked['\"]?[[:space:]]*=>[[:space:]]*true" "$CONFIG_PHP"; then
    needs_install=0
fi

# PHP-escape a single-quoted string literal: backslash and single-quote are
# the only metacharacters inside '...' in PHP.
php_squote() {
    printf "%s" "$1" | sed -e "s/\\\\/\\\\\\\\/g" -e "s/'/\\\\'/g"
}

if [ "$needs_install" -eq 1 ]; then
    log "installer_locked not detected — running silent install"

    APP_URL_E=$(php_squote "$APP_URL")
    DB_HOST_E=$(php_squote "$DB_HOST")
    DB_NAME_E=$(php_squote "$DB_NAME")
    DB_USER_E=$(php_squote "$DB_USER")
    DB_PASS_E=$(php_squote "$DB_PASS")
    ADMIN_USER_E=$(php_squote "$ADMIN_USER")
    ADMIN_PASS_E=$(php_squote "$ADMIN_PASS")
    SITE_NAME_E=$(php_squote "$SITE_NAME")

    # Keys verified against install/install_utils.php::pullSilentInstallVarsIntoSession()
    # at v7.15.1. setup_db_create_database/user=false: we expect the DB and user
    # to already exist (compose provisions them); setup_db_drop_tables=false to
    # avoid clobbering a pre-existing instance if the operator pointed us at one.
    cat > "${APP_DIR}/config_si.php" <<PHP
<?php
\$sugar_config_si = array(
    'setup_site_url'                    => '${APP_URL_E}',
    'setup_db_host_name'                => '${DB_HOST_E}',
    'setup_db_port_num'                 => '${DB_PORT}',
    'setup_db_database_name'            => '${DB_NAME_E}',
    'setup_db_type'                     => 'mysql',
    'setup_db_manager'                  => 'MysqliManager',
    'setup_db_admin_user_name'          => '${DB_USER_E}',
    'setup_db_admin_password'           => '${DB_PASS_E}',
    'setup_db_sugarsales_user'          => '${DB_USER_E}',
    'setup_db_sugarsales_password'      => '${DB_PASS_E}',
    'setup_db_create_database'          => false,
    'setup_db_create_sugarsales_user'   => false,
    'setup_db_drop_tables'              => false,
    'setup_db_collation'                => 'utf8_general_ci',
    'setup_db_charset'                  => 'utf8',
    'setup_site_admin_user_name'        => '${ADMIN_USER_E}',
    'setup_site_admin_password'         => '${ADMIN_PASS_E}',
    'setup_system_name'                 => '${SITE_NAME_E}',
    'setup_license_accept'              => true,
    'demoData'                          => 'no',
    'default_currency_iso4217'          => 'USD',
    'default_currency_name'             => 'US Dollar',
    'default_currency_significant_digits' => 2,
    'default_currency_symbol'           => '\$',
    'default_date_format'               => 'Y-m-d',
    'default_time_format'               => 'H:i',
    'default_decimal_seperator'         => '.',
    'default_export_charset'            => 'ISO-8859-1',
    'default_language'                  => 'en_us',
    'default_locale_name_format'        => 's f l',
    'default_number_grouping_seperator' => ',',
    'export_delimiter'                  => ',',
);
PHP
    chmod 600 "${APP_DIR}/config_si.php"

    # Spin up a transient PHP web server for the installer. install.php uses
    # header() and \$_SERVER, so CLI invocation requires heavy shimming —
    # the built-in server is the path of least resistance.
    php -S 127.0.0.1:8765 -t "$APP_DIR" >/tmp/silent-install.log 2>&1 &
    PHP_PID=$!
    # Ensure cleanup even on early exit.
    trap 'kill "$PHP_PID" 2>/dev/null || true; rm -f "${APP_DIR}/config_si.php"' EXIT INT TERM

    # Wait for the port to come up (≤10s). Any HTTP response — including a
    # non-2xx — proves the server is listening; we use `-o /dev/null -w` so a
    # 4xx from the installer's redirect chain still counts as "up".
    port_deadline=$(( $(date +%s) + 10 ))
    while :; do
        code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 \
            'http://127.0.0.1:8765/install.php' 2>/dev/null || true)
        if [ "$code" != "000" ]; then
            break
        fi
        if [ "$(date +%s)" -ge "$port_deadline" ]; then
            kill "$PHP_PID" 2>/dev/null || true
            cat /tmp/silent-install.log >&2 || true
            die "transient php server did not come up within 10s"
        fi
        sleep 1
    done

    log "invoking silent installer (up to 300s)"
    if ! curl -fsS --max-time 300 \
            -o /tmp/silent-install.body \
            -D /tmp/silent-install.headers \
            'http://127.0.0.1:8765/install.php?goto=SilentInstall&cli=true' 2>/tmp/silent-install.err; then
        kill "$PHP_PID" 2>/dev/null || true
        wait "$PHP_PID" 2>/dev/null || true
        log "silent install HTTP call failed — dumping artifacts:"
        log "--- /tmp/silent-install.log ---"
        cat /tmp/silent-install.log >&2 || true
        log "--- /tmp/silent-install.err ---"
        cat /tmp/silent-install.err >&2 || true
        log "--- /tmp/silent-install.body (first 200 lines) ---"
        head -n 200 /tmp/silent-install.body >&2 || true
        die "silent install failed"
    fi

    kill "$PHP_PID" 2>/dev/null || true
    wait "$PHP_PID" 2>/dev/null || true
    trap - EXIT INT TERM
    rm -f "${APP_DIR}/config_si.php"

    if ! grep -qE "installer_locked['\"]?[[:space:]]*=>[[:space:]]*true" "$CONFIG_PHP"; then
        log "silent install completed without writing installer_locked=true"
        log "--- /tmp/silent-install.body (first 200 lines) ---"
        head -n 200 /tmp/silent-install.body >&2 || true
        die "installer_locked not set after silent install"
    fi
    log "silent install complete — installer_locked=true"
    rm -f /tmp/silent-install.body /tmp/silent-install.headers /tmp/silent-install.err /tmp/silent-install.log
else
    log "installer_locked=true detected — skipping silent install"
fi

# ---------------------------------------------------------------------------
# 5. DB/URL reconciliation on every boot. Mutates only dbconfig and site_url;
#    leaves installer_locked, unique_key, and everything else untouched.
# ---------------------------------------------------------------------------
log "reconciling /data/config.php with current env"
APP_URL_E=$(php_squote "$APP_URL")
DB_HOST_E=$(php_squote "$DB_HOST")
DB_NAME_E=$(php_squote "$DB_NAME")
DB_USER_E=$(php_squote "$DB_USER")
DB_PASS_E=$(php_squote "$DB_PASS")

php -r "
\$config_path = '${CONFIG_PHP}';
\$sugar_config = array();
include \$config_path;
if (!is_array(\$sugar_config)) {
    fwrite(STDERR, \"config.php did not define \\\$sugar_config as array\n\");
    exit(1);
}
if (!isset(\$sugar_config['dbconfig']) || !is_array(\$sugar_config['dbconfig'])) {
    \$sugar_config['dbconfig'] = array();
}
\$sugar_config['site_url']                    = '${APP_URL_E}';
\$sugar_config['dbconfig']['db_host_name']    = '${DB_HOST_E}';
\$sugar_config['dbconfig']['db_port']         = '${DB_PORT}';
\$sugar_config['dbconfig']['db_user_name']    = '${DB_USER_E}';
\$sugar_config['dbconfig']['db_password']     = '${DB_PASS_E}';
\$sugar_config['dbconfig']['db_name']         = '${DB_NAME_E}';
\$sugar_config['dbconfig']['db_type']         = 'mysql';
\$sugar_config['dbconfig']['db_manager']      = 'MysqliManager';
\$dump = \"<?php\n\" . '\$sugar_config = ' . var_export(\$sugar_config, true) . \";\n\";
if (file_put_contents(\$config_path, \$dump) === false) {
    fwrite(STDERR, \"could not write \$config_path\n\");
    exit(1);
}
" || die "config reconciliation failed"

# ---------------------------------------------------------------------------
# 6. Permissions sweep on /data — symlink targets must be writable by www-data.
# ---------------------------------------------------------------------------
# Owned by the running container UID/GID (set by docker-php-serversideup-set-id
# at build time when WWW_DATA_UID overridden).
chown -R "$(id -u):$(id -g)" \
    "${DATA_DIR}/custom" \
    "${DATA_DIR}/upload" \
    "${DATA_DIR}/cache" \
    "${DATA_DIR}/runtime-data" 2>/dev/null || true

log "bootstrap complete"
