# syntax=docker/dockerfile:1
# Construction Hermes operations service (web dashboard, MCP tool server, worker, backups).
FROM python:3.11-slim-bookworm

ARG PG_MAJOR=16
# Set to 0 only for wiring tests where apt mirrors are unreachable; backups need the client.
ARG INSTALL_PG_CLIENT=1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# PostgreSQL client matching the server major version (pg_dump must be >= server).
# Optional build secret `extra_ca` (PEM) for networks behind a TLS-intercepting proxy; unused otherwise.
RUN --mount=type=secret,id=extra_ca,required=false sh -ec '\
  [ "${INSTALL_PG_CLIENT}" = "1" ] || exit 0; \
  CA=/etc/ssl/certs/ca-certificates.crt; \
  if [ -s /run/secrets/extra_ca ]; then cat "$CA" /run/secrets/extra_ca > /tmp/ca.pem; CA=/tmp/ca.pem; fi; \
  sed -i "s|http://deb.debian.org|https://deb.debian.org|g" /etc/apt/sources.list.d/debian.sources; \
  apt-get -o Acquire::https::CAInfo="$CA" update; \
  apt-get -o Acquire::https::CAInfo="$CA" install -y --no-install-recommends curl gnupg; \
  install -d /usr/share/postgresql-common/pgdg; \
  curl --cacert "$CA" -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list; \
  apt-get -o Acquire::https::CAInfo="$CA" update; \
  apt-get -o Acquire::https::CAInfo="$CA" install -y --no-install-recommends postgresql-client-${PG_MAJOR}; \
  apt-get purge -y curl gnupg; apt-get autoremove -y; rm -rf /var/lib/apt/lists/* /tmp/ca.pem'

RUN useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin chops \
 && install -d -o chops -g chops -m 0750 /data /data/documents /backups
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=secret,id=extra_ca,required=false sh -ec '\
  if [ -s /run/secrets/extra_ca ]; then cat /etc/ssl/certs/ca-certificates.crt /run/secrets/extra_ca > /tmp/ca.pem; export PIP_CERT=/tmp/ca.pem; fi; \
  pip install --no-cache-dir . ; rm -rf /root/.cache /tmp/ca.pem'

ENV CHOPS_PG_DUMP=/usr/lib/postgresql/${PG_MAJOR}/bin/pg_dump CHOPS_PG_RESTORE=/usr/lib/postgresql/${PG_MAJOR}/bin/pg_restore

USER chops
EXPOSE 8640 8641
HEALTHCHECK --interval=30s --timeout=10s --retries=3 CMD ["chops", "health"]
ENTRYPOINT ["chops"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8640"]
