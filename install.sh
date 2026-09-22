#!/bin/bash
# Set up the Claude Session Manager for one person on one Mac.
#
#   ./install.sh                      asks for everything
#   ./install.sh --user alex --name Alex --root ~/Projects/work --yes
#
# Every step is optional and safe to re-run: it checks what is already there
# before writing, and says what it skipped. Nothing here needs sudo.
#
#   --user ID        the primary user's id: lower case, no spaces. Tasks you
#                    own, and everything you do on the dashboard, carry it.
#   --name NAME      how you are shown ("Alex has next step"). Default: ID
#                    capitalised.
#   --alias TEXT     another spelling that should count as you when an agent
#                    hands a task to it (a full name, initials). Repeatable.
#   --root DIR       the workspace: where priorities.md, project task lists
#                    and operations/tasks.db live.
#   --port N         dashboard port (default 7433)
#   --no-store       dashboard only: no task store, no mh
#   --no-hook        skip the Claude Code SessionStart hook
#   --no-autostart   skip the LaunchAgents
#   --yes            accept every default without asking

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${CSM_STATE_DIR:-$HOME/.claude-manager}"
CONFIG="$STATE_DIR/config.json"
LABEL="com.claude-sessions"

USER_ID="" NAME="" ROOT="" PORT="" YES=0
ALIASES=()
DO_STORE=1 DO_HOOK=1 DO_AUTOSTART=1

while [ $# -gt 0 ]; do
    case "$1" in
        --user) USER_ID="$2"; shift 2 ;;
        --name) NAME="$2"; shift 2 ;;
        --alias) ALIASES+=("$2"); shift 2 ;;
        --root) ROOT="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --no-store) DO_STORE=0; shift ;;
        --no-hook) DO_HOOK=0; shift ;;
        --no-autostart) DO_AUTOSTART=0; shift ;;
        --yes|-y) YES=1; shift ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ask VAR "question" default
ask() {
    local __var="$1" __q="$2" __def="${3:-}" __ans=""
    if [ "$YES" = 1 ]; then
        printf -v "$__var" '%s' "$__def"; return
    fi
    read -r -p "  $__q${__def:+ [$__def]}: " __ans || true
    printf -v "$__var" '%s' "${__ans:-$__def}"
}

# confirm "question" -> 0 for yes. Default yes.
confirm() {
    [ "$YES" = 1 ] && return 0
    local ans=""
    read -r -p "  $1 [Y/n]: " ans || true
    [[ ! "$ans" =~ ^[Nn] ]]
}

config_get() {
    [ -f "$CONFIG" ] || return 0
    "$PYTHON" -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")
except Exception: pass' "$CONFIG" "$1"
}

# ---------------------------------------------------------------------------
say "Checking requirements"

[ "$(uname)" = "Darwin" ] || die "macOS only: sessions are found through iTerm2 and AppleScript."
ok "macOS"

. "$HERE/find-python.sh"
find_python || die "Python 3.11 or newer not found. Install it (brew install python) or set CSM_PYTHON."
ok "Python: $PYTHON ($("$PYTHON" -c 'import platform; print(platform.python_version())'))"

if [ -d /Applications/iTerm.app ] || [ -d "$HOME/Applications/iTerm.app" ]; then
    ok "iTerm2"
else
    warn "iTerm2 not found. The dashboard only sees Claude sessions running in iTerm2 tabs:"
    warn "https://iterm2.com. Continuing; install it before starting the server."
fi

if command -v claude >/dev/null 2>&1; then
    ok "Claude Code: $(command -v claude)"
else
    warn "claude is not on PATH. Resume and \"Work on this\" open tabs that run it."
fi

# ---------------------------------------------------------------------------
say "Who is this for"

DEFAULT_USER="$(printf '%s' "${USER:-me}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
[ -n "$USER_ID" ] || ask USER_ID "Your user id (lower case, no spaces)" "$DEFAULT_USER"
USER_ID="$(printf '%s' "$USER_ID" | tr '[:upper:]' '[:lower:]')"
[[ "$USER_ID" =~ ^[a-z][a-z0-9_-]{0,31}$ ]] \
    || die "user id must be lower case letters, digits, - or _, starting with a letter"

DEFAULT_NAME="$(printf '%s' "$USER_ID" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')"
[ -n "$NAME" ] || ask NAME "Name shown on the dashboard" "$DEFAULT_NAME"
if [ ${#ALIASES[@]} -eq 0 ] && [ "$YES" = 0 ]; then
    FULL="$(id -F 2>/dev/null || true)"
    ask ALIAS_LINE "Other names that mean you, comma separated (optional)" "$FULL"
    IFS=',' read -r -a ALIASES <<< "${ALIAS_LINE:-}"
fi

# ---------------------------------------------------------------------------
say "Where your tasks live"

EXISTING_ROOT="$(config_get root)"
if [ -z "$ROOT" ]; then
    DEFAULT_ROOT="${EXISTING_ROOT:-${MELLONHEAD_ROOT:-$HOME/Projects/workspace}}"
    ask ROOT "Workspace directory (priorities.md, task lists, the store)" "$DEFAULT_ROOT"
fi
ROOT="${ROOT/#\~/$HOME}"
mkdir -p "$ROOT/operations"
ROOT="$(cd "$ROOT" && pwd)"
ok "workspace: $ROOT"

[ -n "$PORT" ] || PORT="$(config_get port)"
PORT="${PORT:-7433}"

mkdir -p "$STATE_DIR"
"$PYTHON" - "$CONFIG" "$ROOT" "$PORT" <<'PY'
import json, sys
path, root, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    cfg = json.load(open(path))
except Exception:
    cfg = {}
cfg.update(root=root, port=port)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY
ok "config: $CONFIG"

# ---------------------------------------------------------------------------
if [ "$DO_STORE" = 1 ]; then
    say "Task store and the mh CLI"

    INIT_ARGS=(--repo "$ROOT" init --user "$USER_ID" --name "$NAME" --actor "$USER_ID")
    for a in "${ALIASES[@]+"${ALIASES[@]}"}"; do
        a="$(printf '%s' "$a" | sed 's/^ *//; s/ *$//')"
        [ -n "$a" ] && INIT_ARGS+=(--alias "$a")
    done

    if [ -f "$ROOT/operations/tasks.db" ]; then
        CURRENT="$("$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); import mhstore
s = mhstore.open_store(root=sys.argv[2], seed_settings=False); print(s.primary_user); s.close()' "$HERE" "$ROOT")"
        if [ "$CURRENT" = "$USER_ID" ]; then
            "$PYTHON" "$HERE/mhcli.py" "${INIT_ARGS[@]}" >/dev/null
            ok "store exists; primary user $USER_ID, shown as $NAME"
        elif confirm "The store belongs to '$CURRENT'. Rename to '$USER_ID' (their tasks move with it)?"; then
            "$PYTHON" "$HERE/mhcli.py" "${INIT_ARGS[@]}" | sed 's/^/  /'
        else
            warn "left the store's primary user as $CURRENT"
        fi
    else
        "$PYTHON" "$HERE/mhcli.py" "${INIT_ARGS[@]}" | sed 's/^/  /'
    fi

    "$HERE/install-mh.sh" "$ROOT" >/dev/null
    ok "installed $ROOT/operations/mh and mh-reference.md"

    BIN="$HOME/.local/bin"
    if confirm "Put mh on your PATH (symlink in $BIN)?"; then
        mkdir -p "$BIN"
        ln -sf "$ROOT/operations/mh" "$BIN/mh"
        ok "linked $BIN/mh"
        case ":$PATH:" in
            *":$BIN:"*) ;;
            *) warn "$BIN is not on your PATH. Add to ~/.zshrc:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
        esac
    fi
fi

# ---------------------------------------------------------------------------
if [ "$DO_HOOK" = 1 ]; then
    say "Claude Code hook"
    echo "  Adds a SessionStart hook that shows Claude the dashboard's todo list"
    echo "  for the tab it starts in. Edits ~/.claude/settings.json (backed up first)."
    if confirm "Install it?"; then
        install -m 755 "$HERE/hooks/load-session-todo.sh" "$STATE_DIR/load-session-todo.sh"
        "$PYTHON" - "$HOME/.claude/settings.json" "$STATE_DIR/load-session-todo.sh" <<'PY'
import json, shutil, sys
from pathlib import Path
path, command = Path(sys.argv[1]), sys.argv[2]
settings = {}
if path.exists():
    shutil.copy2(path, path.with_name(path.name + ".bak"))
    settings = json.loads(path.read_text() or "{}")
starts = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
if any(h.get("command") == command
       for group in starts for h in group.get("hooks", [])):
    print("  already present")
else:
    starts.append({"hooks": [{"type": "command", "command": command,
                              "timeout": 5}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print("  added")
PY
        ok "hook: $STATE_DIR/load-session-todo.sh"
    fi
fi

# ---------------------------------------------------------------------------
if [ "$DO_AUTOSTART" = 1 ]; then
    say "Start at login"
    AGENTS="$HOME/Library/LaunchAgents"
    if ls "$AGENTS"/com.mellonhead.claude-sessions.plist >/dev/null 2>&1; then
        warn "com.mellonhead.claude-sessions already runs a server here; not adding another."
    elif confirm "Start the dashboard at login and open it in the browser?"; then
        mkdir -p "$AGENTS"
        cat > "$AGENTS/$LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$HERE/server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$HERE</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$STATE_DIR/server.log</string>
    <key>StandardErrorPath</key>
    <string>$STATE_DIR/server.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(dirname "$PYTHON"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
EOF
        cat > "$AGENTS/$LABEL.browser.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL.browser</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>for i in 1 2 3 4 5 6 7 8 9 10; do if curl -s -o /dev/null -w "%{http_code}" http://localhost:$PORT/ 2>/dev/null | grep -q 200; then open "http://localhost:$PORT"; exit 0; fi; sleep 1; done</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF
        for plist in "$LABEL" "$LABEL.browser"; do
            launchctl bootout "gui/$UID/$plist" 2>/dev/null || true
            launchctl bootstrap "gui/$UID" "$AGENTS/$plist.plist"
        done
        ok "LaunchAgents $LABEL and $LABEL.browser loaded"
    fi
fi

# ---------------------------------------------------------------------------
say "Done"
cat <<EOF
  Dashboard:  http://localhost:$PORT
  Start it by hand:  $HERE/launch.command

  The first time the dashboard reads iTerm2, macOS asks whether your terminal
  (or python3, under launchd) may control iTerm2. Allow it, or no sessions
  show up. Change it later in System Settings > Privacy & Security > Automation.
EOF
if [ "$DO_STORE" = 1 ]; then
cat <<EOF

  Add your first project:
    cd "$ROOT" && ./operations/mh project add <key> "<Name>" --dir <key>
  mh finds the store by walking up from the current directory, so run it
  inside $ROOT or export MELLONHEAD_ROOT="$ROOT".
EOF
fi
