# rekuest-agentd

The rekuest agent protocol server, in Rust: what agents connect to (`/agi` over websocket,
`/agi/http/{id}` for hook agents).

## Why

Every rekuest deployment ran GraphQL, every agent socket and every subscription in one Python
process. Under UI load, agents' reports and heartbeat answers queued behind GraphQL work until
agents were kicked (heartbeat 3001). Measured with `conformance/bench`: about 26 concurrent UI
queries a second cut agent-report throughput about 5×, and a thread per connection did not help
(the limit is the GIL). agentd takes the whole agent protocol out of that process.

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
  ```

## Phases

| Phase | Scope | Status |
|---|---|---|
| 0 | `rekuest-protocol`, this repo, conformance seed, benchmark | in progress |
| 1 | auth (authentikate), registration (`implement_agent`), lease/heartbeat/queue | |
| 2 | reports, transitions, positions, state, locks, shelve, fan-out | |
| 3 | assign, control, guards, probes, caller mirrors, internal API | |
| 4 | agent sweeps, workflow resume | |
| 5 | HTTP hook agents | |
| 6 | cutover; the Python agent path is deleted | |

## Development

```bash
eval "$(scripts/test-db.sh)"   # a Postgres the Python server migrated (database tests skip without it)
cargo test
cargo clippy --all-targets -- -D warnings
scripts/test-db.sh down
```
