#!/bin/bash
# mh — the write path for agents.
#
# Installed at <repo>/operations/mh and operates on <repo>, derived from this
# script's own location. It must not fall back to a default path: pointed at
# the wrong repo it writes an empty store and regenerates the views from it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MELLONHEAD_ROOT="${MELLONHEAD_ROOT:-$(dirname "$HERE")}"
exec /opt/homebrew/bin/python3 \
  "$HOME/Projects/claude-sessions-phase2/mhcli.py" "$@"
