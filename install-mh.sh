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

install -m 755 "$HERE/mh" "$TARGET/operations/mh"

# Regenerate rather than copy, so the reference can never be stale relative
# to the parser it documents.
/opt/homebrew/bin/python3 "$HERE/mhcli.py" docs > "$TARGET/operations/mh-reference.md"

echo "installed into $TARGET/operations:"
echo "  mh              -> agents call ./operations/mh"
echo "  mh-reference.md -> the command surface, generated from the parser"

if [ ! -f "$TARGET/operations/tasks.db" ]; then
    echo
    echo "note: no tasks.db here yet, so mh will refuse to run against it."
    echo "      Run the migration first."
fi
