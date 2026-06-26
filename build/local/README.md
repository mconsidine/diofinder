# Local build toolkit (no CI, no act)

Shell equivalents of the GitHub Actions jobs that produce diofinder's binary
inputs, for running on any x86 Linux box. Each script mirrors its workflow's
flags exactly, so local artifacts match CI output. Nothing here pushes; you
review and push the vendor commit yourself.

| Script | Replaces | Output |
|---|---|---|
| `build-sycamore-wheel.sh [ref]` | sycamore-extract `build.yml` | `star_detect-*-cp313-*linux_aarch64.whl` |
| `build-olive-wheel.sh [ref]` | `vendor-binaries.yml` build-wheel job | `tetra3-*-abi3-*linux_aarch64.whl` |
| `vendor-wheels.sh [--commit]` | the vendor/commit steps of both vendor workflows | staged/committed `vendor/wheels/` update |
| `build-database.sh` | the database step of `release.yml` | `solver_database.npz` |
| `check-workflows.sh [repo ...]` | pushing to CI just to find YAML errors | actionlint report |

## One-time prerequisites

```bash
sudo apt-get install -y gcc-aarch64-linux-gnu
curl https://sh.rustup.rs -sSf | sh        # if rustup not installed
rustup target add aarch64-unknown-linux-gnu
pip install 'maturin>=1.0,<2.0'
```

`build-sycamore-wheel.sh` additionally needs a `python3.13` binary on PATH
(matches the Pi OS Bookworm venv; override with `PYVER=`). The olive wheel is
abi3 and needs no specific Python.

## Typical flow

```bash
cd diofinder

# Rebuild both wheels from main/HEAD (or pass a tag/branch/sha):
build/local/build-sycamore-wheel.sh v0.11.1
build/local/build-olive-wheel.sh                # add SOLVER_ONLY=1 for the slim wheel

# Stage them into vendor/wheels/ (clears stale versions, like CI does):
build/local/vendor-wheels.sh --commit
git push origin olive

# Regenerate the solver database when FOV parameters change:
DB_MAX_FOV=14.0 build/local/build-database.sh
scp build/local/out/solver_database.npz pi:/var/lib/diofinder/

# Validate workflow YAML before pushing (this repo + siblings).
# Anything after -- is passed to actionlint, e.g. to mute style-level
# shellcheck notes:
build/local/check-workflows.sh . ../sycamore-extract -- -ignore SC2012 -ignore SC2086
```

Sources are found in this order: `$SYCAMORE_SRC` / `$OLIVE_SRC` env vars, a
sibling checkout (`../sycamore-extract`, `../olive-solve`), else a cached
clone under `build/local/src/`. Built artifacts land in `build/local/out/`.

## Notes

- Wheels are tagged `linux_aarch64` (via `maturin --compatibility linux`)
  instead of the CI release wheels' `manylinux2014`. Pip on the Pi accepts
  both; this just avoids pretending a plain cross-build is manylinux-audited,
  and replaces the old "rename manylinux_2_34 → linux_aarch64" workaround.
- Cortex-A53 tuning: sycamore gets it from its committed `.cargo/config.toml`;
  the olive build sets the same `RUSTFLAGS` the vendor workflow uses (which
  also covers older olive-solve refs that predate its config file).
- `build/local/out/`, `build/local/src/`, the venv, and the catalogue cache
  are git-ignored; only the scripts and this README are tracked.
