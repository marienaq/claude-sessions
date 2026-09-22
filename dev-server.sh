#!/bin/bash
# Start the Phase 2 session manager against a scratch copy of the content repo.
# The live Phase 1 manager keeps running on 7433 with its own state; nothing
# here touches it. See operations/prioritization-implementation-plan.md 5.7.

set -euo pipefail

export MELLONHEAD_ROOT="${MELLONHEAD_ROOT:-$HOME/Projects/mellonhead-dev}"
export CSM_STATE_DIR="${CSM_STATE_DIR:-$HOME/.claude-manager-dev}"
PORT="${CSM_PORT:-7434}"

if [ ! -d "$MELLONHEAD_ROOT" ]; then
    echo "No content repo at $MELLONHEAD_ROOT. Build it with:" >&2
    echo "  ./refresh-dev-copy.sh" >&2
    exit 1
fi

if [ -e "$MELLONHEAD_ROOT/.git" ]; then
    echo "Refusing to start: $MELLONHEAD_ROOT is a git repo." >&2
    echo "The scratch copy must have no git remote (mellonhead's is public)." >&2
    exit 1
fi

if [ "$PORT" = "7433" ]; then
    echo "Refusing to start on 7433, that is the live manager." >&2
    exit 1
fi

# Sessions work in the real repo; the store here is built from a copy. Map the
# prefix so project lookup resolves and the task panel can be tested.
export CSM_CWD_ALIAS="${CSM_CWD_ALIAS:-$HOME/Projects/mellonhead:$MELLONHEAD_ROOT}"

mkdir -p "$CSM_STATE_DIR"
cd "$(dirname "$0")"
. ./find-python.sh
find_python || { echo "needs Python 3.11 or newer" >&2; exit 1; }
exec "$PYTHON" server.py --port "$PORT"
