#!/bin/bash
# Claude Code hook: Load session todo context on conversation start
# This script looks up the current session's todo file and outputs it

STATE_DIR="${CSM_STATE_DIR:-$HOME/.claude-manager}"
INDEX_FILE="$STATE_DIR/todos/index.json"
TODOS_DIR="$STATE_DIR/todos"

# Get our PID (Claude Code's parent shell PID)
MY_PID=$$

# Walk up the process tree to find the claude process PID
CLAUDE_PID=""
CURRENT_PID=$PPID
for i in 1 2 3 4 5; do
    CMD=$(ps -o command= -p $CURRENT_PID 2>/dev/null | xargs)
    if [[ "$CMD" == "claude" || "$CMD" == *"/claude"* ]]; then
        CLAUDE_PID=$CURRENT_PID
        break
    fi
    CURRENT_PID=$(ps -o ppid= -p $CURRENT_PID 2>/dev/null | xargs)
    [ -z "$CURRENT_PID" ] && break
done

if [ -z "$CLAUDE_PID" ] || [ ! -f "$INDEX_FILE" ]; then
    exit 0
fi

# Look up iTerm session ID from index using Claude's PID
ITERM_ID=$(python3 - "$INDEX_FILE" "$CLAUDE_PID" 2>/dev/null <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get(sys.argv[2], ""))
except Exception:
    pass
PY
)

if [ -z "$ITERM_ID" ]; then
    exit 0
fi

TODO_FILE="$TODOS_DIR/$ITERM_ID.md"

if [ ! -f "$TODO_FILE" ]; then
    exit 0
fi

# Output the todo file contents for Claude to see
echo ""
echo "=== SESSION TODO LIST ==="
echo "File: $TODO_FILE"
echo ""
cat "$TODO_FILE"
echo ""
echo "=== END SESSION TODO ==="
echo ""
echo "When you complete a step, update this file by checking off the item (change [ ] to [x])."
echo "When adding new steps, append them to the file."
echo ""
