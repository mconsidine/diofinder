# Decision Record: hip_main.dat Download Fix

**Date:** 2026-06-12  
**Session ID:** 016awkUtyTwCrGAJTFND3uKT  
**Session URL:** https://claude.ai/code/session_016awkUtyTwCrGAJTFND3uKT  
**Branch:** olive  
**Repo:** mconsidine/diofinder  

---

## Problem

The "Generate tetra3 star database" step in `.github/workflows/release.yml`
downloaded `hip_main.dat` from the upstream CDS Hipparcos archive:

```
https://cdsarc.cds.unistra.fr/ftp/cats/I/239/hip_main.dat
```

This fails with `wget` exit code 4 (network failure) on GitHub Actions
runners. The CDS archive is unreliable / rate-limited from CI IP ranges.

---

## Decision

Replace the CDS `wget` with a `curl | gunzip` download from the owner's
own mirror at `mconsidine/astro_databases`.

---

## Change Applied

**File:** `.github/workflows/release.yml`  
**Commit:** `d9c0e882c33b654fd9bdbc59a61ce23dd68d4497`

Before:
```yaml
          echo "Downloading hip_main.dat from CDS Hipparcos archive..."
          wget -q --show-progress \
            https://cdsarc.cds.unistra.fr/ftp/cats/I/239/hip_main.dat \
            -O "${TETRA3_DIR}/hip_main.dat"
```

After:
```yaml
          echo "Downloading hip_main.dat from mconsidine/astro_databases mirror..."
          curl -fsSL \
            https://raw.githubusercontent.com/mconsidine/astro_databases/main/hip_main.dat.gz \
            | gunzip > "${TETRA3_DIR}/hip_main.dat"
```

`curl` and `gunzip` (from `gzip`) are both available on the ubuntu-latest
runner without any additional `apt-get` install step. `curl` was already
present in the job's install list.

---

## Assumptions

- `hip_main.dat.gz` is at the root of the `main` branch of
  `mconsidine/astro_databases`. If the branch or path differs, update the
  raw URL accordingly.

---

## Same Fix Applied Elsewhere

The identical fix was applied to the `sycamore-only` branch of this repo
(same session), and manually documented for two additional workflow files
in `mconsidine/testrepo` (outside tool-access scope; applied by owner).
