#!/bin/bash
set -euo pipefail
echo "=> Waiting for DB to be online"
python manage.py wait_for_database -s 6

echo "=> Performing database migrations..."
python manage.py migrate

# No reconcile step: this process serves GraphQL only. Agent and task state is agentd's, and its
# sweeps heal whatever a previous process left behind (stuck agents, orphaned or undelivered
# work) on their first tick. See agentd/crates/facade/src/reaper.rs.

# Start the first process
echo "=> Starting Server"
daphne -b 0.0.0.0 -p 80 --websocket_timeout -1 rekuest.asgi:application