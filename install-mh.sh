#!/bin/bash
# Install the mh entry point and its reference into a content repo.
#
#   ./install-mh.sh ~/Projects/mellonhead-dev
#   ./install-mh.sh ~/Projects/mellonhead          # a cutover step
#
# The implementation stays in this repo, with the store it writes to. What
# lands in the content repo is the shim agents call and the reference they
# read, matching how Phase 1 ships operations/task-done.sh.
#
# Needed because refresh-dev-copy.sh mirrors from live with --delete:
# mh-reference.md matches its *.md filter and is removed on every refresh,
# and the shim survives only because it happens to have no extension. That
# is luck, not design, so both are reinstalled explicitly.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"

if [ -z "$TARGET" ]; then
    echo "usage: install-mh.sh <content-repo>" >&2
    exit 2
fi
TARGET="${TARGET/#\~/$HOME}"

if [ ! -d "$TARGET/operations" ]; then
    echo "no operations/ directory in $TARGET; is that a content repo?" >&2
    exit 2
fi

. "$HERE/find-python.sh"
find_python || { echo "needs Python 3.11 or newer; set CSM_PYTHON" >&2; exit 2; }

# The path is written into a double-quoted shell string in the shim, and
# through sed, so anything that could end the string or the sed expression
# is refused rather than escaped.
case "$HERE" in
    *[\"\$\`\\\|\&]*)
        echo "refusing: checkout path contains a quote, \$, backtick, \\, | or &: $HERE" >&2
        exit 2 ;;
esac

# The shim records which checkout it came from, so it finds mhcli.py without
# MH_CODE wherever this repo was cloned. `|` as the sed delimiter because the
# path is full of slashes.
sed "s|@MH_CODE@|$HERE|" "$HERE/mh" > "$TARGET/operations/mh.tmp"
chmod 755 "$TARGET/operations/mh.tmp"
mv "$TARGET/operations/mh.tmp" "$TARGET/operations/mh"

# Regenerate rather than copy, so the reference can never be stale relative
# to the parser it documents. --repo so it names this repo's primary user.
"$PYTHON" "$HERE/mhcli.py" docs --repo "$TARGET" > "$TARGET/operations/mh-reference.md"

echo "installed into $TARGET/operations:"
echo "  mh              -> agents call ./operations/mh"
echo "  mh-reference.md -> the command surface, generated from the parser"

if [ ! -f "$TARGET/operations/tasks.db" ]; then
    echo
    echo "note: no tasks.db here yet, so mh will refuse to run against it."
    echo "      Create one with: $TARGET/operations/mh init --repo $TARGET --user <you>"
fi
