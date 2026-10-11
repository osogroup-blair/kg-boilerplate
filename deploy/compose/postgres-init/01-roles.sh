#!/bin/bash
# Runs once, as the Postgres superuser, on first start of an empty data directory.
# kg_owner owns the schema and runs migrations. It has BYPASSRLS so the SECURITY DEFINER auth functions it owns can look up credentials
# before any workspace is known (L3); it is never used at runtime. kg_app (API + worker) can never bypass row-level security (L1).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE kg_owner LOGIN NOSUPERUSER BYPASSRLS NOCREATEROLE PASSWORD '${KG_OWNER_PASSWORD}';
CREATE ROLE kg_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '${KG_APP_PASSWORD}';
CREATE DATABASE kg OWNER kg_owner;
CREATE DATABASE kg_test OWNER kg_owner;
CREATE DATABASE kg_scratch OWNER kg_owner;   -- migration round-trip tests (T-DB-06) so they never wipe kg_test mid-run
SQL

for db in kg kg_test kg_scratch; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<SQL
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE ALL ON DATABASE ${db} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${db} TO kg_app, kg_owner;
SQL
done
