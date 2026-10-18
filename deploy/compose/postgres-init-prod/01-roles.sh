#!/bin/bash
# Production first boot (empty data directory only): the two application roles and the one database. Passwords come from Docker secrets (*_FILE), never from the environment.
# No kg_test / kg_scratch here: they exist for the test suites only.
set -euo pipefail

owner_pw="$(tr -d '\r\n' < "${KG_OWNER_PASSWORD_FILE:?KG_OWNER_PASSWORD_FILE is required}")"
app_pw="$(tr -d '\r\n' < "${KG_APP_PASSWORD_FILE:?KG_APP_PASSWORD_FILE is required}")"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -v owner_pw="$owner_pw" -v app_pw="$app_pw" <<'SQL'
CREATE ROLE kg_owner LOGIN NOSUPERUSER BYPASSRLS NOCREATEROLE PASSWORD :'owner_pw';
CREATE ROLE kg_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD :'app_pw';
CREATE DATABASE kg OWNER kg_owner;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname kg <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE ALL ON DATABASE kg FROM PUBLIC;
GRANT CONNECT ON DATABASE kg TO kg_app, kg_owner;
SQL
