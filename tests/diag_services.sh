#!/usr/bin/env bash
# eFinder service & file health check.
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

CONF="${EFINDER_CONFIG:-/etc/efinder/efinder.conf}"

# ---------------------------------------------------------------------------
sep "Systemd services"
for svc in efinder cedar-detect; do
    state=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
    enabled=$(systemctl is-enabled "$svc" 2>/dev/null || echo "unknown")
    case "$state" in
        active)   pass "$svc: active  (enabled=$enabled)" ;;
        inactive) warn "$svc: inactive (enabled=$enabled)" ;;
        failed)   fail "$svc: FAILED  (enabled=$enabled)" ;;
        *)        warn "$svc: state=$state  enabled=$enabled" ;;
    esac
done

# ---------------------------------------------------------------------------
sep "Cedar-detect gRPC port (50051)"
if command -v ss &>/dev/null; then
    listeners=$(ss -tnlp 2>/dev/null | grep ':50051' || true)
else
    listeners=$(netstat -tnlp 2>/dev/null | grep ':50051' || true)
fi
if [ -n "$listeners" ]; then
    pass "Port 50051 is listening"
    echo "$listeners" | sed 's/^/    /'
else
    fail "Nothing listening on :50051 — cedar-detect is not running"
fi

# ---------------------------------------------------------------------------
sep "Maintenance socket"
MAINT_SOCK="${EFINDER_MAINT_SOCKET:-/run/efinder/maint.sock}"
if [ -S "$MAINT_SOCK" ]; then
    pass "$MAINT_SOCK exists"
    # Quick JSON status query
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
    obj = json.loads(line)
    if obj.get('ok'):
        r = obj['result']
        print(f\"  backend={r.get('solver_backend','?')}  \"\
              f\"test_mode={r.get('test_mode','?')}  \"\
              f\"solved={r.get('solved','?')}  \"\
              f\"stars={r.get('stars','?')}  \"\
              f\"solve_ms={r.get('solve_ms','?')}\")
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
    warn "$MAINT_SOCK not found (efinder daemon is not running)"
fi

# ---------------------------------------------------------------------------
sep "Shared memory frame buffers"
found=0
for i in 0 1 2; do
    shm="/dev/shm/efinder_frame_$i"
    if [ -f "$shm" ]; then
        sz=$(stat -c%s "$shm" 2>/dev/null || echo "?")
        pass "efinder_frame_$i  ($sz bytes)"
        found=$((found + 1))
    else
        warn "efinder_frame_$i: not present"
    fi
done
[ "$found" -eq 0 ] && fail "No SHM buffers found — efinder daemon is not running"

# ---------------------------------------------------------------------------
sep "Database files"
if [ -f "$CONF" ]; then
    t3_db=$(grep -E '^\s*tetra3_db\s*:' "$CONF" 2>/dev/null | tail -1 \
            | cut -d: -f2- | xargs 2>/dev/null || echo "default_database")
    t3rs_db=$(grep -E '^\s*tetra3rs_db\s*:' "$CONF" 2>/dev/null | tail -1 \
              | cut -d: -f2- | xargs 2>/dev/null || echo "/var/lib/efinder/efinder-tetra-database.bin")
else
    t3_db="default_database"
    t3rs_db="/var/lib/efinder/efinder-tetra-database.bin"
fi
info "tetra3_db   = $t3_db"
info "tetra3rs_db = $t3rs_db"

if [ -f "$t3rs_db" ]; then
    sz=$(du -h "$t3rs_db" | cut -f1)
    pass "tetra3rs DB: $t3rs_db  ($sz)"
else
    fail "tetra3rs DB NOT FOUND: $t3rs_db"
fi

# Check tetra3 Python DB via Python if available
if command -v /opt/efinder/venv/bin/python3 &>/dev/null; then
    t3_check=$(/opt/efinder/venv/bin/python3 -c "
try:
    import tetra3
    t3 = tetra3.Tetra3('$t3_db')
    print('OK  (loaded $t3_db)')
except Exception as e:
    print(f'FAIL  {e}')
" 2>&1 || echo "FAIL  python check error")
    if echo "$t3_check" | grep -q '^OK'; then
        pass "tetra3 Python DB: $t3_check"
    else
        fail "tetra3 Python DB: $t3_check"
    fi
fi

# ---------------------------------------------------------------------------
sep "Python libraries"
PYTHON="${EFINDER_PYTHON:-/opt/efinder/venv/bin/python3}"
if [ ! -x "$PYTHON" ]; then
    PYTHON=$(command -v python3 2>/dev/null || echo "")
fi
if [ -z "$PYTHON" ]; then
    warn "No python3 found; skipping library checks"
else
    info "Using $PYTHON"
    for lib in grpc numpy tetra3 tetra3rs picamera2 PIL; do
        result=$("$PYTHON" -c "import $lib; print('ok')" 2>&1 || true)
        if [ "$result" = "ok" ]; then
            pass "$lib"
        else
            fail "$lib: $(echo "$result" | head -1)"
        fi
    done
fi

# ---------------------------------------------------------------------------
sep "efinder / cedar processes"
procs=$(ps aux 2>/dev/null | grep -E '(efinder|cedar)' | grep -v grep | grep -v 'diag_services' || true)
if [ -n "$procs" ]; then
    echo "$procs"
else
    warn "No efinder/cedar processes found"
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
sep "Recent journal: efinder (last 40 lines)"
journalctl -u efinder --no-pager -n 40 2>/dev/null \
    || journalctl -u efinder -n 40 2>/dev/null \
    || warn "No journal for efinder (not a systemd unit or insufficient privileges)"

sep "Recent journal: cedar-detect (last 20 lines)"
journalctl -u cedar-detect --no-pager -n 20 2>/dev/null \
    || journalctl -u cedar-detect -n 20 2>/dev/null \
    || warn "No journal for cedar-detect"

echo
