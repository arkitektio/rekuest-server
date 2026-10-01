# Provenance: the attestation token

> Code references are to agentd's `facade` crate, `agentd/crates/facade/src/`, unless a path
> says otherwise. agentd mints the tokens; the rekuest server publishes the verifying key.

Rekuest is the broker in the loop at the start of **every** assignment and sub-assignment. That
position lets it act as a second authority, orthogonal to the one that authorises writes: the
**provenance authority**. When a non-trivial assignment is dispatched, Rekuest mints a short,
signed JWT that attests *who caused the work* and *with which inputs*, and hands it to the executing
agent alongside the cleartext args. Any downstream service holding Rekuest's public key can record a
**verified, immutable provenance fact** — offline, without ever calling back into Rekuest.

This document explains the trust model, the lineage invariant, and the token vocabulary. It covers
the **issuing** side only (what Rekuest emits). Verification, single-use enforcement, actor-binding
and the provenance store live downstream (in Mikro / koherent) and are deliberately out of scope.

> Read [identity.md](../../docs/design/identity.md) first — the token is built almost entirely from the
> `(client, user, organization)` triple and the `Task` lineage described there.

## Why a separate token

The auth JWT that gates writes answers *"is this request allowed?"*. It is an **authorization**
artifact: short-lived, audience = the resource server, discarded once the request is served.

The provenance token answers a different question — *"who is accountable for this artifact, forever?"*
It is an **attestation**: it is meant to be **recorded**, never to grant access. Because the two have
different lifetimes, different audiences and different failure modes, they are different tokens, from
different issuers, verified against different keys.

| | Auth token (lok) | Provenance token (rekuest) |
| --- | --- | --- |
| Question | may this request write? | who caused this, with what inputs? |
| Role | authorization (a grant) | attestation (a record) |
| Issuer | the identity provider (`lok`) | Rekuest itself |
| Signature | RSA | Ed25519 |
| Lifetime | short (until the request completes) | longer; revoked via `jti`, not tight `exp` |
| Consumed by | Rekuest (to admit the request) | downstream stores (to record provenance) |

## Trust model

- **Rekuest is the single logical issuer per trust domain** — conceptually the Transaction-Token
  Service role of `draft-ietf-oauth-transaction-tokens`. Being the broker at every hop, it mints a
  correctly-scoped token at each assignment and **inherits lineage from the parent**, so no
  holder-side attenuation is needed.
- **The agent is a conduit.** It forwards the token opaque to downstream services; it never
  validates it, and it never receives a signing or verifying key. Rekuest's private key never leaves
  Rekuest.
- **Residual trust, acknowledged.** The signature bounds *forgery* (no invented assignments, no
  tampered claims), not agent honesty. Binding the misattribution blast radius — args bound by hash,
  a unique `jti` — is the issuer's contribution; enforcement is the verifier's.

## Conformance

- JWS over **Ed25519** (RFC 9864 fully-specified `alg`, replacing the deprecated `EdDSA`) — asymmetric, verifiable offline against the published JWKS.
- Follows RFC 9700 (OAuth 2.0 Security BCP) and RFC 8725 (JWT BCP): `alg` is pinned (never `none`),
  `exp` is always bound, `aud` is always present and always a **list** (never a wildcard), every JWS
  header carries a `kid`, and every token carries a unique `jti`.
- The claims map cleanly onto W3C **PROV-O** (Entity / Activity / Agent) for downstream recording.

## The lineage invariant: the root is always a human

This is the guarantee the whole scheme leans on: **every artifact, however deep in a causal tree,
traces back to an accountable human at the root.**

Rekuest enforces it at mint time, using its broker position and the existing `Task.parent`
lineage (the root is found by **walking the `parent` chain** to the task that has no parent,
`resolve_root` in `provenance/mint.rs`):

- **Top-level assignment** (no parent): the initiator must be a human. Rekuest classifies the
  current request principal from its roles (see *Human classification* below). The token sets
  `rcb = sub = <the initiating human>`, and `rtk = tsk` (the root is itself).
- **Sub-assignment** (has a parent): Rekuest walks to the root and **inherits** the root human and
  root id from it — `rcb` and `rtk` are copied from the root task's caller, never recomputed.
  `sub` is whatever caused *this* hop (which may be an agent or service); `ptk` is the immediate
  parent.
- **Refusal.** If the root principal cannot be confirmed human, a tree with a non-human root is a
  provenance error: under a **strict** policy Rekuest raises and the mint fails; under the default
  **lenient** policy it skips minting (emits no token) and logs — it never emits a token that falsely
  asserts a human root.

### Human classification

The auth token has no built-in human/service flag, so "is this a human?" is a **configurable
predicate over roles**, read from whichever source is available at mint time:

- the **live auth token** roles for the principal making the current request, or
- the persisted **`Membership.roles`** (the roles a user holds in an organization, captured at login)
  when only a stored `Caller` is available — i.e. when classifying the root of a sub-assignment.

`provenance.human_roles` (in `config.yaml`) lists the role(s) that mark an accountable human. If
it is **empty** (the default), enforcement is off and every principal is treated as human — the
invariant is opt-in, so dispatch keeps working until an operator declares the policy.
`provenance.strict` then chooses between refuse-and-raise and skip-and-log.

## Token vocabulary

One token is minted per assignment (not per output). It stays **flat and fixed-size** — short
claims, two id pointers (parent + root) rather than the full chain, and a 32-byte args hash rather
than inline args — so it never approaches the ~8 KB `Authorization` / gRPC-metadata ceilings, and the
only way it could grow is if a claim started scaling with payload or tree depth. None does.

Standard **RFC-registered** claims keep their canonical names for interoperability. Rekuest's **own**
claims use compact three-letter symbols.

### Registered claims (standard names)

| Claim | Meaning | Source |
| --- | --- | --- |
| `iss` | the provenance issuer id (`provenance.issuer`, e.g. `rekuest`) | RFC 7519 |
| `aud` | **list** of target services the token is scoped to (never a wildcard) | RFC 7519 |
| `sub` | the **immediate** causer of *this* hop (the request principal) | RFC 7519 |
| `act` | the **actor** the token is issued to — the executing agent (see below) | RFC 8693 |
| `iat` | issued-at (unix seconds) | RFC 7519 |
| `exp` | expiry (`iat + provenance.token_ttl_seconds`) | RFC 7519 |
| `jti` | unique per token; the verifier enforces single-use | RFC 7519 |

`act` is an object: `act.sub` is the executing agent's user sub, `act.cid` is the agent's OAuth
`client_id`. The verifier binds these against the agent's own auth-token subject — that binding check
is the verifier's job, not Rekuest's.

### Rekuest provenance claims (compact symbols)

| Symbol | Expands to | Meaning |
| --- | --- | --- |
| `tsk` | task | this task id |
| `ptk` | parent task | immediate parent task id (`null` if this is the root) |
| `rtk` | root task | root task id of the whole tree (`== tsk` when this is the root) |
| `rcb` | root caused by | the **human** principal at the root of the tree (invariant: always human) |
| `ahs` | args hash | SHA-256 of the canonicalized args |
| `aha` | args hash alg | the canonicalization algorithm/version, so a verifier can recompute `ahs` |

**`sub` vs `rcb`.** For a top-level assignment they coincide (both the initiating human). For a
sub-assignment, `sub` is whatever caused this hop (possibly an agent/service) while `rcb` still points
at the human who started the tree. `sub` is the *immediate* cause; `rcb` is the *ultimately
accountable* party.

**`ptk` + `rtk`, not the full chain.** The token carries only the two endpoints — immediate parent
and tree root. The complete lineage is reconstructed downstream from recorded facts, so token size is
independent of tree depth.

**`ahs` + `aha`, not inline args.** The args travel in the clear alongside the token (the agent needs
them to execute); the hash binds them to the signature. `aha` (currently `sha256-canonical-v1`) names
the canonical form so any verifier reproduces the exact bytes before hashing — see *Canonicalization*.

### Example payload

```jsonc
{
  // registered
  "iss": "rekuest",
  "aud": ["mikro"],
  "sub": "user-42",                 // the human who clicked "run"
  "act": { "sub": "agent-7", "cid": "imagej-app" },
  "iat": 1750000000,
  "exp": 1750003600,
  "jti": "f1c2…",
  // rekuest provenance
  "tsk": "9b1a…",                   // this task
  "ptk": null,                      // no parent → this is the root
  "rtk": "9b1a…",                   // root == self
  "rcb": "user-42",                 // accountable human at the root
  "ahs": "e3b0c44298fc1c14…",       // sha256 of canonicalized args
  "aha": "sha256-canonical-v1"
}
```

## Canonicalization (a versioned contract)

`ahs` is the SHA-256 of a **canonical** byte encoding of the args, defined in
`provenance/canonical.rs`. The encoding is a **versioned contract**: a verifier must reproduce
it exactly to recompute the hash, so any change is breaking and bumps `CANONICALIZATION_VERSION`
(reflected in `aha`).

- **v1:** the bytes Python's `json.dumps` writes with sorted keys, no insignificant whitespace
  (`separators=(",", ":")`) and non-ASCII left as UTF-8 (floats as `repr(float)`), then SHA-256
  of the UTF-8 bytes, hex-encoded.

## Audience resolution

`aud` is scoped to the actual downstream service(s) and resolved **once, at implementation
registration time** (not at dispatch) — it is a property of the implementation, not of an individual
assignment:

1. **Declared** — `Implementation.provenance_audience`, set explicitly at registration, when present.
2. **Derived** — otherwise, from the implementation's action **ports**: the structure identifiers on
   its arg + return ports (e.g. `@mikro/image` → `mikro`), walked recursively through nested ports.

The resolved set is persisted on `Implementation.provenance_audience`, so dispatch simply reads it —
it never recomputes the audience. There is **no default audience**: an implementation whose ports
reference no external structure gets an empty `aud` (present, per RFC 8725, but empty).

## Keys, signing and the JWKS endpoint

- Rekuest holds one **Ed25519 key**, the instance key: `instance.private_key` in `config.yaml`
  (PKCS#8 PEM). The server and agentd read the same file, so they hold the same key. It is
  required: agentd refuses to start without the `instance` block, and the server's configuration
  fails validation. The private key never leaves Rekuest and is never sent to agents.
- agentd signs tokens with `alg=Ed25519`; the `kid` in the JWS header is the key's RFC 7638
  thumbprint (`provenance/keys.rs`).
- The rekuest server publishes the verifying key at **`/.well-known/jwks.json`** as a standard
  JWKS document, served with a `Cache-Control: public` header so verifiers fetch-and-cache and
  verify **offline** — never a synchronous per-use callback into Rekuest.
- **Rotation** (not yet implemented) is designed for: publish overlapping keys and keep a retired
  public key in the JWKS until every token signed under its `kid` has expired.

The same key signs the service tokens of the internal API and of requests to the hub's services.

## Where this lives in the code

| Concern | Location |
| --- | --- |
| Key, signing, thumbprint | `provenance/keys.rs` |
| Canonical args hash | `provenance/canonical.rs` |
| Human classification | `provenance/principal.rs` |
| Audience derivation (registration) | `provenance/audience.rs` |
| Claim builder + signer | `provenance/mint.rs` (`mint_token_for_task`) |
| Mint at dispatch | `backend.rs` (`assign_with_status`, `assign_higher_order`); a redelivered Assign gets a fresh token (`persist/reconcile.rs`, `redispatch_token`) |
| Verifying this rekuest's own token (a signal's causing task) | `provenance/verify.rs` |
| Token on the wire | `Assign.token` (the `rekuest-protocol` crate) |
| Registration fields | `Implementation.needs_token`, `Implementation.provenance_audience` |
| JWKS endpoint | the server: `rekuest/urls.py` (`/.well-known/jwks.json`), `facade/provenance/keys.py` |
| Config | `config.yaml`: the `provenance:` block (policy) and the `instance:` block (the key) |

## Out of scope (downstream — Mikro / koherent)

Signature/JWKS verification, `aud` membership checks, the actor-binding check (`act.sub` vs the
agent's auth subject), `ahs` recomputation, single-use `jti` enforcement, the provenance store, and
full-chain reconstruction from the `ptk`/`rtk` pointers. Rekuest's job ends at emitting a correct,
signed, conformant claim set and publishing the keys to verify it.
