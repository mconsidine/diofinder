# Auto-activate the eFinder Python venv for interactive login shells, so that
# python / pip and the diagnostic scripts resolve their deps without a manual
# `source /opt/efinder/venv/bin/activate`. Installed to /etc/profile.d/ by
# scripts/install.sh.
#
# Scope: this only affects LOGIN shells (e.g. an interactive `ssh efinder@host`
# session). Non-interactive `ssh efinder@host '<cmd>'` and the systemd services
# are unaffected — those should call /opt/efinder/venv/bin/python3 by full path
# (as the tests/ scripts already do). The guard skips activation when a venv is
# already active or the venv has not been created yet.
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f /opt/efinder/venv/bin/activate ]; then
    . /opt/efinder/venv/bin/activate
fi
