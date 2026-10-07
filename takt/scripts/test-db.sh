#!/usr/bin/env bash
# Bring up a Postgres with the rekuest server's schema (migrated by this repository's server) and
# print the URL takt's database tests read from TAKT_TEST_DATABASE_URL.
#   eval "$(scripts/test-db.sh)"   then   cargo test
#   scripts/test-db.sh down
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker compose -p takt-testdb -f conformance/stack/docker-compose.yml -f scripts/testdb.compose.yml)
if [ "${1:-}" = "down" ]; then "${compose[@]}" down -v >&2; exit 0; fi
port="${TAKT_TEST_DB_PORT:-5694}"
"${compose[@]}" up -d --build db redis rustfs rekuest >&2
# The server is started after its migration job; its GraphQL answering means the schema is in place.
for _ in $(seq 1 90); do
  if "${compose[@]}" exec -T rekuest python -c "import urllib.request; urllib.request.urlopen('http://localhost:80/graphql', timeout=2)" >/dev/null 2>&1; then
    echo "export TAKT_TEST_DATABASE_URL=postgres://hello_django:hello_django@localhost:${port}/rekuest"
    echo "export TAKT_TEST_REDIS_URL=redis://localhost:${TAKT_TEST_REDIS_PORT:-5695}/"
    exit 0
  fi
  sleep 2
done
echo "the rekuest server did not come up" >&2
exit 1
