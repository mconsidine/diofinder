#!/bin/bash
# Local dev: verify the source tree has everything build-image.sh expects,
# WITHOUT doing a real image build. Run this after any restructuring of
# the repo to catch missing files before pushing.
#
# Usage: bash build/check-tree.sh
#
# This deliberately doesn't need root, qemu, or losetup -- it's just
# checking that the inputs to build-image.sh are present and well-formed.

set -euo pipefail

LOG()  { echo "==> $*"; }
WARN() { echo "WARNING: $*" >&2; }
FAIL() { echo "ERROR: $*" >&2; exit 1; }

# Run from repo root
[ -d diofinder ] || FAIL "must run from repo root"

# 1. Required directories
LOG "Checking required directories"
for d in diofinder webui systemd scripts etc build .github/workflows; do
  [ -d "$d" ] || FAIL "missing dir: $d"
done

# 2. Required files
LOG "Checking required files"
for f in \
  scripts/install.sh \
  scripts/firstboot.sh \
  scripts/diofinder-update \
  scripts/diofinder-ctl \
  scripts/ap.sh \
  scripts/station.sh \
  build/build-image.sh \
  systemd/diofinder.service \
  systemd/diofinder-firstboot.service \
  systemd/diofinder-webui.service \
  etc/diofinder.conf.default \
  etc/sudoers.d/diofinder-update \
  webui/app.py \
  webui/templates/dashboard.html \
  webui/templates/polar.html \
  webui/static/style.css \
  requirements.txt \
  diofinder/diofinder_main.py \
  diofinder/solver_proc.py \
  diofinder/comms_proc.py \
  diofinder/camera_proc.py \
  diofinder/config.py \
  diofinder/calibration.py \
  diofinder/polar.py \
  diofinder/polar_run.py \
  diofinder/maint.py \
  diofinder/align.py \
  .github/workflows/release.yml; do
  [ -f "$f" ] || FAIL "missing file: $f"
done

# 3. Shell scripts must parse
LOG "Checking shell scripts parse"
for s in scripts/install.sh scripts/firstboot.sh scripts/diofinder-update \
         scripts/ap.sh scripts/station.sh \
         build/build-image.sh build/check-tree.sh; do
  bash -n "$s" || FAIL "$s has syntax errors"
done

# 4. Python files must parse
LOG "Checking Python files parse"
PY=$(command -v python3 || true)
[ -n "$PY" ] || FAIL "python3 not in PATH"
find diofinder webui tests -name "*.py" -print0 \
  | xargs -0 -I{} "$PY" -m py_compile {} \
  || FAIL "Python syntax errors detected"
"$PY" -m py_compile scripts/diofinder-ctl \
  || FAIL "scripts/diofinder-ctl has syntax errors"

# 5. systemd unit syntax (basic INI-style)
LOG "Checking systemd unit syntax"
for u in systemd/*.service; do
  if ! grep -q "^\[Unit\]" "$u"; then
    FAIL "$u missing [Unit] section"
  fi
  if ! grep -q "^\[Service\]" "$u"; then
    FAIL "$u missing [Service] section"
  fi
  # ExecStart is required for Type=simple/oneshot/etc.
  if ! grep -qE "^ExecStart=" "$u"; then
    WARN "$u has no ExecStart= line"
  fi
done

# 6. YAML workflow validity
LOG "Checking GitHub Actions workflow YAML"
"$PY" -c "
import sys, yaml, glob
for f in glob.glob('.github/workflows/*.yml'):
    try:
        yaml.safe_load(open(f))
        print(f'  OK: {f}')
    except Exception as e:
        print(f'  FAIL: {f}: {e}', file=sys.stderr)
        sys.exit(1)
"

# 7. Cross-references: things install.sh expects to install
LOG "Checking install.sh references"
for f in $(grep -oE 'install -m [0-9]+ "\$DIOFINDER_DIR/[^"]+"' scripts/install.sh \
           | sed 's|install -m [0-9]* "$DIOFINDER_DIR/||;s|"$||'); do
  [ -f "$f" ] || FAIL "install.sh references missing file: $f"
done

LOG "All tree checks passed"
