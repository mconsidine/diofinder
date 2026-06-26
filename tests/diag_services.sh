#!/usr/bin/env bash
# diofinder service & file health check (olive backend).
# Run as root (or with sudo) for full journal and process info.
#
# Usage:
#   sudo bash tests/diag_services.sh

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

pass() { echo -e "  ${GREEN}PASS${NC}  $*"; }
fail() { echo -e "  ${RED}FAIL${NC}  $*"; }
warn() { echo -e "  ${YELLOW}WARN${NC}  $*"; }
info() { echo -e "  ${CYAN}INFO${NC}  $*"; }
sep()  { echo; echo -e "${BOLD}=== $* ===${NC}"; }

CONF="${DIOFINDER_CONFIG:-/etc/diofinder/diofinder.conf}"

# ---------------------------------------------------------------------------
sep "Systemd service"
state=$(systemctl is-active "diofinder" 2>/dev/null || echo "unknown")
enabled=$(systemctl is-enabled "diofinder" 2>/dev/null || echo "unknown")
case "$state" in
    active)   pass "diofinder: active  (enabled=$enabled)" ;;
    inactive) warn "diofinder: inactive (enabled=$enabled)" ;;
    failed)   fail "diofinder: FAILED  (enabled=$enabled)" ;;
    *)        warn "diofinder: state=$state  enabled=$enabled" ;;
esac

# ---------------------------------------------------------------------------
sep "Maintenance socket"
MAINT_SOCK="${DIOFINDER_MAINT_SOCKET:-/run/diofinder/maint.sock}"
if [ -S "$MAINT_SOCK" ]; then
    pass "$MAINT_SOCK exists"
    if command -v python3 &>/dev/null; then
        status_out=$(python3 -c "
import sys, socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(3.0)
try:
    s.connect('$MAINT_SOCK')
    s.sendall(b'{\"cmd\":\"status\",\"args\":{}}\n')
    buf = b''
    while b'\n' not in buf:
        chunk = s.recv(4096)
        if not chunk: break
        buf += chunk
    line = buf.partition(b'\n')[0]
    obj  = json.loads(line)
    if obj.get('ok'):
        r = obj['result']
        print(f\"  solved={r.get('solved','?')}  \"\
              f\"stars={r.get('stars','?')}  \"\
              f\"solve_ms={r.get('solve_ms','?'):.0f}  \"\
              f\"fov={r.get('fov_deg','?')}\")
    else:
        print(f\"  error: {obj.get('error')}\")
except Exception as e:
    print(f\"  maint query failed: {e}\")
finally:
    s.close()
" 2>&1 || true)
        info "maint status: $status_out"
    fi
else
    warn "$MAINT_SOCK not found (diofinder daemon is not running)"
fi

# ---------------------------------------------------------------------------
sep "Shared memory frame buffers"
found=0
for i in 0 1 2; do
    shm="/dev/shm/diofinder_frame_$i"
    if [ -f "$shm" ]; then
        sz=$(stat -c%s "$shm" 2>/dev/null || echo "?")
        pass "diofinder_frame_$i  ($sz bytes)"
        found=$((found + 1))
    else
        warn "diofinder_frame_$i: not present"
    fi
done
[ "$found" -eq 0 ] && fail "No SHM buffers found — diofinder daemon is not running"

# ---------------------------------------------------------------------------
sep "Database file (olive-solve tetra3-py)"
if [ -f "$CONF" ]; then
    solver_db=$(grep -E '^\s*solver_db\s*:' "$CONF" 2>/dev/null | tail -1 \
                | cut -d: -f2- | xargs 2>/dev/null || echo "default_database")
else
    solver_db="default_database"
fi
# Expand to absolute path if needed
if [[ "$solver_db" != /* ]]; then
    solver_db="/var/lib/diofinder/${solver_db}.npz"
fi
info "solver_db = $solver_db"

if [ -f "$solver_db" ]; then
    sz=$(du -h "$solver_db" | cut -f1)
    pass "Database: $solver_db  ($sz)"
else
    fail "Database NOT FOUND: $solver_db"
    warn "  Run: sudo bash build/build-image.sh  (or trigger vendor-binaries CI)"
fi

# Verify database loads correctly
PYTHON="${DIOFINDER_PYTHON:-/opt/diofinder/venv/bin/python3}"
if [ ! -x "$PYTHON" ]; then
    PYTHON=$(command -v python3 2>/dev/null || echo "")
fi
if [ -n "$PYTHON" ] && [ -f "$solver_db" ]; then
    db_check=$("$PYTHON" -c "
import tetra3, time
t0 = time.monotonic()
t3 = tetra3.Tetra3('$solver_db')
ms = (time.monotonic()-t0)*1000
print(f'OK  loaded in {ms:.0f}ms')
" 2>&1 || echo "FAIL  python check error")
    if echo "$db_check" | grep -q '^OK'; then
        pass "Database loads: $db_check"
    else
        fail "Database load failed: $db_check"
    fi
fi

# ---------------------------------------------------------------------------
sep "Python libraries"
if [ -z "${PYTHON:-}" ]; then
    warn "No python3 found; skipping library checks"
else
    info "Using $PYTHON"
    for lib in numpy tetra3 picamera2 PIL; do
        result=$("$PYTHON" -c "import $lib; print('ok')" 2>&1 || true)
        if [ "$result" = "ok" ]; then
            pass "$lib"
        else
            fail "$lib: $(echo "$result" | head -1)"
        fi
    done
fi

# ---------------------------------------------------------------------------
sep "diofinder processes"
procs=$(ps aux 2>/dev/null | grep -E '(diofinder|solver_proc|camera_proc|comms_proc)' \
        | grep -v grep | grep -v 'diag_services' || true)
if [ -n "$procs" ]; then
    echo "$procs"
else
    warn "No diofinder processes found"
fi

# ---------------------------------------------------------------------------
sep "Config file"
if [ -f "$CONF" ]; then
    pass "$CONF"
    grep -v '^\s*#' "$CONF" | grep -v '^\s*$' | sed 's/^/    /'
else
    warn "$CONF not found — daemon will use built-in defaults"
fi

# ---------------------------------------------------------------------------
sep "Recent journal: diofinder (last 50 lines)"
journalctl -u diofinder --no-pager -n 50 2>/dev/null \
    || journalctl -u diofinder -n 50 2>/dev/null \
    || warn "No journal for diofinder (not a systemd unit or insufficient privileges)"

echo
