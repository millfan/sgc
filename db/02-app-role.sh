#!/bin/sh
# Creates the application role with INSERT and SELECT only, so no agent can rewrite history.
# The password comes from APP_DB_PASSWORD (set in .env), never from the repository.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_password="$APP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE stockassistant_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'stockassistant_app') \gexec
GRANT SELECT, INSERT ON journal_entries, kb_chunks TO stockassistant_app;
GRANT USAGE ON SEQUENCE journal_entries_id_seq, kb_chunks_id_seq TO stockassistant_app;
SQL
