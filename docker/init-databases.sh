#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
  --set rags_password="$RAGS_DB_PASSWORD" --set agent_password="$AGENT_DB_PASSWORD" <<'SQL'
CREATE ROLE rags_app LOGIN PASSWORD :'rags_password';
CREATE ROLE agent_app LOGIN PASSWORD :'agent_password';
CREATE DATABASE rags OWNER rags_app;
CREATE DATABASE agentic OWNER agent_app;
REVOKE ALL ON DATABASE rags FROM PUBLIC;
REVOKE ALL ON DATABASE agentic FROM PUBLIC;
GRANT CONNECT ON DATABASE rags TO rags_app;
GRANT CONNECT ON DATABASE agentic TO agent_app;
\connect rags
CREATE EXTENSION vector WITH SCHEMA public;
SQL
