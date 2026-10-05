# Releasing rekuest-server

This repository releases **two Docker images under one version**, not a PyPI
package:

| Image | Built from | What it is |
| --- | --- | --- |
| `jhnnsrs/rekuest` | the repository root (`Dockerfile`) | the rekuest server: GraphQL, migrations, the upkeep jobs takt asks for |
| `jhnnsrs/rekuest-takt` | `takt/` (`takt/Dockerfile`) | takt: the agent protocol and every sweep |

Every release pushes both images with the same set of tags. takt writes the
schema the server of the same release migrates, so **always run the same
version of both**.

Versioning is automated by [python-semantic-release][psr] from
[Conventional Commits][cc] — you never bump the version by hand. A push to a
release branch runs `.github/workflows/release.yaml`, which:

1. runs the server's test suite (`uv run pytest`),
2. computes the next version from the commit history, tags `vX.Y.Z`, and cuts
   a GitHub Release whose notes name both images. The release is tag-only:
   nothing is committed to the branch, so `pyproject.toml` and `CHANGELOG.md`
   are not touched,
3. builds both images at the new tag and pushes them under the semver
   multi-tag regime.

A commit that only touches `takt/` releases like any other: the version is
the repository's, and both images are rebuilt.

The pair is tested on every push and pull request to `main` and `next` by
`.github/workflows/takt.yaml`:

- the Rust workspace (`cargo fmt`, `cargo clippy`, `cargo test`) against a
  database migrated by the checkout's server (`takt/scripts/test-db.sh`),
- a contract job that regenerates takt's fixtures from the server's models
  and fails if they differ from the committed ones,
- the conformance suite (`takt/conformance`, `uv run pytest`) against both
  images built from the checkout.

## Commit messages drive the version

| Commit prefix | Bump | Example |
| --- | --- | --- |
| `fix:` | patch | `fix: handle empty agent queue` |
| `feat:` | minor | `feat: add a pickup deadline for undelivered tasks` |
| `feat!:` / `BREAKING CHANGE:` footer | **major** | `feat!: new agent protocol` |

Commits that aren't releasable (`chore:`, `docs:`, `refactor:` …) don't trigger
a release on their own.

### A changed config key is a breaking change

A hub's files are written by an installer for one **major** of this service, and a hub follows
that major's tag (`jhnnsrs/rekuest:6`). So within a major, a config written for its first
release has to keep working:

- **Adding** a key with a default that keeps the old behaviour is a `feat:`.
- **Renaming** a key within a major keeps the old name read (`validation_alias=AliasChoices(new,
  old)`, as `takt_url` / `agentd_url`; takt's `configuration.rs` reads the same file and needs
  the same). `validate_settings` and a boot-time warning report the old name without
  failing, and the next major drops it.
- **Removing** a key, changing what one means, or needing a new one to work at all is a
  `feat!:` with a `BREAKING CHANGE:` footer that **names the keys** — the footer is what the
  release notes carry, and what whoever writes the installer's next layout reads.

What takt needs from its container (`TAKT_INTERNAL_BIND`, a mounted socket) counts as config.

## Branches

| Branch | Releases | Docker tags |
| --- | --- | --- |
| `main` | stable `X.Y.Z` | `X.Y.Z`, `X.Y`, `X`, `latest` |
| `next` | prereleases `X.Y.Z-rc.N` | `X.Y.Z-rc.N` (no moving tags) + moving `:next` |
| `N.x` (e.g. `1.x`) | maintenance `X.Y.Z` | `X.Y.Z`, `X.Y`, `X` (**no** `latest`) |

The tags apply to both images. A release from `next` also moves the `:next`
tag of both, which the `deployments/next` staging environment tracks. Only
`main` ever moves `latest`.

## Day-to-day

- **Patch/feature for the current line:** merge a `fix:`/`feat:` PR into `main`.
  PSR cuts the next stable release and deploys it to production.
- **Anything risky / breaking:** land it on `next` first. Each push cuts a fresh
  `…-rc.N` and updates the `:next` staging image so you can soak it. Promote by
  merging `next` → `main`.

## Working on a new major (v2)

```
next   feat!: …      -> 2.0.0-rc.1, 2.0.0-rc.2 …   (+ moving :next image -> staging)
              │ merge main into next regularly to keep the rc base correct
main   ──1.5.2──(merge next)──> 2.0.0 -> 2.0.1 …    (Docker: latest, 2, 2.0, 2.0.x)
          │ cut `1.x` from main HEAD *before* the 2.0.0 merge
1.x    ──1.5.2──> 1.5.3 -> 1.5.4 …                  (Docker: 1, 1.5, 1.5.x  — no latest)
```

1. **Develop v2 on `next`.** Land `feat!:` / `BREAKING CHANGE:` commits there.
   PSR cuts `2.0.0-rc.N` and the `:next` image auto-deploys to staging.
   Periodically merge `main` → `next` so the rc base stays at the latest v1.
2. **Cut the maintenance branch first.** Right before promoting, branch `1.x`
   from `main` HEAD (still at the last v1 commit):
   ```sh
   git checkout main && git pull
   git checkout -b 1.x && git push -u origin 1.x
   ```
3. **Promote v2.** Merge `next` → `main`. The breaking change since `1.5.2`
   makes PSR cut stable `2.0.0` → images `2.0.0`, `2.0`, `2`, `latest`.

## Backporting a fix to v1 (after v2 has shipped)

Branch off `1.x`, PR the fix into `1.x` with a `fix:` commit. PSR cuts `1.5.3`
and publishes `1.5.3`, `1.5`, `1` — `latest` stays on v2. Forward-port the same
fix to `main`/`next` if it also applies there.

## Deployment pinning

- **Pin both images to the same tag.** `jhnnsrs/rekuest:X` beside
  `jhnnsrs/rekuest-takt:X`, and pull them together: a moving tag pulled for
  one image only leaves takt on another release than the server.
- **Order on upgrade.** The server migrates on boot. takt waits at startup
  until the database has the migrations it was written against
  (`takt/schema-migrations.txt`), so bring up the new server first, or both
  at once.
- **Staging** (`deployments/next`) pins `:next` — it rides the rc work.
- **Stable production** should pin the **major** tag (`jhnnsrs/rekuest:1`), not
  `:latest`. It then receives every `1.x` patch automatically but never jumps a
  major on its own; adopting v2 is a deliberate re-pin to `:2`.



## Dry-running locally

`python-semantic-release` is in the dev group, so you can preview the version a
branch would cut without pushing anything:

```sh
uv run semantic-release version --print   # prints the next version, makes no changes
```

[psr]: https://python-semantic-release.readthedocs.io/
[cc]: https://www.conventionalcommits.org/
