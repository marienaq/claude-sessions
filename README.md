# Claude Session Manager

A local dashboard for managing multiple Claude Code conversations running in iTerm2. Built as a single Python file with zero external dependencies.

## What it does

When you're running 15+ Claude Code sessions across iTerm2 tabs, it becomes hard to remember what each one is doing, which need your attention, and what to work on next. This tool gives you a browser-based dashboard that:

- **Shows all active sessions** with their status, working directory, and what they're doing
- **Navigates to any session** by clicking its card (activates the correct iTerm2 tab and window)
- **Tracks per-session todo lists** so each conversation has its own steps, visible both from the dashboard and inside Claude Code
- **Shows weekly priorities** from `priorities.md`, with clickable checkboxes that update the file
- **Links sessions to Notion tasks** via a two-step picker (project -> task), connecting conversations to your project management
- **Color-codes sessions by state**: waiting on AI (green), ready for you (blue), needs review (orange), blocked (grey)
- **Groups by priority**: Today, This Week, Ongoing, Next Week, Later
- **Supports dark mode**

## Quick start

```bash
# Start the server
python3 ~/Projects/claude-sessions/server.py

# Or double-click the desktop shortcut
open ~/Desktop/Claude\ Sessions.command
```

Then open http://localhost:7433 in your browser.

## Auto-start on login

Two LaunchAgents handle this:

- `~/Library/LaunchAgents/com.mellonhead.claude-sessions.plist` — starts the server at login, auto-restarts if it crashes. Logs to `~/.claude-manager/server.log` and `server.err.log`.
- `~/Library/LaunchAgents/com.mellonhead.claude-sessions-browser.plist` — one-shot at login that waits for the server to be ready, then opens the browser.

**Useful commands:**

```bash
# Check status
launchctl list | grep mellonhead

# Stop server (until next login)
launchctl unload ~/Library/LaunchAgents/com.mellonhead.claude-sessions.plist

# Start it back up
launchctl load -w ~/Library/LaunchAgents/com.mellonhead.claude-sessions.plist

# Restart server (e.g. after editing server.py)
launchctl kickstart -k gui/$UID/com.mellonhead.claude-sessions

# Tail logs
tail -f ~/.claude-manager/server.err.log

# Disable auto-start permanently
launchctl unload -w ~/Library/LaunchAgents/com.mellonhead.claude-sessions.plist
launchctl unload -w ~/Library/LaunchAgents/com.mellonhead.claude-sessions-browser.plist
```

## Architecture

### Single-file, zero dependencies

The entire application is one Python file (`server.py`, ~2800 lines) using only the Python 3 standard library. No npm, no pip, no build step. The HTML, CSS, and JavaScript are embedded in the Python file as a template string.

**Why:** Minimizes maintenance burden and deployment complexity. `python3 server.py` is all you need.

### Data flow

```
iTerm2 (AppleScript)  --->  /api/sessions  --->  Browser UI
~/.claude/sessions/   ---/      |
                           task-list.md files
                           priorities.md
                           ~/.claude-manager/
                              sessions.json (tags, renames, priorities, assignments)
                              todos/{itermId}.md (per-session todo lists)
                              todos/index.json (PID/sessionId -> itermId mapping)

Browser click  --->  /api/navigate  --->  AppleScript  --->  iTerm2 focus
```

### Session discovery

On each poll (every 5 seconds), the server:

1. Runs AppleScript to get all iTerm2 sessions (name, tty, session ID)
2. Runs `ps` to get Claude process PIDs, TTYs, CPU usage, and state
3. Reads `~/.claude/sessions/{pid}.json` for each process to get CWD and Claude session ID
4. **Joins on TTY** — this is the stable key connecting iTerm2 tabs to Claude processes
5. Reads per-session todo files and metadata from `~/.claude-manager/`
6. Updates the todo index so Claude Code hooks can find their session's todo file

### Key design decisions

**TTY as the join key.** iTerm2 sessions, Claude processes, and the terminal are all connected by the TTY device. PIDs change when sessions restart. iTerm session IDs are stable per tab but not known to Claude Code. The TTY bridges both worlds.

**iTerm session ID for persistence.** Tags, renames, priorities, todo files, and task assignments are all keyed by the iTerm session UUID. This is stable for the lifetime of a tab and unique across all tabs. PIDs can be recycled by the OS.

**Per-session todos over project-level task lists.** Project task lists (`task-list.md`) are useful for tracking what needs to happen across a project, but most sessions are opened from broad directories like `~/Projects/mellonhead`. A session-level todo captures "what am I actually doing in this specific conversation" — the granular steps, not the project overview.

**Explicit task linking over directory-based discovery.** Early versions tried to auto-discover `task-list.md` files by walking up/down from the CWD. This failed because most sessions share the same CWD. The two-step picker (project -> task) gives you explicit control.

**Manual priority mapping over automatic.** Auto-mapping weekly priorities to sessions ran on every page load, which fought with manual changes. The "Map to sessions" button runs it once on demand. Cleared priorities are stored as `__cleared__` so auto-mapping doesn't override them.

**Light theme with dark mode toggle.** The dashboard started dark but was hard to read with the state color coding. Light theme provides better contrast for the blue/orange/grey state backgrounds. Dark mode is available via toggle and persists in localStorage.

## File structure

```
~/Projects/claude-sessions/
  server.py              # The entire application
  launch.command         # Double-clickable macOS shortcut
  README.md              # This file

~/.claude-manager/
  sessions.json          # Persisted state: tags, renames, priorities, task assignments
  todos/
    {itermId}.md         # Per-session todo list with Notion metadata
    index.json           # Maps Claude PIDs and session IDs to iTerm session IDs
  load-session-todo.sh   # Claude Code hook script

~/Desktop/
  Claude Sessions.command  # Symlink to launch.command
```

## Session states

| State | Color | Meaning | Detection |
|-------|-------|---------|-----------|
| Waiting on AI | Green (pulsing) | Claude process is actively running | CPU > 1% or process state = R |
| MQ has next step | Blue | Claude is idle, task not blocked | Default when idle |
| Needs review | Orange | You need to do offline work before prompting again | Manual toggle (circle icon on card) |
| Blocked | Grey | Waiting on someone else | Task status is "On hold" or "Blocked" in task-list.md |
| Inactive | Grey, dashed border | Conversation exists on disk but no live tab | No matching iTerm session for the Claude session UUID |

## Recovering sessions after reboot

When iTerm tabs close (reboot, accidental quit, etc.), Claude Code conversations are still on disk at `~/.claude/projects/<flattened-cwd>/<uuid>.jsonl`. The dashboard surfaces every conversation modified in the last 30 days as an **inactive card**, alongside live sessions in the same priority groups.

- **Click an inactive card** → opens a new iTerm tab, `cd`s to the original directory, runs `claude --resume <uuid>`. On the next poll the card flips from inactive to a live state.
- **× on any card archives the conversation.** On an inactive card it hides it from the dashboard. On a live card it kills the process, closes the iTerm tab, AND archives — so it won't reappear as an inactive card later. Stored in `sessions.json` under `archivedSessions`.
- **State is keyed by Claude session ID** (the jsonl UUID), so tags, priorities, todos, and task links survive reboot for any session that has been live in the dashboard at least once. Live sessions lazily migrate from iTerm-id-keyed state to Claude-session-id-keyed state on each poll.

The discovery walks `~/.claude/projects/*/*.jsonl`, reads the first 16KB to regex out `"cwd":"..."`, and falls back to decoding the flattened directory name. Live session IDs are excluded so a live and its own jsonl don't double up.

## Card features

Each session card shows:

- **Priority label** — click to pick Today / This Week / Next Week / Later
- **Session name** — double-click to rename
- **Working directory** — double-click to override (for sessions opened from a parent dir)
- **Tags** — auto-generated from directory path + manual tags
- **Next step** — from session todo (priority), manual override, or project task list
- **Session todos** — expandable per-session checklist, add/check/edit/remove inline
- **Notion link** — project and task links when assigned
- **Review toggle** — circle icon to flag for offline review
- **Close button** — sends SIGINT and closes the iTerm2 tab

## Task view

An **In flight** strip sits between the week and the session cards: every
open task with a brief, a question for MQ, an agent at work, or a live
conversation, ordered by when it bites. Clicking a chip, a day-card task
name, a Projects-panel next action, or a card's linked task opens a popup
for that task (`#task/<id>`, so it can be bookmarked): the top of the
brief, the open questions with Accept / Answer, the conversations on the
task (click to focus or resume; "+ link a conversation" attaches one), and
what agents have out and have done. The circle on a day-card line, or in
the popup header, checks the task off.

**Work on this with Orca** resumes the conversation linked to the task, or
failing that the most recent interactive one whose `mh` writes touched it
(scheduled sweeps and capture-only writes are skipped), or opens a fresh
`claude --agent orca`. Design and plan: `operations/ai-workflows/task-view/`
in the content repo.

The store behind it (`mhstore.py`) records every `mh` write as an event
stamped with the Claude conversation that made it (`mhsession.py` finds the
session from the process tree), holds questions with a proposed answer and
who they block, and derives dispatch-readiness rather than storing it.

## Weekly priorities

The priorities bar at the top reads from `~/Projects/mellonhead/priorities.md`. It shows day-by-day goals with clickable checkboxes. Completed days are hidden automatically. Checking an item updates the markdown file and appends the completion date.

The "Map to sessions" button runs fuzzy keyword matching (or exact Notion ID matching) to auto-assign Today/This Week priorities to sessions.

## Claude Code integration

A `SessionStart` hook (`~/.claude-manager/load-session-todo.sh`) runs when a new Claude Code conversation starts. It:

1. Finds the Claude process PID
2. Looks up the iTerm session ID via `~/.claude-manager/todos/index.json`
3. Reads the session's todo file
4. Outputs it so Claude sees the todo context

This means Claude knows what steps have been done and what's next when you resume a conversation. Claude can update the todo file directly since it's just a markdown file at a known path.

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/sessions` | All session data (sessions, priorities, color groups) |
| GET | `/api/task-files` | List all task-list.md files |
| POST | `/api/navigate` | Activate an iTerm2 tab |
| POST | `/api/todo` | CRUD for session todos (add, check, edit, remove, reorder) |
| POST | `/api/link-task` | Link session to a project + specific task |
| POST | `/api/task-tasks` | Get tasks from a specific task-list.md |
| POST | `/api/create-task` | Add a new task to a task-list.md |
| POST | `/api/complete-task` | Mark a task done in task-list.md |
| POST | `/api/tag` | Add/remove manual tags |
| POST | `/api/rename` | Rename a session |
| POST | `/api/set-cwd` | Override a session's working directory |
| POST | `/api/set-next-step` | Manual next step override |
| POST | `/api/set-review` | Toggle needs-review flag |
| POST | `/api/priority` | Set session priority |
| POST | `/api/map-priorities` | Auto-map weekly priorities to sessions |
| POST | `/api/color` | Assign color to a tag group |
| POST | `/api/close` | Close a session (SIGINT + close tab) |
| POST | `/api/toggle-priority-item` | Check/uncheck a weekly priority item |
| POST | `/api/resume` | Open new iTerm tab and run `claude --resume <id>` |
| POST | `/api/archive` | Hide an inactive conversation from the dashboard |
| GET | `/api/tasks` | In-flight tasks: open rows with a brief, an open question, an open dispatch, or a live conversation |
| GET | `/api/task/<id>` | One task's page: task, brief top, questions, conversations, open dispatches, last 50 events |
| POST | `/api/question/answer` | Record MQ's answer to a question (`actor=mq`, `source=dashboard`) |
| POST | `/api/question/accept` | Answer a question with its proposed answer |
| POST | `/api/task/link` | Link a conversation to a store task; records a `link` event |
| POST | `/api/task/orca` | Resume the conversation that last touched the task, or open Orca on it |
