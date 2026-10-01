#!/bin/bash
# The server's background loop (the `rekuest-reaper` container), see facade/reaper.py.
#
# It does two things: provisions this hub's services as HookAgents (facade/service_agents.py)
# and re-embeds actions whose embedding is stale (embeddings/healer.py). Deadlines, schedules,
# triggers and retention are agentd's sweeps, not this loop's.
#
# No `migrate` here: the web process (`run.sh`) owns the schema. If this starts before the
# first migration has run, its first ticks fail, are logged, and are retried.
set -euo pipefail

echo "=> Validating configuration"
python manage.py validate_settings

echo "=> Waiting for DB to be online"
python manage.py wait_for_database -s 2

echo "=> Starting the reaper"
exec python manage.py reaper
