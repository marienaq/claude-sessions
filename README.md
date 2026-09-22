# Claude Session Manager

A local dashboard for managing multiple Claude Code conversations running in iTerm2, with an optional task store and CLI (`mh`) behind it. Python standard library only.

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

## Requirements

- **macOS.** Sessions are found through AppleScript.
- **iTerm2.** The dashboard only sees Claude Code sessions running in iTerm2
  tabs. Sessions in Terminal.app, Warp, VS Code or an IDE do not appear.
- **Python 3.11 or newer.** The stock `/usr/bin/python3` on macOS is too old;
  `brew install python` is the usual fix. Nothing to `pip install`, and no
  database to set up: the task store is SQLite, which ships inside Python.
  (A pyenv Python built without SQLite headers lacks it; the installer
  checks.)
- **Claude Code** (`claude`) on your PATH, for resuming conversations and
  opening new ones from the dashboard.

## Install

```bash
git clone https://github.com/marienaq/claude-sessions.git ~/Projects/claude-sessions
cd ~/Projects/claude-sessions
./install.sh
```

The installer asks four things, with a sensible default for each:

| Question | What it is for |
|---|---|
| **User id** (e.g. `alex`) | You, as the task store knows you. Tasks you own, and everything you do on the dashboard, carry it. Lower case, no spaces. |
| **Display name** (e.g. `Alex`) | How the dashboard and the generated markdown refer to you ("Alex has next step"). |
| **Other names** | Spellings an agent might use when handing a task to you (a full name, initials). They fold to your id. |
| **Workspace directory** | Where your `priorities.md`, per-project `task-list.md` files and the task store (`operations/tasks.db`) live. Any directory; it is created if missing. |

It then offers, each one optional:

1. **Create the task store** and install the `mh` CLI into the workspace
   (`<workspace>/operations/mh`), with a symlink at `~/.local/bin/mh`.
2. **Add a Claude Code SessionStart hook** so Claude sees the dashboard's
   todo list for the tab it starts in. Edits `~/.claude/settings.json`,
   backing it up to `settings.json.bak` first.
3. **Start at login**: two LaunchAgents, one for the server and one that
   opens the browser once the server is up.

Non-interactive: `./install.sh --user alex --name Alex --root ~/Projects/work --yes`.
`--no-store`, `--no-hook` and `--no-autostart` skip steps; `--help` lists
everything. Re-running is safe: it detects what is already set up.

### First run

Open http://localhost:7433, or double-click `launch.command` if you skipped
auto-start. The first time the server talks to iTerm2, **macOS asks whether
it may control iTerm2. Allow it**, or no sessions appear. If you missed the
prompt: System Settings → Privacy & Security → Automation.

The session cards work immediately. The task side (the **In flight** strip,
the projects panel, the week) fills in as you add to the store:

```bash
cd ~/Projects/work                      # your workspace
mh project add website "Website redesign" --dir website
mh task add website "Draft the new nav" --day 2026-09-24
mh task next website
```

`mh` finds the store by walking up from the current directory, so run it
inside the workspace, or `export MELLONHEAD_ROOT=~/Projects/work` in your
shell profile. The full command list is `operations/mh-reference.md` in the
workspace, generated for your user, and `mh <group> --help`.

### Dashboard only

`./install.sh --no-store` sets up the session cards, todos and resume without
the task store. Or skip the installer entirely: `python3 server.py` (3.11+)
and open http://localhost:7433.

## Configuration

`install.sh` writes `~/.claude-manager/config.json`:

```json
{ "root": "/Users/alex/Projects/work", "port": 7433 }
```

Optional keys: `agent`, the Claude Code agent that "Work on this" opens
(default `orca`, used only if `~/.claude/agents/<agent>.md` or the
workspace's `.claude/agents/<agent>.md` exists; otherwise plain `claude`).

Environment variables override the file:

| Variable | Default | Meaning |
|---|---|---|
| `MELLONHEAD_ROOT` | `root` from config, else `~/Projects/mellonhead` | the workspace |
| `CSM_PORT` | `port` from config, else 7433 | dashboard port |
| `CSM_STATE_DIR` | `~/.claude-manager` | dashboard state and config |
| `CSM_AGENT` | `agent` from config, else `orca` | agent for "Work on this" |
| `CSM_PYTHON` | first 3.11+ found | the Python every script uses |
| `MH_CODE` | the checkout `install-mh.sh` ran from | where `mh` finds `mhcli.py` |

The primary user lives in the store, not the config: change it with
`mh init --user <id> --name <Name>`. Tasks owned by the old id move to the
new one.

## Auto-start on login

`install.sh` creates two LaunchAgents:

- `~/Library/LaunchAgents/com.claude-sessions.plist` starts the server at
  login and restarts it if it crashes. Logs go to `~/.claude-manager/server.log`
  and `server.err.log`.
- `~/Library/LaunchAgents/com.claude-sessions.browser.plist` waits for the
  server at login, then opens the browser.

```bash
launchctl list | grep claude-sessions                      # status
launchctl kickstart -k gui/$UID/com.claude-sessions        # restart, e.g. after git pull
tail -f ~/.claude-manager/server.err.log                   # logs

# Remove auto-start
launchctl bootout gui/$UID/com.claude-sessions
launchctl bootout gui/$UID/com.claude-sessions.browser
rm ~/Library/LaunchAgents/com.claude-sessions*.plist
```

## Updating

```bash
cd ~/Projects/claude-sessions && git pull
launchctl kickstart -k gui/$UID/com.claude-sessions
./install-mh.sh ~/Projects/work      # refreshes the mh shim and its reference
```

## Uninstalling

Remove the LaunchAgents (above), the SessionStart entry pointing at
`~/.claude-manager/load-session-todo.sh` from `~/.claude/settings.json`,
`~/.local/bin/mh`, and `~/.claude-manager/`. The workspace is yours: its
markdown is readable without any of this, and `mh export` writes a plain
copy of the store.

## Things that assume one particular setup

The dashboard grew out of one person's workflow, and a few features still
expect pieces that live outside this repo:

- **"Work on this with Orca" / "Review with Orca"** open `claude --agent orca`
  with prompts that call skills such as `/scope-and-start`. Without an
  `orca` agent the buttons read "with Claude" and open plain `claude`; the
  prompt still asks for those skills, which you will not have.
- **Weekly priorities** come from the store's weeks (`mh plan propose`,
  `mh plan lock`), or failing that from a hand-written `priorities.md` in
  the workspace. The "Commitment Rules" section mentioned in the week-review
  prompt is a convention of that workspace, not something the tool creates.
- **Notion links** on tasks are optional ids; nothing calls Notion.

## Architecture

### Zero dependencies

The dashboard is one Python file (`server.py`) using only the standard library, with the HTML, CSS and JavaScript embedded as a template string. The task store and CLI are a handful of sibling modules (`mhstore.py`, `mhcli.py`, `mhgen.py`, `mhsession.py`), also stdlib only. No npm, no pip, no build step.

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
claude-sessions/                 # this repo
  server.py              # the dashboard
  mhstore.py             # the task store (SQLite)
  mhcli.py               # mh, the CLI agents write through
  mhgen.py               # regenerates task-list.md, priorities.md, the projects dashboard
  mhsession.py           # finds the Claude conversation a write came from
  mhmigrate.py           # one-off import of hand-written task lists into the store
  mh                     # shell entry point, copied into the workspace by install-mh.sh
  mh-reference.md        # generated by `mh docs`; a test keeps it in step
  install.sh             # per-person setup
  install-mh.sh          # (re)installs mh into a workspace
  find-python.sh         # finds a 3.11+ python for the scripts
  launch.command         # double-clickable start
  hooks/load-session-todo.sh   # the SessionStart hook install.sh installs
  dev-server.sh, refresh-dev-copy.sh, backfill_task_view.py   # maintainer only, see below

~/.claude-manager/
  config.json            # workspace, port (written by install.sh)
  sessions.json          # tags, renames, priorities, task assignments
  todos/
    {id}.md              # per-session todo list
    index.json           # maps Claude PIDs and session IDs to iTerm session IDs
  load-session-todo.sh   # the installed hook

<workspace>/
  priorities.md          # generated from the store's weeks
  <project>/task-list.md # generated task tables; prose around them is yours
  operations/
    tasks.db             # the store
    tasks-audit.log      # every write, one JSON line each
    mh, mh-reference.md  # installed by install-mh.sh
```

## Session states

| State | Color | Meaning | Detection |
|-------|-------|---------|-----------|
| Waiting on AI | Green (pulsing) | Claude process is actively running | CPU > 1% or process state = R |
| *Name* has next step | Blue | Claude is idle, task not blocked | Default when idle |
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

The priorities bar at the top reads from `priorities.md` in the workspace. It shows day-by-day goals with clickable checkboxes. Completed days are hidden automatically. Checking an item updates the markdown file and appends the completion date.

The "Map to sessions" button runs fuzzy keyword matching (or exact Notion ID matching) to auto-assign Today/This Week priorities to sessions.

## Claude Code integration

A `SessionStart` hook (`hooks/load-session-todo.sh`, installed to `~/.claude-manager/` by `install.sh`) runs when a new Claude Code conversation starts. It:

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

## Maintainer notes

These describe the original deployment and are not needed to run the tool.

- The original install predates `install.sh` and uses the LaunchAgent labels
  `com.mellonhead.claude-sessions` and `com.mellonhead.claude-sessions-browser`,
  running straight from the main checkout. `install.sh` sees them and does not
  add a second server.
- `dev-server.sh` serves a checkout on 7434 against `~/Projects/mellonhead-dev`,
  a git-less text copy of the live workspace that `refresh-dev-copy.sh` builds.
  Use it with a worktree and `MH_CODE=<worktree>` so unmerged code never opens
  the live store: opening a store migrates its schema.
- A store with no `primary_user` setting is MQ's: it reads as `mq` / "MQ",
  with "Mariena" folding to `mq`. `mh init --user` sets it explicitly.
- Tests: `python3 -m unittest discover -s tests`. After changing any `mh`
  verb, regenerate the reference with `env -u MELLONHEAD_ROOT python3 mhcli.py docs > mh-reference.md`.
