# Third-party notices

diofinder is licensed under the **GNU General Public License v3.0** (see
[`LICENSE`](LICENSE)). It builds on, bundles, or interoperates with the
components below. Their own licenses are reproduced/linked here and continue to
govern those components; the GPL-3.0 covers diofinder's own code.

GPL-3.0 is one-way compatible with the permissive (Apache-2.0 / MIT) licenses
below, so distributing the combined image under GPL-3.0 is consistent with
those terms (their copyright and attribution notices are preserved).

## Solver / extractor stack (bundled as wheels in the image)

| Component | License | Copyright / source |
|---|---|---|
| **sycamore-extract** (`star_detect`) | Apache-2.0 | © Matt Considine — https://github.com/mconsidine/sycamore-extract |
| **olive-solve** (`tetra3` plate solver) | Apache-2.0¹ | derivative of tetra3 / cedar-solve — https://github.com/mconsidine/olive-solve |
| **tetra3rs** (off-device calibration) | MIT | © Steven Michael — https://github.com/mconsidine/tetra3rs |

¹ olive-solve is distributed by its own maintainer; it is a derivative of tetra3
and cedar-solve (Apache-2.0). diofinder bundles its published wheel and does not
relicense it.

## Upstream algorithms these derive from

- **tetra3** — © European Space Agency (ESA), Apache-2.0 —
  https://github.com/esa/tetra3
- **cedar-solve** — © Steven Rosenthal (smroid), Apache-2.0 —
  https://github.com/smroid/cedar-solve
- **Tetra** (the original) — © 2016 brownj4, MIT.

## Star catalog data

The star databases are derived from:

- **Gaia DR3** — © ESA / Gaia DPAC, licensed **CC BY-SA 3.0 IGO**
  (https://creativecommons.org/licenses/by-sa/3.0/igo/). Gaia Collaboration et
  al. (2023), A&A, 674, A1.
- **Hipparcos 2** — ESA; van Leeuwen (2007), A&A, 474, 653. Free for scientific
  use.

## Inspiration (not incorporated)

- **eFinder_cli** by *AstroKeith* — **GPL-3.0** —
  https://github.com/mconsidine/eFinder_cli

  diofinder is an **independent reimplementation** inspired by AstroKeith's
  eFinder concept and the LX200 finder workflow. It does not incorporate code
  from eFinder_cli (different architecture: a multiprocess `diofinder` package
  with a Rust matched-filter extractor, temporal background cache, and Flask UI,
  versus eFinder_cli's `Solver/` layout). The "Legacy" seeing preset
  *reproduces* eFinder_cli's tetra3-extractor behavior via olive-solve, but is a
  reimplementation, not a copy. eFinder_cli is acknowledged here with gratitude
  as the project that inspired this one.

## Runtime libraries

diofinder also uses, unmodified, standard open-source libraries including NumPy
(BSD-3-Clause), Flask (BSD-3-Clause), and picamera2 / libcamera (BSD-2-Clause /
LGPL-2.1). These are installed from their upstream distributions and retain
their own licenses.
