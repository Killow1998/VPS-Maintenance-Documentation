# syntax=docker/dockerfile:1
# Adapted from the locked XBoard source's Dockerfile; its MIT license is in /www/LICENSE.
ARG PHP_IMAGE
ARG COMPOSER_IMAGE
ARG EXTENSION_INSTALLER_IMAGE
FROM ${COMPOSER_IMAGE} AS composer
FROM ${EXTENSION_INSTALLER_IMAGE} AS extensions
FROM ${PHP_IMAGE} AS runtime

COPY --from=extensions /usr/bin/install-php-extensions /usr/local/bin/
COPY --from=composer /usr/bin/composer /usr/local/bin/composer
RUN CFLAGS="-O0" IPE_PROCESSOR_COUNT=2 install-php-extensions pcntl && \
    CFLAGS="-O0 -g0" IPE_PROCESSOR_COUNT=2 install-php-extensions bcmath
RUN <<'SH'
set -eu
mkdir -p /tmp/extensions
set -- pdo_mysql sockets zip
while read -r package checksum; do
    archive="/tmp/extensions/$package.tgz"
    curl -fsSL --retry 5 --connect-timeout 20 --max-time 180 \
        "https://pecl.php.net/get/$package.tgz" -o "$archive"
    printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c -
    mkdir -p "/tmp/extensions/$package"
    tar -xzf "$archive" -C /tmp/extensions
    mv /tmp/extensions/package.xml "/tmp/extensions/$package/package.xml"
    set -- "$@" "/tmp/extensions/$package"
done <<'EXTENSIONS'
igbinary-3.2.16 8bf25d465abc7973d9e2c9a3039a5f8eea635b23bc1477017ff3999ff95836da
redis-6.3.0 0d5141f634bd1db6c1ddcda053d25ecf2c4fc1c395430d534fd3f8d51dd7f0b5
EXTENSIONS
IPE_PROCESSOR_COUNT=2 install-php-extensions "$@"
rm -rf /tmp/extensions
SH
RUN <<'SH'
set -eu
package=swoole-6.2.3
checksum=21dfa1c4a4a09f4d1cb4b154f7defdc91fbabafc8d44a77f8f618b9742c67304
curl -fsSL --retry 5 --connect-timeout 20 --max-time 180 \
    "https://pecl.php.net/get/$package.tgz" -o /tmp/swoole.tgz
printf '%s  %s\n' "$checksum" /tmp/swoole.tgz | sha256sum -c -
mkdir -p /tmp/swoole-src
tar -xzf /tmp/swoole.tgz -C /tmp/swoole-src
mv /tmp/swoole-src/package.xml "/tmp/swoole-src/$package/package.xml"
IPE_PROCESSOR_COUNT=2 IPE_SWOOLE_WITHOUT_IOURING=1 install-php-extensions "/tmp/swoole-src/$package"
rm -rf /tmp/swoole-src /tmp/swoole.tgz /usr/local/lib/php/test
SH
RUN apk add --no-cache shadow sqlite mysql-client mariadb-connector-c git supervisor redis caddy && \
    addgroup -S -g 1000 www && adduser -S -G www -u 1000 www && \
    (getent group redis || addgroup -S redis) && \
    (getent passwd redis || adduser -S -G redis -H -h /data redis)

WORKDIR /www
COPY source/ /www/
COPY source/.docker/ /
COPY source/.docker/supervisor/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY source/.docker/caddy/Caddyfile /etc/caddy/Caddyfile
COPY source/.docker/php/zz-xboard.ini /usr/local/etc/php/conf.d/zz-xboard.ini
RUN test ! -e /www/.env && \
    composer install --no-dev --no-interaction --prefer-dist --no-progress --no-scripts && \
    php artisan package:discover --ansi && \
    php artisan storage:link && \
    find /www/storage/logs -type f ! -name .gitignore -delete && \
    test ! -e /www/.env && \
    chown -R www:www /www && chmod -R 775 /www && \
    mkdir -p /data && chown redis:redis /data && chmod 755 /data && \
    chmod +x /entrypoint.sh && \
    php -r 'if (PHP_VERSION !== "8.2.35") exit(1); foreach (["swoole","redis","igbinary","pcntl","bcmath","pdo_mysql","pdo_sqlite","sockets","zip"] as $e) if (!extension_loaded($e)) exit(2);' && \
    composer check-platform-reqs --no-dev
ARG UPSTREAM_COMMIT
LABEL org.opencontainers.image.title="Clean XBoard" \
    org.opencontainers.image.source="https://github.com/Killow1998/VPS-Maintenance-Documentation" \
    org.opencontainers.image.revision="${UPSTREAM_COMMIT}" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.description="Uninitialized XBoard from pinned public source; no production snapshot or accounts"
ENV ENABLE_WEB=true ENABLE_HORIZON=true ENABLE_REDIS=true ENABLE_WS_SERVER=true ENABLE_CADDY=true
EXPOSE 7001
ENTRYPOINT ["/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
