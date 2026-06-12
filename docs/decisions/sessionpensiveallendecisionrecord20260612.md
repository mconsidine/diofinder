# Decision record — release-pipeline restructure across the eFinder repos

- **Date:** 2026-06-12T14:47Z (session spanned 2026-06-10 — 2026-06-12)
- **Session:** Claude Code session "pensive-allen", ID `session_01MBjXhx3TLxkk3WEkvHWqRX`
  (https://claude.ai/code/session_01MBjXhx3TLxkk3WEkvHWqRX), working branches `claude/pensive-allen-q5iis1`
- **Repositories affected:** astro_databases, diofinder, olive-solve, sycamore-extract, tetra3rs
  (plus user-applied changes to the cedar-solve fork)

## Changes in this repository (astro_databases)

**Role after this session:** catalog data + database generation + tagged-release publishing. The single
source of star data for the whole system.

- Restructured: source catalogs moved to `data/`; committed wheel artifacts (`databases-latest/`) and the
  800-line multi-repo `manual_build.yml` removed (`e86e707`).
- New `build-databases.yml`: a `v*` tag builds both databases and attaches them plus `manifest.json`
  (parameters, input/output SHA-256s, generator versions) to a GitHub release; manual dispatch builds an
  artifact without publishing.
- `generate_databases.py` fixed against the real cedar-solve API: `max_fov`/`min_fov` (the original
  `fov_range=`/`catalog=` parameters never existed), `Tetra3(load_database=None)`, `save_as` passed as a
  `pathlib.Path` (a `str` is treated as a name inside cedar-solve's package data dir) (`580a740`, `3cb8629`).
- cedar-solve installed via `uv pip install --override` to neutralize stale dependency ceilings
  (Pillow<9 etc.) while still resolving future upstream deps (`18c98ff`); the user later removed the
  ceilings in the cedar-solve fork itself and bumped its version 0.5.1 → 0.6.0.
- New `scripts/gaia_to_hip.py`: converts `gaia_hipp_merged.csv` into committed
  `data/gaia_hip_main.dat.gz` — hip_main pipe format that stock esa/tetra3, cedar-solve, and olive-solve
  parse unmodified. De-propagates positions 2016.0 → 1991.25 with Gaia PMs; writes 0.0 for the 912
  missing-PM rows (blank would drop 86 of the 704 brightest stars); synthetic row-number IDs (`995c384`).
- The Gaia catalog became the default `.npz` source: 63,154 stars kept at G ≤ 8.0 vs 41,394 (V ≤ 8.0)
  from hip_main; `--hip-catalog data/hip_main.dat.gz` keeps the Hipparcos path.
- tetra3rs database built from the mconsidine fork (`tetra3rs-ref` dispatch input / `TETRA3RS_REF` var;
  default `main` tracks upstream, so left alone the result equals a PyPI install) (`deee9a7`).
- Validated by three green dispatch runs; first release **v2026.06** published with
  `cedar_solve_13deg.npz` (13.3 MB), `tetra3rs_13deg.bin` (25.0 MB), `manifest.json`.
- *Adjacent post-session work (not part of this record):* a "deep" G ≤ 8.5 database variant
  (`--variant deep`, `*_mag85` outputs from a locally generated G ≤ 9.0 catalog) was added on main.

## Changes in this repository (diofinder)

**Role after this session:** pure consumer — pulls wheels and the star database from the source repos'
GitHub releases at image-build time; no binaries in git.

- `release.yml` ("Release image", renamed from "Release (sycamore)" `4af9364`; stale `sycamore-only`
  push trigger dropped `3b6489d`):
  - Fetches the tetra3 (olive-solve) and star_detect (sycamore-extract) aarch64 wheels from those
    repos' releases into gitignored `vendor/wheels/`; committed wheels and the Vendor Binaries /
    Vendor Sycamore workflows retired (`4c476c2`).
  - Downloads `cedar_solve_13deg.npz` from the astro_databases release and verifies its SHA-256
    against `manifest.json`; esa/tetra3 generation (from the Gaia-derived catalog) remains only as the
    custom-FOV fallback (`b54de4f`, `4c476c2`).
  - Pinning: `OLIVE_SOLVE_TAG` / `SYCAMORE_TAG` / `DB_TAG` repo variables, overridable per-run by the
    `olive_solve_tag` / `sycamore_tag` / `db_tag` dispatch inputs (`79923de`).
  - Wheel patterns accept abi3 wheels (`star_detect-*aarch64*.whl`) after the cp313-only pattern
    caused the first v0.0.24 image to build without the extractor (`b8c8f8a`).
- `efinder-update` (OTA): refreshes both wheels from the latest releases (interpreter-specific pattern
  first, abi3 fallback); a wheel placed in `vendor/wheels/` overrides the download; all wheel-refresh
  failures are non-fatal so an update cannot break a working install.
- New `efinder-db-update` device script: downloads `cedar_solve_13deg.npz` from an astro_databases
  release, verifies against the manifest, backs up and replaces the database named by `solver_db`,
  restarts the service (`b54de4f`).
- Image builds use the Gaia-derived catalog/database (broad G band matches the unfiltered IMX477
  response; Gaia complete where Hipparcos is not).
- Rebuilt **v0.0.24** verified: both wheels fetched (star_detect 0.11.2 abi3 + tetra3 0.1.0 abi3),
  database SHA-verified, in-image checks "olive-solve tetra3-py OK" and "sycamore star_detect OK".

## Changes in this repository (olive-solve)

**Role:** the solver that runs on the device — its `tetra3-py` wheel installs under the import name
`tetra3` and performs every live plate solve, loading the cedar-solve-generated `.npz` (its loader
implements both the cedar-solve 828-byte and standard tetra3 props schemas).

- New `release-wheels.yml` (this repo previously had no CI): a `v*` tag or dispatch builds the abi3
  aarch64 wheel (one wheel covers Python ≥ 3.8; Cortex-A53 tuning from the committed
  `.cargo/config.toml`) and attaches it to a GitHub release (`03a6d07`).
- Version conventions: `vX.Y.Z` = full release (becomes "latest"); `vX.Y.Z-suffix` = **prerelease**,
  excluded from "latest" so devices and default image builds never pick up branch/test wheels —
  consumers opt in via diofinder's `olive_solve_tag` input or `OLIVE_SOLVE_TAG` variable; `*-dev` =
  build-only (`172847a`, cherry-picked to `olive-solve-noext` as `ac57e4b` via PR #5).
- **User decision:** the deployed flavor is the `olive-solve-noext` branch (solver without the
  extraction pipelines — sycamore does extraction on-device). Release v0.1.1 (noext) is the fleet
  "latest"; division of labor mirrors cedar-detect (extract) / cedar-solve (solve).
- Branch cleanup: both `claude/*` branches verified fully content-present in `olive-solve-noext`
  and deleted. Note: `main` does not contain the noext-branch work; treat noext as the branch of
  record or reconcile main deliberately.

## Changes in this repository (sycamore-extract)

**Role:** the on-device extractor (star_detect). Its existing tag-triggered `build.yml` was already the
model the other repos copied; this session aligned it with the abi3 switch and the prerelease convention.

- Per-Python build matrix collapsed to a single abi3 build (the cp311/cp312/cp313 jobs produced the
  identical `cp38-abi3` wheel three times after the abi3 switch), and the release-body install snippet
  fixed to resolve the actual asset instead of a hardcoded cp313 filename (`9cd18f5`).
- Version bumped 0.11.1 → 0.11.2 to match the already-cut v0.11.2 tag, with a CHANGELOG entry
  documenting the abi3 switch and the wheel-version skew on the original release asset (`12ce8f9`).
- Prerelease convention added (same as olive-solve/tetra3rs): hyphenated versions publish as
  prereleases, `*-dev` builds only (`8c905c5`).
- The v0.11.2 release was re-run after merge: it now carries a single correctly-versioned
  `star_detect-0.11.2-cp38-abi3-…aarch64.whl` (stale 0.11.1-named asset deleted by the user).

## Changes in this repository (tetra3rs)

**Role:** experimental/secondary solver (fork of ssmichael1/tetra3rs). Its `.bin` databases are
published by astro_databases; the solver is not currently deployed on diofinder devices.

- New `pi-wheels.yml`: on a `v*` tag (alongside the PyPI publish), builds cp311–cp313 aarch64 wheels
  with `-C target-cpu=cortex-a53` on a native ARM runner and attaches them to the tag's GitHub
  release; hyphenated tags publish as prereleases (`559311a` + follow-ups).
- **PyPI publishing restricted to upstream** (`github.repository_owner == 'ssmichael1'` guard on the
  publish job): the fork distributes via GitHub release assets only — the owner is not the original
  author and does not want the fork on PyPI. Tags still run tests and wheel builds (`5ed44ba`).
- astro_databases now generates `tetra3rs_13deg.bin` from this fork (default `main` = upstream-
  equivalent; any branch selectable), so the database always matches the fork's code if it diverges.

## Session narrative and key decisions

1. **astro_databases assessed, then narrowed to its name** — the repo had ~52 MB of committed binaries,
   a single failed CI run, a workflow depending on a release and a sibling workflow that never existed,
   and a generation script written against an imagined cedar-solve API (`fov_range=`) that had never
   executed. Decision: data + generation + tagged releases only; the five foreign wheel-build jobs were
   dropped in favor of per-repo workflows.
2. **GitHub releases over going offline** — release assets don't count against repo quotas (2 GB/file)
   and Actions is free for public repos, so the contemplated local/'act' pipeline was unnecessary;
   'act' was specifically recommended against (token still required, runner-image drift, no benefit
   over a plain script).
3. **Tagged, immutable, manifest-verified releases as the vendoring contract** — every database release
   carries `manifest.json` (parameters, input/output SHA-256s, generator versions); consumers verify
   hashes at fetch time.
4. **One star list for everything** — the merged Gaia DR3 + Hipparcos catalog (63,491 stars to G ≈ 8.0)
   feeds both database formats. A ~100-line converter (`gaia_to_hip.py`) reformats it into hip_main
   layout so stock esa/tetra3, cedar-solve, and olive-solve parse it **unmodified** — upstream
   compatibility preserved with zero solver patches. Verified: identical pattern counts vs hip_main
   with substantially denser lattice fields (min field depth 26 → 35 stars), `.npz` cost only ~0.5 MB.
5. **Per-repo wheel workflows, not an aggregator repo** — build config lives next to the code it
   builds; the old aggregator's pathologies (cross-repo SHA bookkeeping, drifting build configs) are
   structurally avoided. diofinder pulls from three release URLs instead of one.
6. **diofinder pulls everything at build time** — no binaries in git; `vendor/wheels/` survives only as
   a gitignored staging dir so `install.sh`/chroot flow stayed untouched. OTA (`efinder-update`)
   refreshes wheels from latest releases, deliberately non-fatally.
7. **Branch-testing without fleet risk** — hyphenated tags (e.g. `v0.2.0-noext`) publish as
   prereleases everywhere, invisible to "latest"-following consumers; selected explicitly via dispatch
   inputs or repo variables.
8. **Identity clarifications** — three codebases answer to `tetra3`: cedar-solve (Python; generates the
   `.npz` in CI only), olive-solve (Rust; the wheel *named* tetra3 that does all on-device solving),
   and upstream esa/tetra3 (custom-FOV fallback only). cedar-detect and cedar-solve do not overlap:
   they are the extract/solve halves of the Cedar design, mirrored here by sycamore / olive-solve.

## Assessments performed

- Initial astro_databases audit (broken paths, phantom dependencies, dead API calls — all documented in
  commit messages).
- Catalog compatibility: committed `gaia_hipp_merged.bin` header (`GDR3` v1, 63,491 stars) matches the
  current tetra3rs parser; G-band depth exactly 8.01; converter output survives the exact hip_main
  parser logic 63,491/63,491.
- olive-solve loader verified to support the cedar-solve `.npz` schema (828-byte props branch) —
  basis for `efinder-db-update` on existing devices.
- Database A/B (hip vs Gaia): 41,394 → 63,154 stars kept; patterns ~950k in both (capped); lattice
  field density up ~20–30%; `cedar_solve_13deg.npz` 13.3 MB.
- Post-release audit of the first green diofinder image (v0.0.24, first build): found it had silently
  shipped **without** the sycamore extractor (abi3 wheel name vs cp313 fetch pattern) — fixed and
  rebuilt; the second v0.0.24 build verified complete.

## Outstanding recommendations

- **olive-solve `main` lags `olive-solve-noext`** (extractor feature-gate, rayon perf work, release
  workflow). Either declare noext the trunk or merge it into main before they drift further.
- **GitHub Actions Node 20 deprecation:** runners force Node 24 from 2026-06-16; the workflows use
  actions/checkout@v4, setup-python@v5, upload-artifact@v4 — bump majors when convenient.
- **cedar-solve fork hygiene:** the fork now carries relaxed dependency floors and version 0.6.0;
  re-apply/verify on future upstream syncs. The uv `--override` in build-databases.yml remains as a
  safety net.
- **tetra3rs `.bin` format coupling:** the database format is tied to the generating tetra3rs version
  (recorded in the manifest). Regenerate + retag astro_databases when upgrading tetra3rs on a device;
  use `TETRA3RS_REF` if the fork diverges.
- **Derived-file discipline:** rerun `scripts/gaia_to_hip.py` whenever `gaia_hipp_merged.csv` changes
  (deterministic, byte-identical output for unchanged input).
- **Existing devices** get the new database via `sudo efinder-db-update [tag]` (after an OTA update
  delivers the script) or the documented curl one-liner against
  `releases/latest/download/cedar_solve_13deg.npz`.
