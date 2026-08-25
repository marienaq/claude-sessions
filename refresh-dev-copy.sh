#!/bin/bash
# Rebuild the scratch content repo from the live one.
#
# Text only: the live repo is 16G, almost all of it course-material video and
# decks that the task store never reads. This mirrors the directory tree and
# every markdown/script/data file, about 54M.
#
# .git is deliberately left out. mellonhead's remote is public and must never
# be pushed; a copy with no git cannot push at all.

set -euo pipefail

SRC="${MELLONHEAD_SRC:-$HOME/Projects/mellonhead}"
DEST="${MELLONHEAD_ROOT:-$HOME/Projects/mellonhead-dev}"

case "$DEST" in
    "$HOME/Projects/mellonhead"|"$HOME/Projects/mellonhead/")
        echo "Refusing: destination is the live repo." >&2; exit 1 ;;
esac

[ -d "$SRC" ] || { echo "No source repo at $SRC" >&2; exit 1; }

echo "Mirroring text files: $SRC -> $DEST"
rsync -a --delete \
    --include='*/' \
    --include='*.md' --include='*.sh' --include='*.py' \
    --include='*.json' --include='*.csv' --include='*.txt' \
    --exclude='*' \
    "$SRC/" "$DEST/"

# rsync's --include='*/' recreates .git as empty dirs before any exclude can
# apply, so clear it after the fact.
rm -rf "${DEST:?}/.git"

echo "done: $(du -sh "$DEST" | cut -f1), $(find "$DEST" -name '*.md' | wc -l | tr -d ' ') markdown files"
echo "git repo: $(git -C "$DEST" rev-parse --git-dir 2>/dev/null || echo 'none (cannot be pushed)')"
