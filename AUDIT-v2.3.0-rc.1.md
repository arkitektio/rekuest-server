# Audit — rekuest-server v2.3.0-rc.1

> **Remediation status.** All five promotion blockers plus most secondary findings are fixed;
> the suite is green at **721 passed** (was 445 at the time of this audit). Two items are deliberately open pending a product
> decision (state-history API, untenanted models) and one is operational (key rotation).
> See *Remediation status* at the end.


Whole-codebase audit across **correctness**, **security**, and **docs-vs-code drift**, run against
`next` @ `3127863` (v2.3.0-rc.1), the candidate for promotion to stable `main`.

## Verdict: do not promote

The headline is not any single bug. It is that **this codebase has authentication but almost no
authorization**. Every GraphQL operation correctly requires a valid token — that part fails closed
and was verified empirically. But once authenticated, a user of any organization can read, mutate,
and delete most other organizations' data by passing an ID.

Roughly **60 of ~110 GraphQL fields** apply no ownership check. This is systemic, not a list of
oversights, and it predates this rc.

| # | Sev | Finding | Location |
|---|---|---|---|
| 1 | **Critical** | Tenant isolation is largely absent across the GraphQL surface (~60 fields) | `facade/schema.py:39-250` |
| 2 | **Critical** | Any agent can complete, fail, or forge results on **any** task in any org | `facade/persist_backend.py:584-683` |
| 3 | **Critical** | `cleanupActions` deletes unreferenced Actions across **every** organization | `facade/mutations/action.py:17` |
| 4 | **Critical** | Agent re-registration cascade-deletes task history **and in-flight tasks** | `facade/mutations/agent.py:206` |
| 5 | **Critical** | Three media mutations carry no `@auth` directive; `DatalayerStore` has no owner column | `facade/schema.py:212-223` |
| 6 | **High** | Agent lifecycle control (`kick`/`block`/`delete`) accepts any agent ID | `facade/backend.py:205-578` |
| 7 | **High** | Entire state-history API is dead — resolvers reference nonexistent columns | `facade/queries/state.py` |
| 8 | **High** | `DEBUG = True` hardcoded; defeats authentikate's static-token tripwire | `rekuest/settings.py:28` |
| 9 | **High** | Committed provenance private key allows forging attestations | `config.yaml:27-30` |

---

## 1. Tenant isolation is largely absent — Critical

Authentication is sound. `AuthentikateExtension.on_operation` raises before resolution on any
missing or invalid credential — verified: a tokenless `{ _service { sdl } }` returns
`NO_AUTHORIZATION_HEADER` with `data = None`. Nothing is anonymously reachable.

But the `@auth` directive attached at `facade/schema.py:23,29,34` is passed **no** `roles`,
`scopes`, or `org_roles`, so it asserts only "a user is set". There is no role or scope
authorization anywhere in the codebase, and `facade/schema.py:17-23` accepts a
`permission_classes` argument that it silently discards (zero call sites).

Scoping therefore exists in exactly three places: `build_prescoped_queryset`
(`facade/types/base.py:6-12`), explicit `organization=` filters in individual resolvers, and
`organization_id=` passed to the matcher.

**Only 4 of ~35 auto-generated types have a `get_queryset`** — `actions`, `agents`,
`implementations`, `tasks`. Unscoped list fields include `clients`, `states`, `sessions`, `bloks`,
`dashboards`, `resolutions`, `memory_shelves`, `test_results`, `shortcuts`, `toolboxes`,
`placements`, `threed_models`, and the federation `_entities` resolver. Several of those models
*have* an unused `organization` FK (`Resolution`, `Space`).

A further **16 inline single-object resolvers** in `facade/schema.py:100-165` are plain
`Model.objects.get(id=id)` with no filter: `resolution`, `state`, `memory_shelve`, `blok`,
`dashboard`, `test_case`, `implementation`, `task`, and others.

**Failure scenario.** A user in org A issues `{ states { id value agent { id name } } }`. They
receive every State row in the deployment, including org B's agent names and state payloads.

**Executed, not inferred.** `tests/graphql/test_cross_tenant_isolation.py` (added by this audit)
builds two independent tenants and queries as org A. The control passes; the isolation assertions
fail:

```
PASSED  test_actions_are_scoped_to_the_callers_organization   # Action IS scoped
FAILED  test_states_are_scoped_to_the_callers_organization    # org A sees org B's states
FAILED  test_single_state_lookup_is_scoped
        AssertionError: org A fetched org B's State by id
          data = {'state': {'id': '6', 'interface': 'xt-state-by-id-b-iface'}}, errors = None
```

The `-b-` in the returned `interface` is org B's seed prefix: org A fetched the other tenant's row
by ID, with no error raised. The passing control proves the harness, contexts and assertions are
sound, so the two failures are the defect rather than a test bug. There is no filter to bypass —
none was applied.

*(These three now pass; see* Remediation status *. The output above is the pre-fix state, kept as
the evidence for the finding.)*

**Traversal caveat.** The four scoped types are reachable *around*: a to-one FK
(`State.agent`, `Placement.agent`) is resolved by attribute and does not re-enter
`Agent.get_queryset`, so `{ states { agent { … } } }` returns unscoped agents through a scoped type.

**Mutations are worse than queries**, because they write. `update_dashboard`
(`facade/mutations/dashboard.py:36-37`) lets a caller set `dashboard.organization` to an arbitrary
value. `create_blok` and `create_toolbox` use `update_or_create(name=…)` against a global name, so
any user can hijack another org's blok or toolbox by name.

## 2. The agent report path is unauthorized — Critical

`facade/message_router.py:120-161` passes `agent_id` into every FromAgent handler. The handlers in
`facade/persist_backend.py` accept it and **never filter on it**:

```python
async def on_agent_done(self, agent_id: int, message: messages.Completed) -> None:
    ...
    x = await models.Task.objects.aget(id=message.task)   # agent_id unused
    await models.TaskEvent.objects.acreate(task=x, kind=enums.TaskEventKind.COMPLETED)
```

The same shape at `on_agent_started:584`, `on_agent_log:586`, `on_agent_yield:596`,
`on_agent_cancelled:628`, `on_agent_error:645`, `on_agent_critical:662`, `on_agent_progress:674`.

This is a bug rather than a design choice, and the discriminator is in the same file:
`_caller_control_sync:496-500` explicitly raises `PermissionError` when
`task.caller_id != caller.pk`, and `on_agent_state_patch:718` scopes its lookup with
`State.objects.aget(agent_id=agent_id, …)`. The report path is the only one that does neither.

**Failure scenario.** An authenticated agent in org A sends `Completed(task="<task id in org B>",
returns={...})` over its websocket. The handler loads org B's task without checking ownership,
writes a `COMPLETED` TaskEvent with attacker-chosen `returns`, marks it done, and fans the result
out to the real caller — who receives fabricated results as genuine. The same works for `Failed`
and `Critical`. Task IDs are exposed by the unscoped queries in #1.

The probe equivalent is the same: `facade/probes/persist.py:86-101,124-129` accept `agent_id` and
drop it, resolving by caller-supplied `message.task`.

## 3. `cleanupActions` deletes across all organizations — Critical

`facade/mutations/action.py:10-26`, exposed at `facade/schema.py:205`.

```python
if action_ids:
    actions_to_check = models.Action.objects.filter(id__in=action_ids, organization=info.context.request.organization)
else:
    actions_to_check = models.Action.objects          # <-- no organization filter
```

**Failure scenario.** A user in org A calls `mutation { cleanupActions }` with no arguments. Every
Action with zero implementations, in every organization, is deleted — and `Task.action` is
`on_delete=CASCADE` (`facade/models/task.py:31`), so their Tasks and TaskEvents go too. The
function's own `# TODO: Check that user has permission to delete actions` confirms the gap.

## 4. Agent re-registration cascade-deletes history and live work — Critical

`facade/mutations/agent.py:205-206`, inside `@transaction.atomic`:

```python
models.State.objects.filter(agent=agent).exclude(id__in=created_states_id).delete()
models.Implementation.objects.filter(agent=agent).exclude(id__in=created_implementations_id).delete()
```

`Task.implementation` is `null=True, blank=True` but `on_delete=models.CASCADE`
(`facade/models/task.py:15-22`).

**Failure scenario.** An agent restarts having renamed or removed an implementation — a routine
deploy. The reap deletes the undeclared `Implementation` rows, and the CASCADE takes every `Task`
that referenced them: all completed history, plus any task currently running with `is_done=False`,
plus their `TaskEvent` / `TaskInstruct` / `Patch` rows.

This contradicts the intent expressed in `facade/retention.py:50-59`, where deletion is opt-in
(`TASK_RETENTION_SECONDS` defaults to `0` = never), batched, and guarded by
`Exists(live_descendants)`. The careful path is guarded; this client-triggered one is not.

**Fix.** `Task.implementation` should be `SET_NULL` — the field is already nullable — and the reap
should refuse implementations with non-terminal tasks.

## 5. Media mutations have no authorization at all — Critical

Three registered mutations carry **no `@auth` directive**, proven by the generated SDL:

```graphql
cleanupActions(actionIds: [ID!] = null): Int! @auth(required_scopes: null, ...)
requestMediaUpload(input: RequestMediaUploadInput!): MediaUploadGrant!   # no directive
finishMediaUpload(input: FinishMediaUploadInput!): MediaStore!          # no directive
requestMediaAccess(input: RequestMediaAccessInput!): MediaAccessGrant!  # no directive
```

Cause: `facade/schema.py:212-223` uses `kante.django_mutation`, which is `strawberry_django.mutation`
verbatim (`kante/type.py:64`) and bypasses the local `mutation()` wrapper at `facade/schema.py:26-29`.

Compounding it, `DatalayerStore` (`datalayer/models.py:30-76`) has **no organization, user, or
creator column**, so `request_media_access` has no ownership to check even in principle —
`MediaStore.objects.get(id=model.store_id)` is the entire authorization
(`datalayer/mutations/media.py:24-31`).

**Failure scenario.** Any authenticated user calls `requestMediaAccess(input: {store: "<any store
id>"})` and receives scoped STS S3 read credentials for another organization's media object.

**Not at fault:** the S3 credential machinery itself is well-built — STS `AssumeRole` with a
per-object inline session policy (`datalayer/datalayer.py:389-432`), duration clamped to
`[900, 43200]`, server-generated opaque keys the client cannot choose, and `_unscoped_fallback`
refusing by default. The exposure is the missing owner column and the missing directive.

## 6. Agent and task control accept any ID — High

`bounce`, `kick`, `block`, `unblock` (`facade/backend.py:514-546`) and `delete_agent` /
`update_agent` (`facade/mutations/agent.py:262-273`) resolve `Agent.objects.get(id=input.agent)`
with no organization check. Likewise `cancel`, `pause`, `resume`, `interrupt`
(`facade/backend.py:205-245`) — and `interrupt` passes `propagate_children=True`.

**Failure scenario.** A user in org A calls `mutation { block(input: {agent: "<org B agent>"}) }`
and permanently blocks another organization's production agent. Or calls `interrupt` on a root task
in org B and kills its entire task tree.

## 7. The state-history API is dead — High

`facade/queries/state.py` and `facade/types/state.py` reference five columns that do not exist.
`Patch` and `Snapshot` (`facade/models/state.py:81-102`) define exactly one revision column:
`global_rev`. Verified directly, no database required — Django resolves field names at queryset
construction:

```
global_current_revision : FieldError: Cannot resolve keyword into field. Choices are: ... global_rev ...
global_future_revision  : FieldError
current_revision        : FieldError
revision                : FieldError
future_revision         : FieldError
global_rev              : (resolves — reaches the DB)
```

All seven public root queries route through the affected helpers: `taskBoundaries`,
`sessionBoundaries`, `stateAtGlobalRev`, `stateAtLocalRev`, `forwardEventsAfterRev`,
`patchEventsBetweenGlobalRevs`, `snapshotsAroundRev` (`facade/schema.py:91-97`). The phantom
columns are also declared on the object types, so they ship in the public SDL as **non-nullable**
(`currentRevision: Int!` etc.).

**Failure scenario.** Any client calls `taskBoundaries` or selects `Patch.currentRevision` and gets
a `FieldError`. Clients generating code from the SDL treat these as guaranteed integers.

**Note.** `tests/test_print_schema.py::test_no_phantom_column_ordering_keys` was written to catch
exactly this bug class, but scopes its assertion to `TaskOrder` / `ImplementationOrder` ordering
inputs. The same defect in object types and resolvers went undetected.

Separately, these resolvers are also unscoped: with no `state_id`, `_get_state_ids` enumerates every
Snapshot and Patch instance-wide.

## 8. `DEBUG = True` defeats the static-token tripwire — High

`rekuest/settings.py:28` hardcodes `DEBUG = True`; `conf.django.debug` is never read, so
`config.yaml`'s `debug: false` is ignored. Same for `ALLOWED_HOSTS = ["*"]` at `:30`.

Precisely stated: `DEBUG=True` does not *enable* the static-token bypass — that fires on map
membership regardless (`authentikate/utils.py:48`). What it does is **kill the only tripwire**:
`authentikate/settings.py:23`'s `if parsed.static_tokens and not settings.DEBUG:` refusal can never
run.

**Verified empirically.** With `AUTHENTIKATE__STATIC_TOKENS='{"pwned":{...}}'` set via env,
`get_settings()` returns without raising and the token materializes with `roles: ['admin']` and a
24h expiry — a signature-free admin credential from one environment variable.

Not exploitable as shipped (`config.yaml:48` has `static_tokens: {}`), but the guard designed to
catch it is dead. `DEBUG=True` independently leaks tracebacks and settings on 500s, alongside
`ALLOWED_HOSTS=["*"]` and `CORS_ALLOW_ALL_ORIGINS=True` (`:154`).

## 9. Committed secrets — High

| Item | Location | Impact |
|---|---|---|
| Provenance Ed25519 **private key** | `config.yaml:27-30` | Whoever holds it can forge provenance attestations that verify against the published JWKS (`rekuest/urls.py:29-38`). Comment says "replace in production". |
| Django `SECRET_KEY` | `config.yaml:9` | Session/signing compromise. `settings.py:25` carries `# TODO: Change this in production`. |
| Postgres password | `config.yaml:53` | Committed. |
| `authentikate.audience: "*"` | `config.yaml:42` | ANY_AUDIENCE — a token the IdP minted for *any* service is accepted here. |
| `allowed_organizations` unset | absent | Every distinct `org` claim auto-creates an Organization + Membership, making token claims an unbounded DB-write primitive. |

`facade/management/commands/validate_settings.py` redacts secrets correctly but checks none of
`DEBUG`, `hosts`, `audience`, or static tokens.

---

## Correctness findings

| # | Sev | Finding | Location |
|---|---|---|---|
| C4 | Medium | `get_latest_state` swallows every patch failure (`except Exception: pass`), silently yielding a partial state; `:120` dereferences a `.first()` that can be `None`. | `facade/logic.py:100-186` |
| C7 | Low | `force_script_name` is assigned to `MY_SCRIPT_NAME`, not Django's `FORCE_SCRIPT_NAME`; nothing reads it, so the documented option is inert. | `rekuest/settings.py:151` |
| C9 | Low | Real type error in the new fencing path: `int \| None` passed to a parameter typed `int`. | `facade/consumers/agent_protocol.py:434` |
| C10 | Low | `Agent.user` declared twice; the second silently overrides the first. | `facade/types/agent.py:31,34` |

## Process findings

| # | Sev | Finding |
|---|---|---|
| P1 | **High** | **Authorization is essentially untested.** Across 93 test files / ~13k lines there are only a handful of negative-authz assertions (the probe tests, plus the agent-mutation tenancy test added with the multi-replica work). No test asserts that org A cannot reach org B's data, though `tests/factories.py` already builds multiple organizations. This is the root cause that let findings #1–#6 accumulate, and the highest-leverage thing to fix. |
| P2 | Medium | Quality gates are advisory (`continue-on-error: true`), hiding **3,015** ruff errors and 25 unformatted files behind a green check — and masking that basedpyright has not run at all (C8). |
| P3 | Medium | No SDL snapshot existed, so client-visible API changes were undetectable. `schema.graphql` is now generated (see *Artifacts*). |

## Documentation drift

Docs were last touched by the probe commit (`ffddaf0`) and are more current than expected.

| # | Sev | Finding | Location |
|---|---|---|---|
| D1 | **High** | **`authentikate.audience` is required with no default and is undocumented.** CONFIG.md's table lists `issuers` as required but omits `audience`; the "Minimal example" omits it too. Following the documented minimal config yields a `ValidationError` on first boot. | `CONFIG.md:140-146,226+` |
| D2 | **High** | CONFIG.md documents `debug` as defaulting to `false` with "Never enable in production", and `hosts` as configurable. Neither is read; both are hardcoded. The config reference describes a safe default the code does not implement. | `CONFIG.md:102-103` vs `settings.py:28,30` |
| D3 | Medium | **`Reservation` is documented as a core subsystem across three docs but does not exist** — no model, migration, mutation, or enum. Includes an ER-diagram entry, a strategy enum, and a `Task.reservation` field. | `domain-model.md:105-124`, `task-lifecycle.md:179-185`, `design/README.md:117` |
| D4 | Medium | The agent→server message catalogue names the **wrong direction**: eight entries are ToAgent mirror classes, five more (`DoneEvent`, `ErrorEvent`, `StatePatchEvent`, …) do not exist, and four real messages are undocumented. | `agent-protocol.md:261-276` |
| D5 | Medium | Task event-kind names are wrong across five docs: `DONE`, `ERROR`, `INTERUPTED`, `CANCELING`, `STEP` vs actual `COMPLETED`, `FAILED`, `INTERRUPTED`, `CANCELLING` (and no `STEP`). | `task-lifecycle.md`, `caller-protocol.md`, `realtime.md`, `higher-order.md`, `domain-model.md` |
| D6 | Low | Four config keys shipped in this rc are undocumented: `task_retention`, `probe_ttl`, `probe_linger`, `probe_max_inflight`. | `CONFIG.md:165-174` |
| D7 | Low | `identity.md:104-107` claims `is_active` uses a 5-minute window; the code uses `AGENT_STALE_AFTER` = 30s. `:86` omits `lease_epoch` and `active_session_id` — the fencing fields the protocol doc's model rests on. | `identity.md` |
| D8 | Low | `provenance.md:62-64` claims the regular assign path does not populate `Task.root` — false (`backend.py:329-333,352`). `task-lifecycle.md:200` miscredits the backfill to migration `0015`, which is a retention index. | `provenance.md`, `task-lifecycle.md` |
| D9 | Low | `realtime.md` names all three channel payloads wrongly and omits two channels. Root `README.md:37-39` still claims RabbitMQ is the default broker; transport is Redis + Channels. | `realtime.md:37-39`, `README.md` |
| D10 | Low | Probes mint real provenance tokens (`facade/probes/backend.py:95`, always a provenance root) but `provenance.md` never mentions them — precisely the edge case that doc exists to specify. | `provenance.md` |
| D11 | Low | Public SDL description references a `call` mutation that does not exist (it is `probe`); same stale vocabulary in the `probe_linger` config description. Input types were renamed (`AgentCallInputModel`→`AgentProbeInputModel`) with nothing to surface the break. | `rekuest_core/inputs/*`, `rekuest/configuration.py:78` |

---

## Confirmed clean

Checked and found sound — worth recording so the next audit does not redo them:

- **JSONPath / SQL injection.** `facade/descriptors.py:20` whitelists keys to
  `^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$` and raises otherwise; values always go through `json.dumps`.
  At read time `facade/managers.py:114` interpolates `compiled_jsonpath` as a *column reference*,
  never a value, and every user value uses bound parameters.
- **Agent identity spoofing.** `default_authenticator` derives the Agent from token-derived
  `(client, user, organization)`; nothing caller-supplied selects identity.
- **Lease fencing.** Gate order and the compare-and-set on `(id, lease_epoch)` are correct, with a
  `select_for_update` claim and a losing-heartbeat close path.
- **HookAgent HMAC intake.** `hmac.compare_digest` over the raw body, fails closed on missing
  secret or signature. (It does route into the unauthorized report path of #2.)
- **Probe guards via GraphQL** — the best-guarded surface in the codebase: org check on read,
  caller check on watch and control, `allow_probe` enforcement, per-caller inflight cap.
- **STS credential issuance** — scoped per-object session policies, clamped duration,
  server-generated keys, unscoped fallback refused by default.
- **Schema-wide authentication** — fails closed; nothing is anonymously reachable.
- **Migrations** — linear `0001 → 0016`, no duplicate numbers, no irreversible `RunPython`.
- **Probe terminal claiming** uses `HSETNX` — atomic single-winner, and notably *stronger* than the
  task path's equivalent (C1).

## Deliberate non-findings

- **75 ruff `F821` "undefined name" errors in `facade/types/*.py` are false positives** —
  strawberry forward references (`user: "User"`) resolved at schema-build time under
  `from __future__ import annotations`.
- **`facade/mutations/probe.py` is not unscoped** — an early grep suggested it was; the scoping
  lives in `facade/probes/backend.py` (ORG + CALLER).
- **`datalayer/mutations/{bigfile,parquet,zarr}.py` are unscoped but unreachable** — not registered
  in `facade/schema.py`. Only `media.py` is exposed.
- **`facade/backend.py`'s sync ORM/redis at the agent layer is by design**, not an event-loop bug.
- **Probes are documented** (`caller-protocol.md`, `task-lifecycle.md`, `domain-model.md`); the gap
  is per-doc coverage (D10), not absence.
- **The agent-root-task removal is correctly reflected** in the docs. No drift.

## Recommended order of work

1. **Write the cross-tenant test harness first** (P1). Two orgs, one asserting denial per resolver
   family. Without it, fixes to #1–#6 cannot be verified and will regress.
2. Fix #3 and #4 — both are small, and #4 destroys data on an ordinary deploy.
3. Add ownership checks to the agent report path (#2) and the media mutations (#5).
4. Make `DEBUG` config-driven (#8); rotate the committed keys (#9).
5. Work through the unscoped surface (#1) family by family, against the harness from step 1.
6. Fix #7, then make the gates non-advisory (P2) and commit `schema.graphql` as a guard (P3).

## Artifacts

All three are untracked additions in the working tree; nothing was committed.

- `AUDIT-v2.3.0-rc.1.md` — this report.
- `schema.graphql` — 4,931-line SDL baseline, newly generated. Committing this gives the repo its
  missing guard against unintended client-visible API changes, and was itself the evidence for #5.
- `tests/graphql/test_cross_tenant_isolation.py` — 1 passing control + 2 failing isolation
  assertions. This is the first brick of P1: it currently fails *by design*, and is the check that
  makes fixes to #1 verifiable.

## Coverage

Swept: `facade/` (runtime, models, mutations, queries, subscriptions, probes, lifecycle),
`rekuest/` settings and config, `rekuest_core/`, `datalayer/`, `config.yaml`, `run.sh`, migrations,
CI workflows, `docs/design/*`, `CONFIG.md`, and `tests/` as coverage evidence.

Not swept: migration internals beyond ordering, the test suite's own code quality, performance and
load behavior, dependency CVEs, and Docker/deployment hardening.

## How each finding was verified

Stated plainly, because "do not promote" deserves to be auditable itself:

- **Executed against a live stack** — #1 (cross-tenant reads, via the new test above; the
  neighbouring `tests/graphql/test_task_filter.py` passes 23/23, confirming the environment).
- **Executed as targeted runtime probes** — #7 (`FieldError` proven by Django field resolution with
  no DB attached), #5 (missing `@auth` read from the generated SDL), #8 (static tokens loaded via
  env with the tripwire silent), C6 (`hasattr` check), C12 (`get_commands()` check),
  D1 (`AuthentikateSettings.model_fields` required-field introspection).
- **Verified by reading the code path, not by executing an exploit** — #2, #3, #4, #6, #10.
  The authorization *gaps* are confirmed: `on_agent_done` provably ignores `agent_id`,
  `cleanup_actions` provably drops the org filter. The end-to-end *exploit* — that a forged
  `Completed` reaches the caller's subscription as genuine — follows from the code but was not
  executed. Treat these as confirmed defects with inferred blast radius.
- **Not run: the full test suite.** Docker is available and individual suites pass, but a complete
  `pytest` run was not part of this audit. A green suite would not invalidate any finding above —
  no test covers these paths, which is finding P1.

One correction to the record: during the audit a background analysis agent modified the working
tree (deleting `facade/migrations/0002`–`0016` and editing `0001_initial`) while probing the Django
runtime. This was detected and reverted; `git status` now matches `HEAD` for every tracked file.
The only working-tree additions are the three artifacts listed above.

---

# Remediation status

Full suite at the time of this audit: **445 passed, 0 failed**. (Now 721, after the stuck-task and
multi-replica hardening added `tests/agent/test_pickup_watchdog.py`, `test_multi_replica.py` and
`tests/models/test_constraint_migrations.py`.)

## Fixed

| # | Finding | Fix |
|---|---|---|
| 1 | Tenant isolation absent | `get_queryset` scoping added to 13 types (`State`, `Patch`, `Snapshot`, `Session`, `HardwareRecord`, `Space`, `Placement`, `Collection`, `Toolbox`, `Shortcut`, `TestCase`, `TestResult`, `Resolution`); new `scoped_get()` helper applied to 9 single-object root resolvers that bypassed type scoping. |
| 2 | Any agent could report on any task | New `ModelPersistBackend._agent_task()` resolves every reported task with an `agent_id` predicate; all 10 report handlers routed through it. Two regression tests added. |
| 3 | `cleanupActions` deleted across all orgs | Organization filter now applied unconditionally, not only in the `action_ids` branch. |
| 4 | Re-registration destroyed task history | `Task.implementation` → `SET_NULL` (migration `0017`), and the reap now keeps implementations with non-terminal tasks. |
| 5 | Media mutations unauthenticated/unowned | Registered through the local `mutation()` wrapper so `@auth` applies (verified in the SDL); `DatalayerStore` gained `organization` + `creator` (migration `datalayer/0002`), stamped on upload and enforced by `_owned_store()`. Fails closed for legacy unowned rows. |
| 6 | Agent/task control accepted any ID | `_agent_in_org()` scopes `bounce`/`block`/`unblock`/`kick`; `_request_control` refuses tasks outside the caller's organization. |
| 8 | `DEBUG` hardcoded | `DEBUG` and `ALLOWED_HOSTS` now read from config (`debug` already defaulted to `False`), restoring authentikate's static-token guard. |
| C5 | `Session` had no unique constraint | `UniqueConstraint(agent, session_id)` (migration `0008`), preceded by a dedupe that re-points the losers' `Patch`/`Snapshot` rows onto the surviving session rather than truncating the log. `tests/models/test_constraint_migrations.py::TestSessionDedupe`. |
| C6 | `reinit` was dead | Removed with the rest of the dead `lifeline` surface (see the Removals table). |
| C1 | Unlocked terminal task transitions | Every agent-reported terminal (`_finalize_from_agent`) and every sweep transition goes through the row-locked `_claim_task_transition_sync`, which now writes the `TaskEvent` in the same transaction. `tests/agent/test_pickup_watchdog.py::TestTerminalReportsAreExactlyOnce`. |
| C2 | `{agent_id}_processing` never drained | `AgentQueue.recover` — the lease holder returns popped-but-unacked frames to the queue before draining; the drain loop survives queue failures and stops on displacement. `tests/agent/test_delivery.py::TestAtLeastOnceDelivery`. |
| C3 | `TaskEventKind` missing `UNASSIGN` | Added, matching `TaskEventChoices`. |
| C8 | basedpyright could not start | Removed the deleted `facade/capabilities.py` from `include`; added the four `facade/probes/*` modules. It now runs (18 pre-existing errors newly visible). |
| C11 | `uv.lock` stale after every release | Release workflow runs `uv lock` before semantic-release commits. |
| C12 | `run.sh` called a nonexistent command | Dead `ensureadmin` step removed; `set -euo pipefail` added so future startup failures are loud. |
| P1 | Authorization untested | `tests/graphql/test_cross_tenant_isolation.py`, `tests/datalayer/test_media_ownership.py`, and two cross-agent regression tests in `tests/agent/test_events.py`. |
| P3 | No SDL baseline | `schema.graphql` generated and kept current. |

## Two test-fixture bugs found while fixing

Both were latent, and both had been *hiding* the vulnerabilities:

- `tests/factories.py::_build_task` documented that "the persist backend looks tasks up by id
  (not by the registered agent)" and built tasks under a throwaway agent while the test reported
  them over a different agent's socket. It now takes `agent_pk`.
- `tests/conftest.py::authenticated_context` hardcoded `slug="test-organization"` while
  `seed_agent` derived the organization from the static token — so caller and agent sat in
  different tenants in every socket test. It now derives its identity from the same token.
  Consequently the two probe ownership tests, which had relied on that mismatch for their
  "foreign" caller, now build one explicitly from the `test2` identity.

## Resolved after the decisions

**State history (#7) — rebuilt on `global_rev`.** `Patch`/`Snapshot` store one revision column,
which `facade.logic.get_latest_state` treats as the revision *after* a patch applies. Everything
now derives from it (`future == global_rev`, `current == global_rev - 1`), and the resolvers
filter on it directly. `tests/graphql/test_state_history.py` exercises all seven root queries and
asserts real reconstruction (snapshot at rev 1 + patches to rev 3 → `{"count": 2}`).

A **second latent bug** surfaced once the queries could run: they filtered `session_id=`, which is
the *FK integer*, while the API argument is `Session.session_id`, the string — every session-scoped
call raised `Field 'id' expected a number`. Now `session__session_id=`, and the GraphQL
`sessionId` field returns the string it accepts.

**Tenancy (untenanted models) — organization added to all of them.** `Blok`, `Dashboard`,
`ThreeDModel`, `MemoryShelve`, `StateDefinition`, `Protocol` and `UICatalog` gained a **required**
`organization`; `MaterializedBlok` and `MemoryDrawer` derive theirs from their parent rather than
denormalizing. Every creation path stamps it, and `infer_protocols` threads the owning
organization through. Migrations were squashed to a single `0001_initial` per app.

Two things this exposed:

- `create_blok` / `create_toolbox` keyed `update_or_create` on `name` alone — a global upsert, so
  any user could overwrite another organization's blok by name. `organization` is now part of the
  lookup key, closing it.
- Adding the column turned `update_dashboard`'s `dashboard.organization = input.organization`
  from an inert assignment on a non-field into a **live** tenancy hole. Ownership reassignment is
  removed and the field dropped from both dashboard inputs.

## Open — operational

- **#9, committed secrets.** The provenance Ed25519 private key, Django `SECRET_KEY` and Postgres
  password in `config.yaml` must be rotated and moved out of the repo. Code cannot do this, and
  rotating the signing key invalidates previously issued attestations.
- **P2, advisory gates.** Left advisory deliberately: ruff still reports ~3,000 findings (mostly
  missing docstrings and annotations), so making it required would block every PR. basedpyright is
  now *functional* and down to 18 errors, and is the realistic first gate to enforce.

## Removals (done)

All eight candidates removed. Suite green at **449 passed**; the SDL shrank by 19 lines.

| Removed | Why |
|---|---|
| `stateAtLocalRev` query | Identical to `stateAtGlobalRev` — one revision column means "local" and "global" cannot differ. `_get_state_at_revision` lost its now-meaningless `use_global_revision` parameter. |
| `Patch.currentRevision` / `futureRevision`, `Snapshot.revision` | Exact duplicates of the `global*` fields, same reason. |
| `reinit` mutation + `facade/mutations/lifeline.py` + `ReInitInput` | Dead: `persist_backend` has no `on_reinit`, so every call raised `AttributeError`. |
| `permission_classes` parameter | Accepted by `field()`/`mutation()` and silently discarded; zero call sites. |
| `datalayer/mutations/{bigfile,parquet,zarr}.py` | Never registered in the schema — unreachable. |
| `request_general_media_access` | Unregistered, and its grant is bucket-wide (`# TODO: FIX ORGANIZATION SCOPED MEDIA GRANTS`). |
| `newActions(cage:)` argument | Accepted and ignored entirely. |
| `facade/infererence/` → `facade/inference/` | Misspelled package name (`git mv`, so history is preserved). |

**Client-visible SDL changes**, confirmed by diffing the regenerated schema:

```
- stateAtLocalRev(...)      - reinit(...)          - input ReInitInput
- Patch.currentRevision     - Patch.futureRevision - Snapshot.revision
- newActions(cage:)  ->  newActions          (field kept, argument dropped)
```

The datalayer removals produced no SDL change: those resolvers were never registered, so they had
never been part of the public schema. The `Datalayer` class methods and store models they used
remain — `BigFileStore`/`ZarrStore`/`ParquetStore` are still referenced from the models layer, so
only the unreachable GraphQL layer was deleted.

## Corrections to the audit

- **C7 (`force_script_name`) was wrong.** `MY_SCRIPT_NAME` is not dead — kante's `dynamicpath`
  reads it, and setting Django's `FORCE_SCRIPT_NAME` too would prefix every URL twice. Only
  CONFIG.md's wording is misleading. No code change.
- **`update_dashboard` was not a live tenancy hole at audit time** — `Dashboard` had no
  `organization` field, so the assignment did nothing. It became real only when the column was
  added, and was fixed in the same change.
- **basedpyright's `probes/store.py` errors are redis-py stub noise** (sync vs async client), and
  `probes/backend.py:163` is a pydantic-default false positive. Neither is a defect.
