# Auto-activate the diofinder Python venv for interactive login shells, so that
# python / pip and the diagnostic scripts resolve their deps without a manual
# `source /opt/diofinder/venv/bin/activate`. Installed to /etc/profile.d/ by
# scripts/install.sh.
#
# Scope: this only affects LOGIN shells (e.g. an interactive `ssh diofinder@host`
# session). Non-interactive `ssh diofinder@host '<cmd>'` and the systemd services
# are unaffected — those should call /opt/diofinder/venv/bin/python3 by full path
# (as the tests/ scripts already do). The guard skips activation when a venv is
# already active or the venv has not been created yet.
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f /opt/diofinder/venv/bin/activate ]; then
    . /opt/diofinder/venv/bin/activate
fi
