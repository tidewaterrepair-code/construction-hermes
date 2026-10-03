# Construction Hermes operations service (web dashboard, MCP tool server, worker, backups).
FROM python:3.11-slim-bookworm

ARG PG_MAJOR=16
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# PostgreSQL client matching the server major version (pg_dump must be >= server).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-client-${PG_MAJOR} \
 && apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin chops
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /root/.cache
ENV CHOPS_PG_DUMP=/usr/lib/postgresql/${PG_MAJOR}/bin/pg_dump CHOPS_PG_RESTORE=/usr/lib/postgresql/${PG_MAJOR}/bin/pg_restore

USER chops
EXPOSE 8640 8641
HEALTHCHECK --interval=30s --timeout=10s --retries=3 CMD ["chops", "health"]
ENTRYPOINT ["chops"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8640"]
