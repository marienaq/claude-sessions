#!/bin/bash
# Resolve the Desktop symlink, or this cds to ~/Desktop.
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"
. ./find-python.sh
find_python || { echo "Needs Python 3.11 or newer (brew install python)."; read -r; exit 1; }
PORT=$("$PYTHON" -c 'import json,os,pathlib
p = pathlib.Path(os.environ.get("CSM_STATE_DIR", pathlib.Path.home() / ".claude-manager")) / "config.json"
try: print(json.loads(p.read_text()).get("port") or 7433)
except Exception: print(7433)')
PORT="${CSM_PORT:-$PORT}"

# If server is already running, just open the browser
if curl -s -o /dev/null -w "%{http_code}" http://localhost:$PORT/ 2>/dev/null | grep -q 200; then
    open "http://localhost:$PORT"
    exit 0
fi

# Otherwise, start the server
"$PYTHON" server.py &
SERVER_PID=$!
sleep 1
open "http://localhost:$PORT"
echo "Claude Session Manager running (PID $SERVER_PID)"
echo "Close this window to stop the server."
wait $SERVER_PID
