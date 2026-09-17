# Releasing rekuest-server

`rekuest-server` ships as Docker images (`jhnnsrs/rekuest`), not a PyPI package.
Versioning is automated by [python-semantic-release][psr] from
[Conventional Commits][cc] — you never bump the version by hand. A push to a
release branch runs `.github/workflows/release.yaml`, which:

1. runs the test suite,
2. computes the next version from the commit history, bumps `pyproject.toml`,
   updates `CHANGELOG.md`, tags `vX.Y.Z`, and cuts a GitHub Release,
3. builds the image at the new tag and pushes it under the semver multi-tag
   regime.

## Commit messages drive the version

| Commit prefix | Bump | Example |
| --- | --- | --- |
| `fix:` | patch | `fix: handle empty agent queue` |
| `feat:` | minor | `feat: add a pickup deadline for undelivered tasks` |
| `feat!:` / `BREAKING CHANGE:` footer | **major** | `feat!: new agent protocol` |

Commits that aren't releasable (`chore:`, `docs:`, `refactor:` …) don't trigger
a release on their own.

## Branches

| Branch | Releases | Docker tags |
| --- | --- | --- |
| `main` | stable `X.Y.Z` | `X.Y.Z`, `X.Y`, `X`, `latest` |
| `next` | prereleases `X.Y.Z-rc.N` | `X.Y.Z-rc.N` (no moving tags) + moving `:next` |
| `N.x` (e.g. `1.x`) | maintenance `X.Y.Z` | `X.Y.Z`, `X.Y`, `X` (**no** `latest`) |

`next` also publishes the moving `:next` image (`docker-next.yaml`) that the
`deployments/next` staging environment tracks. Only `main` ever moves `latest`.

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

- **Staging** (`deployments/next`) pins `:next` — it rides the rc work. No change.
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
