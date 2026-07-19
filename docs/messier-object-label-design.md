# "Centered object" label — name the Messier DSO the aim point is on

Design spec for a **display-only** label that names the bright deep-sky object
(Messier) the aim point currently sits on, mirroring the existing "Centered
star" label. **Not built** — backlog. Display overlay only; it never touches the
solve, the align, or the aim point.

## Motivation

diofinder already labels the **Centered star** — the cataloged star at the aim
point (`star_names.py`, from `star_names.csv`). But a finder is often pointed at
a **DSO**, and today the crosshair on M31 just names a nearby field star. A
"Centered object" label ("**M31 — Andromeda Galaxy**") tells the user what
they're actually on, on diofinder's own page, without switching to SkySafari.

This is the constructive half of the align-snap discussion
(`docs/decisions/2026-07-19-centroid-align-target-id.md`): the Messier list is
the set of bright, visually-catalogued DSOs a finder is aimed at, so it is the
right catalog for *labeling* even though it is **not** wanted for the align.

## Scope / non-goals

- **Label only.** Sets a `latest_solution` field the web UI shows; changes
  nothing in detection, solving, calibration, or the aim point.
- **Messier only** (110 rows), not NGC/IC. This is deliberately a *narrow local
  aim-point label*, not a DSO *planning* catalog — SkySafari owns planning (see
  AGENTS.md §7 accepted-by-design, which this does not overturn).
- Optional, like star naming: a missing catalog file disables it silently.

## Catalog (`astro_databases` side)

A `messier.csv` built and shipped by `astro_databases` alongside `star_names.csv`
and refreshed by `diofinder-db-update`. Columns (J2000):

| col | notes |
|-----|-------|
| `m` | Messier number, e.g. `M31` |
| `name` | common name, e.g. `Andromeda Galaxy` (may be empty) |
| `ra_deg`, `dec_deg` | J2000 center |
| `size_arcmin` | major-axis extent (drives the match radius) |
| `mag` | integrated magnitude (optional, for display/tie-break) |
| `type` | Gx / Neb / OC / GC / PN … (optional) |

110 rows — trivial to build (public Messier tables) and to load.

## Match logic (`diofinder`)

Mirror `star_names.py`. Load `messier.csv` into a `MessierCatalog` (precomputed
unit vectors, same as `StarNames`). Per solve, given the aim-point RA/Dec:

- An object is "centered" if the angular separation is within its **own extent**
  plus a small margin — **not** a fixed radius, because Messier sizes span
  ~1′ (M57) to ~3° (M31/M45). Use
  `radius = max(0.5 * size_arcmin/60 + margin_deg, floor_deg)` with e.g.
  `margin_deg ≈ 0.1`, `floor_deg ≈ 0.25` (so tiny objects still match within a
  finder-sensible circle). A fixed radius would either miss M31's disk or
  over-claim tiny planetaries.
- If several match (rare — overlapping large objects), pick the **nearest**
  center, tie-break on brightness.
- Result: `{m, name, mag, type, sep_deg}` or `None`.

Cost: one vectorized dot-product over 110 rows per solve — negligible, same shape
as the star-name lookup.

## Precedence — DSO vs star label

Both a Centered star and a Centered object can be present. Keep it simple and
non-destructive:

- Publish the DSO label in a **separate** field (`dso_*`), never overwriting the
  existing `star_*` fields — so nothing regresses if the feature is off/absent.
- The web UI shows the **DSO line when present** (it's the more useful "what am I
  on"), with the centered star as secondary/dimmed. Example:
  `Centered: M45 (Pleiades) · star Alcyone 0.3° off`.

## Plumbing

| Layer | Change |
|-------|--------|
| `astro_databases` | `build_messier.py` → `messier.csv` release asset; `diofinder-db-update` fetches it. |
| `diofinder/messier.py` (new) | `MessierCatalog.load()` + `centered(ra, dec)` — pure/vectorized, unit-tested like `tests/test_star_names.py`. |
| `solver_proc.py` | load the catalog at startup (non-fatal if missing); in the naming block, set `sol["dso_m"]/["dso_name"]/["dso_sep_deg"]` alongside the star fields. |
| `config.py` / conf | `messier_path` (default `/var/lib/diofinder/messier.csv`); `star_name_dso: bool = true` (live-mutable, mirrors `star_name_brightest`). |
| `webui` | Home + Camera "Centered" readout renders the DSO line; the status poller updates it (`status.result.solution.dso_*`). |

## Flaws / caveats

- **Extent is fuzzy.** A single center + size circle is a coarse model of a real
  DSO's shape; edge cases near a large object's boundary will flip in/out. Fine
  for a "what am I near" label; don't treat it as membership truth.
- **Overlaps.** M45 vs its member stars, M42/M43, the Virgo cluster's Messier
  galaxies — pick nearest-center; acceptable for a label.
- **Not a planning tool.** Only 110 objects; anything fainter/other-catalog isn't
  labeled. That's intentional — SkySafari covers planning.
- **Epoch.** Catalog is J2000, matched against the internal J2000 aim point (the
  label runs *before* the JNow report boundary) — no precession needed here.

## Effort

Small. A `messier.csv` builder in `astro_databases`, a
`messier.py` (a near-copy of `star_names.py`), a few solver + webui lines, and a
unit test. One release once the catalog asset exists.
