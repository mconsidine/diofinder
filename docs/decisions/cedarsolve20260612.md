# Build Configuration Decision Record

**Date:** 2026-06-12  
**Session:** amazing-ride-zwrob  
**Session ID:** session_01VPcZw7fvor24dqdJYnHfyP  
**Repository:** mconsidine/cedar-solve  
**Role in pipeline:** Source repository — pure-Python wheel built by `mconsidine/testrepo` CI

---

## Role

`cedar-solve` provides the `cedar_solve` pure-Python plate solver (tetra3-based, Hipparcos catalog). It produces a `py3-none-any` wheel — architecture-independent — so a single build is used for both the aarch64 Pi deployment and the x86_64 CI benchmark.

It also provides the `cedar_detect_pb2*.py` gRPC stubs used by the benchmark to communicate with `cedar-detect-server`.

---

## Build Configuration Decisions

### Wheel Type

`py3-none-any` — pure Python, no compiled extension. Built with `python -m build --wheel` using Python 3.13 on the CI runner.

**Decision: single build, two consumers.**  
The aarch64 vendor step and the x86_64 benchmark both consume the same artifact (`cedar-solve-wheel`). No architecture-specific build is needed.

### Dependency Constraint Workaround

cedar-solve constrains `Pillow < 9`. Python 3.13 has no Pillow<9 wheel on PyPI. This breaks a normal `pip install cedar-solve`.

**Decision: install with `--no-deps` in all database and benchmark contexts.**  
Rationale: Only `tetra3.Tetra3.generate_database()` and the gRPC stubs are needed; Pillow is not exercised in the CI pipeline. `grpcio`, `protobuf`, and `numpy` are installed separately without version constraints.

### Branch Targeting

Default branch: `master` (cedar-solve uses `master`, not `main`). Configurable via `CEDARSOLVE_REF` repository variable.

---

## Release Asset

Published to `databases-latest` as `cedar_solve-*-py3-none-any.whl`. This single wheel serves both Pi deployment and CI benchmark.

---

## SHA Caching

Tracked in `vendor/.shas/cedar-solve` against the `CEDARSOLVE_REF` branch HEAD.

---

## Namespace Conflict with olive-solve

Both cedar-solve and olive-solve install into the `tetra3` Python package namespace. In the benchmark, they cannot be installed simultaneously. The benchmark pipeline:
1. Installs cedar-solve; copies `cedar_detect_pb2*.py` stubs to a safe location.
2. Uninstalls cedar-solve; installs olive-solve.
3. The pb2 stubs survive because they were copied out before the switch.

**Recommendation:** If cedar-solve is ever updated to use a distinct namespace, this workaround can be removed from `buildbinaries.yml` benchmark phases 2–4.

---

## Recommendations

- The `Pillow < 9` constraint in cedar-solve's `setup.cfg`/`pyproject.toml` is the root of the `--no-deps` workaround. Relaxing it upstream would allow a clean install.
- Pin `CEDARSOLVE_REF` to a release tag for reproducible builds.
