# SuiteCRM 7 image — self-maintained, derived from serversideup/php.
# See README.md for design notes and usage.
#
# Build:
#   docker build \
#     --build-arg SUITECRM_VERSION=7.15.1 \
#     --build-arg PHP_VERSION=8.4 \
#     -t ghcr.io/pikapods/docker-suitecrm:7.15.1-php8.4 .

ARG PHP_VERSION=8.4
FROM serversideup/php:${PHP_VERSION}-fpm-nginx-alpine

ARG SUITECRM_VERSION=7.15.1
ARG SUITECRM_RELEASE_BASE=https://github.com/SuiteCRM/SuiteCRM/releases/download

LABEL org.opencontainers.image.title="SuiteCRM" \
      org.opencontainers.image.description="Self-maintained SuiteCRM 7 container" \
      org.opencontainers.image.source="https://github.com/pikapods/docker-suitecrm" \
      org.opencontainers.image.licenses="AGPL-3.0" \
      org.opencontainers.image.version="${SUITECRM_VERSION}"

USER root

# Runtime + build dependencies.
# Runtime: mysql-client (mysqladmin ping), tzdata, curl, unzip.
# No dcron — cron.php runs as an s6 longrun service.
RUN apk add --no-cache \
        curl \
        mysql-client \
        tzdata \
        unzip \
    && install-php-extensions \
        mysqli \
        pdo_mysql \
        gd \
        imap \
        intl \
        soap \
        ldap \
        bcmath \
        zip \
        gnupg \
        exif \
        opcache

# Fetch and unpack the SuiteCRM release tarball. The zip unpacks to
# SuiteCRM-X.Y.Z/ — strip that prefix so the app lands at /var/www/html.
# vendor/ is shipped in the release zip; we do NOT run composer.
RUN curl -fsSL -o /tmp/suitecrm.zip \
        "${SUITECRM_RELEASE_BASE}/v${SUITECRM_VERSION}/SuiteCRM-${SUITECRM_VERSION}.zip" \
    && mkdir -p /tmp/suitecrm-extract \
    && unzip -q /tmp/suitecrm.zip -d /tmp/suitecrm-extract \
    && rm -rf /var/www/html \
    && mv "/tmp/suitecrm-extract/SuiteCRM-${SUITECRM_VERSION}" /var/www/html \
    && rm -rf /tmp/suitecrm.zip /tmp/suitecrm-extract

# Replace mutable paths with symlinks into /data. Targets won't resolve until
# the bootstrap mkdir -p's them on first boot. Module Loader output under
# modules/ and themes/ is intentionally NOT persisted — Studio customisations
# under custom/ are the supported customisation path. See README.
#
# /data/runtime-data avoids the /data <-> data/ name collision: SuiteCRM's
# `data/` holds runtime caches & template files, while the volume root is /data.
RUN mkdir -p /data \
    && mv /var/www/html/data /data/runtime-data \
    && rm -rf /var/www/html/custom /var/www/html/upload /var/www/html/cache \
    && rm -f /var/www/html/config.php /var/www/html/config_override.php \
    && ln -s /data/config.php          /var/www/html/config.php \
    && ln -s /data/config_override.php /var/www/html/config_override.php \
    && ln -s /data/custom              /var/www/html/custom \
    && ln -s /data/upload              /var/www/html/upload \
    && ln -s /data/cache               /var/www/html/cache \
    && ln -s /data/runtime-data        /var/www/html/data \
    && chown www-data:www-data /data \
    && chown -R www-data:www-data /var/www/html

# Build-arg UID/GID override. See README "User & permissions".
ARG WWW_DATA_UID=82
ARG WWW_DATA_GID=82
RUN if [ "$WWW_DATA_UID" != "82" ] || [ "$WWW_DATA_GID" != "82" ]; then \
        docker-php-serversideup-set-id www-data "${WWW_DATA_UID}:${WWW_DATA_GID}" \
     && docker-php-serversideup-set-file-permissions --owner "${WWW_DATA_UID}:${WWW_DATA_GID}" \
     && chown "${WWW_DATA_UID}:${WWW_DATA_GID}" /data; \
    fi

VOLUME /data

# Overlay our entrypoint hook + s6 cron service + nginx site config.
COPY rootfs/ /

# - chmod *before* docker-php-serversideup-s6-init: the init tool moves
#   /etc/entrypoint.d/*.sh into /etc/s6-overlay/scripts/ and renames them, so
#   chmod afterwards at the original path would fail.
# - chown /etc/nginx to www-data: ServerSideUp's 10-init-webserver-config
#   runs as www-data and renders /etc/nginx/nginx.conf at boot.
RUN chmod +x /etc/entrypoint.d/20-suitecrm-bootstrap.sh \
             /etc/s6-overlay/s6-rc.d/suitecrm-cron/run \
    && chown -R www-data:www-data /etc/nginx \
    && docker-php-serversideup-s6-init

# Image defaults.
# AUTORUN_ENABLED=false: we own the boot sequence.
# SSL_MODE=off: TLS terminates at the reverse proxy.
# ENABLE_SUITECRM_CRON=TRUE: SuiteCRM Schedulers (inbound email, workflows,
# reminders) are broken without per-minute cron.
ENV AUTORUN_ENABLED=false \
    SSL_MODE=off \
    ENABLE_SUITECRM_CRON=TRUE \
    APP_BASE_DIR=/var/www/html \
    NGINX_WEBROOT=/var/www/html \
    PHP_OPCACHE_ENABLE=1

# Health endpoint hits the login page (cheap, no auth, exercises nginx + php-fpm).
# start-period bumped to 180s — silent install on first boot can take 60-180s.
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS 'http://localhost:8080/index.php?action=Login&module=Users' -o /dev/null || exit 1

EXPOSE 8080

USER www-data
