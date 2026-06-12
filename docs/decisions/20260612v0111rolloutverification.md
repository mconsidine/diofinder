# 2026-06-12 — v0.11.1 rollout verification; local-build wheel tagging

**Session:** funny-noether (`claude/funny-noether-lro1ig`)
**Session ID:** `session_01216HrQG6gvzvjiiAqZQUux` — https://claude.ai/code/session_01216HrQG6gvzvjiiAqZQUux
**Continues:** `session_018PPLNhiAv325425icnyf8g`

No code changes were made to this repo this session; this records the
verification and cross-repo decisions that touch it.

## Assessments

- **v0.11.1 rollout confirmed complete** (the prior session's open action
  item): tag `v0.11.1` at `7ee0b8e` on `main`; build workflow green
  (2026-06-10 10:42 UTC); Vendor Sycamore run vendored the cp313 aarch64
  wheel into diofinder `olive` (`ce26d41`); diofinder release v0.0.20 built
  green (2026-06-10 10:51 UTC). Remaining step is on-device:
  `efinder-update` / image install, then the handoff's verification list
  (incl. the predicted top_hat ≈100 ms → ~40–60 ms at bin=2).
- Local cross-build of this crate verified from the tag via diofinder's new
  `build/local/build-sycamore-wheel.sh` (19 s on x86; aarch64 ELF confirmed).

## Decisions affecting this repo

1. **Local builds now tag wheels `linux_aarch64`** via
   `maturin --compatibility linux` (used by diofinder's
   `build/local/build-sycamore-wheel.sh`). This supersedes the CLAUDE.md
   gotcha about renaming `manylinux_2_34_aarch64` wheels after a local cross
   build — no rename needed. CI release wheels remain manylinux2014 (built in
   maturin-action's docker image) and are unaffected.
2. The Cortex-A53 `.cargo/config.toml` pattern from this repo was propagated
   to olive-solve (its commit `0176bf7` on `olive-solve-noext`), closing the
   gap where local olive builds lost `target-cpu`.

## Recommendations

- Optionally update the CLAUDE.md "common gotchas" wheel-rename entry to
  mention `--compatibility linux` as the current local-build practice.
- `scripts/build_cross.sh` could adopt `--compatibility linux` for the same
  reason (left unchanged this session).
