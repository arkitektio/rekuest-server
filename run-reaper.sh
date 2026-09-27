#!/bin/bash
# The reaper — the second process of a rekuest deployment (the `rekuest-reaper` container).
#
# Every deadline, schedule and delayed task fires from this loop (facade/reaper.py); the web
# replicas never sweep. No `migrate` here: the web process (`run.sh`) owns the schema. If this
# starts before the first migration has run, its first ticks fail, are logged, and are retried.
set -euo pipefail

echo "=> Validating configuration"
python manage.py validate_settings

echo "=> Waiting for DB to be online"
python manage.py wait_for_database -s 2

echo "=> Starting the reaper"
exec python manage.py reaper
