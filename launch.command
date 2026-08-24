#!/bin/bash
cd "$(dirname "$0")"

# If server is already running, just open the browser
if curl -s -o /dev/null -w "%{http_code}" http://localhost:7433/ 2>/dev/null | grep -q 200; then
    open "http://localhost:7433"
    exit 0
fi

# Otherwise, start the server
python3 server.py &
SERVER_PID=$!
sleep 1
open "http://localhost:7433"
echo "Claude Session Manager running (PID $SERVER_PID)"
echo "Close this window to stop the server."
wait $SERVER_PID
