#!/usr/bin/env bash
# Lint GitHub Actions workflow YAML locally — schema, expression/context
# typing, action inputs, and shellcheck on every run: block — so workflow
# edits are validated before any push spends Actions minutes (and without
# act). Uses rhysd/actionlint, downloaded once into build/local/bin/.
#
# Usage:
#   build/local/check-workflows.sh [repo-dir ...] [-- <actionlint args>]
#
# With no arguments, checks this repo's .github/workflows/. Pass other
# checkout paths to lint sibling repos too:
#   build/local/check-workflows.sh . ../sycamore-extract ../olive-solve
#
# Exit code is non-zero if any repo has findings. shellcheck findings are
# included when shellcheck is installed (sudo apt-get install -y shellcheck).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

ACTIONLINT_VERSION="1.7.12"
BIN_DIR="$REPO_ROOT/build/local/bin"
ACTIONLINT="$BIN_DIR/actionlint"

if [ ! -x "$ACTIONLINT" ] || ! "$ACTIONLINT" --version | head -1 | grep -q "^$ACTIONLINT_VERSION$"; then
    mkdir -p "$BIN_DIR"
    echo "Installing actionlint $ACTIONLINT_VERSION into $BIN_DIR ..." >&2
    bash <(curl -fsSL https://raw.githubusercontent.com/rhysd/actionlint/main/scripts/download-actionlint.bash) \
        "$ACTIONLINT_VERSION" "$BIN_DIR" >/dev/null
fi

REPOS=()
EXTRA_ARGS=()
seen_dashdash=0
for arg in "$@"; do
    if [ "$arg" = "--" ]; then seen_dashdash=1; continue; fi
    if [ "$seen_dashdash" = "1" ]; then EXTRA_ARGS+=("$arg"); else REPOS+=("$arg"); fi
done
[ ${#REPOS[@]} -gt 0 ] || REPOS=("$REPO_ROOT")

command -v shellcheck >/dev/null \
    || echo "note: shellcheck not installed; run-block linting is skipped" >&2

status=0
for repo in "${REPOS[@]}"; do
    wf="$repo/.github/workflows"
    if [ ! -d "$wf" ]; then
        echo "--- $repo: no .github/workflows, skipping"
        continue
    fi
    echo "--- $repo"
    if (cd "$repo" && "$ACTIONLINT" "${EXTRA_ARGS[@]}"); then
        echo "    OK"
    else
        status=1
    fi
done
exit $status
