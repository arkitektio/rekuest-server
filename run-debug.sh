#!/bin/bash
# Fail loudly: without this a failed migrate still started the server against a half-migrated DB.
set -euo pipefail
echo "=> Waiting for DB to be online"
python manage.py wait_for_database -s 2

# `migrate` is this project's own command (facade/management/commands/migrate.py): it takes a
# Postgres advisory lock, so any number of replicas may boot at once — one migrates, the rest wait.
echo "=> Performing database migrations..."
python manage.py migrate

# (The `ensureadmin` step that used to follow was never a registered command — it failed
# silently on every boot. Removed, as in run.sh.)

# Start the first process
echo "=> Starting Server"
python manage.py runserver 0.0.0.0:80
