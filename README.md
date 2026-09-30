# rekuest-agentd

The rekuest agent protocol server, in Rust: what agents connect to (`/agi` over websocket,
`/agi/http/{id}` for hook agents).

## Why

Every rekuest deployment ran GraphQL, every agent socket and every subscription in one Python
process. Under UI load, agents' reports and heartbeat answers queued behind GraphQL work until
agents were kicked (heartbeat 3001); a thread per connection did not help (the limit is the GIL).
agentd takes the whole agent protocol out of that process. Measured with the conformance
benchmark (one agent streaming a report every 50 ms, two workers querying GraphQL at the rate):

| GraphQL load | Python server | agentd (GraphQL still on Python) |
|---|---|---|
| none | 19.5 reports/s, ack lag p50 0.22 s | 19.9 reports/s, p50 0.14 s |
| 10 queries/s × 2 | 9.2 reports/s, ack lag p50 5.3 s, **kicked (3001)** | 19.4 reports/s, p50 0.13 s |
| 26 queries/s × 2 | (the agent was already gone) | 19.2 reports/s, p50 0.12 s, no kicks |

## Shape

```
agents ──ws /agi─────┐
hook agents ─http────┤
                     ▼
            rekuest-agentd (this repo, N replicas)
                     │ internal API
   rekuest (Python) ─┘  GraphQL and subscriptions; calls agentd to assign/control
   Postgres (Django's schema: never migrated here) · Redis (agent queues, channels_redis groups)
```

agentd reads the rekuest server's own `config.yaml` (`AGENTD_CONFIG`, default `config.yaml`) and
listens on `AGENTD_BIND` (default `0.0.0.0:8080`), under the config's `force_script_name`.

## Layout: the Python server's packages, in Rust

`rekuest` is the client (the Python SDK, and arkirust's crate); everything here is the server, so
rekuest's own packages are named `rekuest-server-…` and import as `rekuest_server…`; the packages every Arkitekt server shares keep their Python names (`authentikate`, `kante`). Each mirrors the Python package whose scope it takes over and keeps its
name as the library name and its module names,
so a behaviour has the same path on both sides (`facade/liveness.py` ↔ `facade::liveness`).

| Package (library) | Mirrors | Scope |
|---|---|---|
| `rekuest-server` (`rekuest_server`) | `rekuest/` (the Django project) | `configuration`, `settings`, `urls`; the `agentd` binary |
| `rekuest-server-facade` (`facade`) | `facade/` (the app) | the agent protocol: `codes`, `liveness`, `redis_keys`, then `consumers`, `persist`, `registration`, `guards`, `provenance`, `reaper`, … |
| `rekuest-server-core` (`rekuest_core`) | `rekuest_core/` | the declaration models and their validation (Phase 1) |
| `authentikate` | `authentikate` 4.1.1 (the version the server pins) | token verification (rsa, rsa_file, jwks_dict, jwks_uri; revocation lists), static tokens, and expansion to user/org/client/membership rows |
| `kante` | `kante` (on channels_redis 4.3.0) | the channel layer, wire-compatible with Python's: groups, sends, receives; the change fan-out (`facade::signals`) runs on it |

A crate appears when its first module is ported; nothing is stubbed ahead of it.

## The contract

- **Wire:** [`rekuest-protocol`](https://github.com/arkitektio/arkirust/tree/main/crates/rekuest-protocol),
  checked against every frame the Python server knows (`agent_wire_examples.json`, generated
  by the server).
- **Behaviour:** `conformance/`, a black-box pytest suite of real sockets. It must be green
  against the Python server first, then against agentd:

  ```bash
  cd conformance
  uv run pytest                                   # brings up stack/ with dokker (the Python server)
  CONFORMANCE_URL=http://localhost:8080 uv run pytest   # any running target
  # agentd serves /agi only; GraphQL (for assigning) stays the Python server's:
  CONFORMANCE_URL=http://127.0.0.1:8480 CONFORMANCE_GRAPHQL_URL=http://localhost:5690/graphql uv run pytest
  # hook scenarios: the servers POST to a receiver in the test process; name an address they reach
  CONFORMANCE_HOOK_HOST=172.23.0.1 CONFORMANCE_URL=… uv run pytest tests/test_hooks.py
  # the liveness benchmark (skipped unless BENCH_SECONDS is set):
  BENCH_SECONDS=30 BENCH_RATES=0,10,26 BENCH_CONCURRENCY=2 CONFORMANCE_URL=… uv run pytest -s tests/test_zz_bench_liveness.py
  ```

  Registration is also checked row by row against Python's own `implement_agent`
  (`crates/facade/tests/registration_parity.rs`), and the fan-out payload by payload
  (`signals_contract.rs`).

## Phases

| Phase | Scope | Status |
|---|---|---|
| 0 | `rekuest-protocol`, this repo, conformance seed, benchmark | done |
| 1 | auth (authentikate), registration (`implement_agent`), lease/heartbeat/queue | done |
| 2 | reports, transitions, positions, state, locks, shelve, fan-out | done |
| 3 | assign, control, guards, probes, caller mirrors, internal API | done |
| 4 | agent sweeps, workflow resume | done (the reaper: `AGENTD_REAPER=0` beside a Python reaper, which shares its tick token) |
| 5 | HTTP hook agents (intake, signed delivery, caller mirrors, service-agent trust) | done |
| 6 | cutover; the Python agent path is deleted | |

## Development

```bash
eval "$(scripts/test-db.sh)"   # a Postgres the Python server migrated (database tests skip without it)
cargo test
cargo clippy --all-targets -- -D warnings
scripts/test-db.sh down
```
