#!/bin/bash
# Runs once on first database init. Creates the schema-owner role (migrations) and the
# least-privilege runtime role. Passwords come from the container environment.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<SQL
CREATE ROLE chops_owner LOGIN PASSWORD '${CHOPS_OWNER_DB_PASSWORD}';
CREATE ROLE chops_app LOGIN PASSWORD '${CHOPS_APP_DB_PASSWORD}';
CREATE DATABASE chops OWNER chops_owner;
REVOKE ALL ON DATABASE chops FROM PUBLIC;
GRANT CONNECT ON DATABASE chops TO chops_app;
SQL
