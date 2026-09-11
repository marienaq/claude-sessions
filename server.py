#!/usr/bin/env python3
"""
Claude Session Manager — a local dashboard for managing Claude Code conversations.
Zero dependencies: Python 3 stdlib only.
"""

import http.server
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import mhstore
    import mhgen
except ImportError:                     # store not installed; markdown only
    mhstore = None
    mhgen = None

# Everything below can be pointed at a scratch copy so a dev instance never
# touches live state. See operations/prioritization-implementation-plan.md 5.7.
#   MELLONHEAD_ROOT  the content repo to read (priorities.md, task lists, store)
#   CSM_STATE_DIR    session-manager state (sessions.json, todos/)
#   CSM_PORT         listen port; --port on the command line wins
PORT = int(os.environ.get("CSM_PORT", "7433"))
MELLONHEAD_ROOT = Path(
    os.environ.get("MELLONHEAD_ROOT", Path.home() / "Projects" / "mellonhead")
).expanduser()
STATE_DIR = Path(
    os.environ.get("CSM_STATE_DIR", Path.home() / ".claude-manager")
).expanduser()

SESSIONS_FILE = STATE_DIR / "sessions.json"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
PRIORITIES_FILE = MELLONHEAD_ROOT / "priorities.md"
STORE_FILE = MELLONHEAD_ROOT / "operations" / "tasks.db"
TODOS_DIR = STATE_DIR / "todos"
TODOS_INDEX = TODOS_DIR / "index.json"
INACTIVE_WINDOW_DAYS = 30
INACTIVE_MAX = 200
STATE_CATEGORIES = (
    "tags", "renames", "priorities", "nextSteps", "cwdOverrides",
    "needsReview", "taskAssignments", "taskLinks",
)

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The task store
#
# The store is the record. The markdown readers below it are kept as a
# fallback for the dual-live window and for a rollback: if tasks.db is absent
# or holds no week, the dashboard still renders from priorities.md rather than
# going blank. See the plan's cutover steps 4 and 6.
# ---------------------------------------------------------------------------

_STORE = None
_STORE_FILE_ID = None


def get_store():
    """
    Open the store once per process, and reopen it if the file underneath has
    been replaced.

    A long-lived handle keeps writing to the old inode after tasks.db is
    swapped out — by a restore, a migration that recreates it, or a rebuilt
    working copy. SQLite reports success, the audit log records the write
    because it reopens by path, and the row never changes. Three checkbox
    clicks were lost that way, with an audit trail saying they happened.
    """
    global _STORE, _STORE_FILE_ID
    if mhstore is None:
        return None
    try:
        info = STORE_FILE.stat()
        file_id = (info.st_dev, info.st_ino)
    except OSError:
        return None

    if _STORE is not None and _STORE_FILE_ID != file_id:
        print("tasks.db was replaced underneath us; reopening")
        try:
            _STORE.close()
        except Exception:
            pass
        _STORE = None

    if _STORE is None:
        try:
            _STORE = mhstore.open_store(root=MELLONHEAD_ROOT,
                                        seed_settings=False)
            _STORE_FILE_ID = file_id
        except Exception as exc:
            print(f"store unavailable, falling back to markdown: {exc}")
            return None
    return _STORE


def store_is_live():
    """True when the store holds a week worth rendering."""
    store = get_store()
    if store is None:
        return False
    try:
        return bool(store.conn.execute(
            "SELECT 1 FROM tasks WHERE planned_day IS NOT NULL LIMIT 1"
        ).fetchone())
    except Exception:
        return False


def regenerate_views(project_keys=()):
    """
    Invariant 4: a write regenerates the views it affects, in the same call.

    The CLI already did this; the dashboard did not, so a checkbox updated the
    store and left task-list.md stale. `mh verify` caught three files that way.
    Measured at about 4ms for a task list, priorities.md and the dashboard
    together, which is far too cheap to skip.

    Never let a generation failure break the write that already succeeded.
    """
    store = get_store()
    if store is None or mhgen is None:
        return
    try:
        for key in {k for k in project_keys if k and k != mhstore.ONE_OFF}:
            mhgen.generate_task_list(store, MELLONHEAD_ROOT, key)
        mhgen.generate_priorities(store, MELLONHEAD_ROOT)
        mhgen.generate_dashboard(store, MELLONHEAD_ROOT)
    except Exception as exc:
        print(f"regeneration failed after a write: {exc}")


def load_store():
    if SESSIONS_FILE.exists():
        with open(SESSIONS_FILE) as f:
            return json.load(f)
    return {"tags": {}, "history": [], "color_groups": {}}


def save_store(store):
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSIONS_FILE, "w") as f:
        json.dump(store, f, indent=2)


# ---------------------------------------------------------------------------
# iTerm2 discovery via AppleScript
# ---------------------------------------------------------------------------

ITERM_SCRIPT = """
tell application "iTerm2"
    set output to ""
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                try
                    set tabColor to ""
                    try
                        if (background color of s) is not missing value then
                            set {r, g, b} to background color of s
                            set tabColor to (r as text) & "," & (g as text) & "," & (b as text)
                        end if
                    end try
                    set output to output & (name of s) & "\\t" & (tty of s) & "\\t" & (id of s) & "\\t" & tabColor & linefeed
                end try
            end repeat
        end repeat
    end repeat
    return output
end tell
"""


def get_iterm_sessions():
    try:
        result = subprocess.run(
            ["osascript", "-e", ITERM_SCRIPT],
            capture_output=True, text=True, timeout=5
        )
        sessions = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                sessions.append({
                    "name": parts[0],
                    "tty": parts[1],
                    "itermId": parts[2],
                    "color": parts[3] if len(parts) > 3 else "",
                })
        return sessions
    except Exception as e:
        print(f"iTerm2 AppleScript error: {e}")
        return []


# ---------------------------------------------------------------------------
# Claude process discovery
# ---------------------------------------------------------------------------

def get_claude_processes():
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,tty,state,%cpu,command"],
            capture_output=True, text=True, timeout=5
        )
        procs = []
        for line in result.stdout.strip().split("\n")[1:]:
            parts = line.split()
            # Match the command name (parts[4]) not the last arg —
            # `claude --resume <uuid>` would otherwise have parts[-1] = uuid.
            if len(parts) >= 5 and (parts[4] == "claude" or parts[4].endswith("/claude")):
                procs.append({
                    "pid": int(parts[0]),
                    "tty": "/dev/" + parts[1] if not parts[1].startswith("/") else parts[1],
                    "state": parts[2],
                    "cpu": float(parts[3]),
                })
        return procs
    except Exception as e:
        print(f"Process discovery error: {e}")
        return []


def get_session_file(pid):
    path = CLAUDE_SESSIONS_DIR / f"{pid}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


# ---------------------------------------------------------------------------
# Task-list.md parser
# ---------------------------------------------------------------------------

def find_task_list(cwd):
    """Walk up from cwd looking for task-list.md, then search immediate children."""
    current = Path(cwd)
    home = Path.home()
    # Walk up
    check = current
    while check != home and check != check.parent:
        task_file = check / "task-list.md"
        if task_file.exists():
            return task_file
        check = check.parent
    # Search downward (2 levels max) from original cwd
    for depth1 in current.iterdir():
        if depth1.is_dir():
            tf = depth1 / "task-list.md"
            if tf.exists():
                return tf
            try:
                for depth2 in depth1.iterdir():
                    if depth2.is_dir():
                        tf2 = depth2 / "task-list.md"
                        if tf2.exists():
                            return tf2
            except PermissionError:
                continue
    return None


def project_for_cwd(cwd):
    """
    The store project whose directory contains `cwd`, deepest match first.

    Replaces walking the filesystem for a task-list.md: the store knows which
    directory belongs to which project.
    """
    store = get_store()
    if store is None or not cwd:
        return None
    try:
        target = Path(cwd).resolve()
    except OSError:
        return None

    # A dev instance runs against a copy of the repo while the sessions
    # themselves are still working in the real one, so no cwd would ever match
    # a project directory. CSM_CWD_ALIAS="<live>:<copy>" rewrites the prefix so
    # the panel can be exercised before cutover. Unset in production.
    alias = os.environ.get("CSM_CWD_ALIAS", "")
    if ":" in alias:
        src, _, dst = alias.partition(":")
        src, dst = src.rstrip("/"), dst.rstrip("/")
        if str(target) == src or str(target).startswith(src + os.sep):
            target = Path(dst + str(target)[len(src):])
    best, best_len = None, -1
    for project in store.projects():
        if not project["dir"]:
            continue
        base = (MELLONHEAD_ROOT / project["dir"]).resolve()
        if target == base or str(target).startswith(str(base) + os.sep):
            if len(str(base)) > best_len:
                best, best_len = project, len(str(base))
    return best


def task_list_from_store(project_key):
    """The task panel's payload, read from the store instead of markdown."""
    store = get_store()
    if store is None:
        return None
    project = store.project(project_key)
    if project is None:
        return None
    rows = store.tasks(project_key=project_key)
    result = {
        "file": str(MELLONHEAD_ROOT / project["dir"]) if project["dir"] else "",
        "projectKey": project["key"],
        "projectName": project["name"],
        "notionProjectId": project["notion_project_id"] or "",
        "status": project["status"],
        "source": "store",
        "tasks": [{
            "id": row["id"],
            "number": row["display_ord"] or row["id"],
            "title": row["title"],
            "status": row["status"],
            "statusRaw": row["status_raw"] or row["status"],
            "notionTaskId": row["notion_task_id"] or "",
            "notes": row["notes"] or "",
            "notePath": row["note_path"],
            "owner": row["owner"],
            "seq": row["seq"],
            "due": row["due"],
            "section": row["section"],
        } for row in rows],
    }
    nxt = store.next_task(project_key)
    if nxt:
        result["nextStep"] = next(
            (t for t in result["tasks"] if t["id"] == nxt["id"]), None)
    return result


def parse_task_list(path):
    with open(path) as f:
        content = f.read()

    result = {"file": str(path), "tasks": [], "projectName": "", "notionProjectId": ""}

    # Extract project name from first heading
    m = re.search(r"^#\s+(.+)", content, re.MULTILINE)
    if m:
        result["projectName"] = m.group(1).strip()

    # Extract Notion project ID
    m = re.search(r"Notion Project ID[:\s]*`?([a-f0-9-]+)`?", content, re.IGNORECASE)
    if m:
        result["notionProjectId"] = m.group(1)

    # Parse markdown table rows
    table_pattern = re.compile(
        r"\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*`?([^|`]*)`?\s*\|\s*(.*?)\s*\|"
    )
    for match in table_pattern.finditer(content):
        task = {
            "number": int(match.group(1)),
            "title": match.group(2).strip(),
            "status": match.group(3).strip(),
            "notionTaskId": match.group(4).strip().strip("`"),
            "notes": match.group(5).strip(),
        }
        result["tasks"].append(task)

    # Find next step — prioritize "Not started" tasks with urgency signals
    not_started = [t for t in result["tasks"] if t["status"].lower() == "not started"]
    if not_started:
        # Check notes for urgency: "today", "start now", "urgent", "this week", dates
        urgent_keywords = ["today", "start now", "urgent", "asap", "priority", "this week"]
        urgent = [t for t in not_started
                  if any(kw in t["notes"].lower() for kw in urgent_keywords)]
        result["nextStep"] = urgent[0] if urgent else not_started[0]

    return result


# ---------------------------------------------------------------------------
# Auto-tagging from directory path
# ---------------------------------------------------------------------------

def auto_tags_from_path(cwd):
    home = str(Path.home())
    rel = cwd.replace(home, "").strip("/")
    parts = rel.split("/")

    tags = []
    # Skip "Projects" as a tag
    meaningful = [p for p in parts if p.lower() != "projects"]
    for part in meaningful[:3]:  # client / project / area
        tag = part.replace("-", " ").replace("_", " ")
        if tag and len(tag) > 1:
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# Priorities parser
# ---------------------------------------------------------------------------

_WEEK_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%B %d", "%b %d")


def _parse_week_date(label):
    """Parse a Week-of label like 'May 18, 2026' or 'May 18 -- ATD' into a date."""
    import datetime
    s = re.split(r"\s+--\s+|\s+\(", label.strip())[0].strip()
    for fmt in _WEEK_DATE_FORMATS:
        try:
            d = datetime.datetime.strptime(s, fmt).date()
            if d.year == 1900:
                d = d.replace(year=datetime.date.today().year)
            return d
        except ValueError:
            continue
    return None


def _parse_week_section(content, match):
    """Parse one week's section text into days + blocked items."""
    section_start = match.end()
    # Stop at the next **Week of ...** header (real section markers only —
    # passing-reference labels ending in ':' are handled by the caller by
    # excluding them from the match set, but we still want to stop on any
    # `**Week of ...**` occurrence we hit here, since none of them belong
    # to the current section).
    next_match = re.search(r"\*\*Week of .+?\*\*", content[section_start:])
    if next_match:
        section_end = section_start + next_match.start()
    else:
        section_end = len(content)

    # Prefer parsing from the "## Weekly Goals" heading if present — some
    # weeks have a "## Week of X Review" block above the goals, separated
    # by a `---` divider. Without this we truncate the section before we
    # ever reach the day headers.
    goals_match = re.search(r"^##\s+Weekly Goals\b", content[section_start:section_end], re.MULTILINE)
    if goals_match:
        section_start = section_start + goals_match.end()

    # After we're inside Weekly Goals, stop at the next `## ` heading (a
    # sibling section like "## Week of July 20 -- In flight") or `---`.
    stopper = re.search(r"^##\s+|^---\s*$", content[section_start:section_end], re.MULTILINE)
    if stopper:
        section_end = section_start + stopper.start()

    section_text = content[section_start:section_end]

    days = []
    blocked = []
    current_day = None
    current_items = []

    def flush():
        if not current_day:
            return
        if "blocked" in current_day.lower() or "waiting" in current_day.lower():
            blocked.extend(current_items)
        else:
            days.append({"day": current_day, "items": current_items})

    for line in section_text.split("\n"):
        line = line.strip()
        day_match = re.match(r"^###\s+(.+)", line)
        if day_match:
            flush()
            current_day = day_match.group(1).strip()
            current_items = []
            continue

        task_match = re.match(
            r"^-\s*\[([ xX~])\]\s*(.+?)(?:\s*<!--\s*notion:(\S+?)\s*-->)?\s*$", line
        )
        if task_match and current_day:
            done = task_match.group(1).lower() in ("x", "~")
            current_items.append({
                "text": task_match.group(2).strip(),
                "done": done,
                "notionTaskId": task_match.group(3) or "",
            })

    flush()
    return days, blocked


DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")


def _week_start_for(day):
    """The Monday on or before `day`."""
    import datetime
    return day - datetime.timedelta(days=day.weekday())


def _priority_item(task):
    """One day-card line, in the shape the front end already renders."""
    tag = (f"{task['project_key']}#{task['display_ord']}"
           if task["display_ord"] else None)
    return {
        "id": task["id"],
        "text": task["title"],
        "done": task["status"] == "done",
        "key": tag,
        "project": task["project_key"],
        "load": task["load"],
        "owner": task["owner"],
        "notePath": task["note_path"],
    }


def _week_from_store(store, week_start):
    """Day cards for one week, Monday first, empty days dropped."""
    import datetime
    rows = store.conn.execute(
        """SELECT * FROM tasks
           WHERE planned_day >= ? AND planned_day <= date(?, '+6 day')
           ORDER BY planned_day, seq IS NULL, seq, id""",
        (week_start.isoformat(), week_start.isoformat())).fetchall()
    by_day = {}
    for row in rows:
        by_day.setdefault(row["planned_day"], []).append(dict(row))
    days = []
    for offset in range(7):
        day = week_start + datetime.timedelta(days=offset)
        items = by_day.get(day.isoformat(), [])
        if not items:
            continue
        days.append({
            "day": f"{DAY_NAMES[day.weekday()]} {day.month}/{day.day}",
            "date": day.isoformat(),
            "items": [_priority_item(t) for t in items],
        })
    return days


def load_priorities():
    """
    The current and next week, read from the store.

    Falls back to the markdown parser when the store has no week, so the
    dashboard keeps working during the dual-live window.
    """
    import datetime
    store = get_store()
    if store is None or not store_is_live():
        return parse_priorities_markdown()

    today = datetime.date.today()
    this_monday = _week_start_for(today)

    planned = [r["planned_day"] for r in store.conn.execute(
        "SELECT DISTINCT planned_day FROM tasks "
        "WHERE planned_day IS NOT NULL ORDER BY planned_day")]
    if not planned:
        return parse_priorities_markdown()

    starts = sorted({_week_start_for(datetime.date.fromisoformat(p))
                     for p in planned})
    current = [s for s in starts if s <= this_monday]
    if current:
        selected, has_current = [current[-1]], True
    else:
        selected, has_current = [starts[0]], False
    following = [s for s in starts if s > selected[0]]
    if following:
        selected.append(following[0])

    weeks = []
    for pos, start in enumerate(selected):
        row = store.week(start.isoformat()) or {}
        # The title is the date, always. weeks.notes holds the reasoning
        # behind a proposal, which the Friday job fills with a couple of
        # thousand words; using it as the heading rendered the whole
        # rationale as one uppercase wall across the top of the bar.
        notes = (row.get("notes") or "").strip()
        if notes == start.strftime("%B %-d, %Y"):
            notes = ""            # migration stored the label here
        weeks.append({
            "title": start.strftime("Week of %B %-d, %Y"),
            "weekStart": start.isoformat(),
            "days": _week_from_store(store, start),
            "isCurrent": has_current and pos == 0,
            "locked": bool(row.get("locked_at")),
            "proposed": bool(row.get("proposed_at") and not row.get("locked_at")),
            "notes": notes,
        })

    blocked = [_priority_item(dict(r)) for r in store.conn.execute(
        "SELECT * FROM tasks WHERE status = 'waiting' AND planned_day IS NULL "
        "ORDER BY project_key, id")]

    return {
        "weekTitle": weeks[0]["title"],
        "days": weeks[0]["days"],
        "blocked": blocked,
        "weeks": weeks,
        "noCurrentWeek": not has_current,
        "source": "store",
    }


def obsidian_url(rel_path):
    """
    obsidian:// link for a repo-relative file, so a note opens somewhere it
    can be edited rather than read-only in a browser.
    """
    if not rel_path:
        return None
    target = MELLONHEAD_ROOT / rel_path
    return "obsidian://open?path=" + urllib.parse.quote(str(target), safe="")


def load_panels():
    """Backlog, unconfirmed proposals, and the per-project next action."""
    store = get_store()
    if store is None or not store_is_live():
        return {"backlog": [], "proposed": [], "projects": [], "available": False}

    backlog, proposed = [], []
    for row in store.conn.execute(
            "SELECT * FROM tasks WHERE status = 'backlog' AND planned_day IS NULL "
            "ORDER BY project_key, seq IS NULL, seq, id"):
        item = _priority_item(dict(row))
        (backlog if row["confirmed"] else proposed).append(item)

    projects = []
    for project in store.projects():
        if project["status"] not in ("active", "waiting"):
            continue
        if project["key"] == mhstore.ONE_OFF:
            continue
        nxt = store.next_task(project["key"])
        open_count = len([t for t in store.tasks(project_key=project["key"],
                                                 open_only=True)])
        projects.append({
            "key": project["key"],
            "name": project["name"],
            "status": project["status"],
            "due": project["due"],
            "openCount": open_count,
            "next": _priority_item(nxt) if nxt else None,
            "notePath": obsidian_url(nxt["note_path"]) if nxt and nxt["note_path"] else None,
        })
    projects.sort(key=lambda p: (p["status"] != "active", p["key"]))
    return {"backlog": backlog, "proposed": proposed, "projects": projects,
            "available": True}


# ---------------------------------------------------------------------------
# The task view (operations/ai-workflows/task-view/design.md §5, plan §B)
#
# One page per task: the brief, the questions MQ has to answer, the
# conversations open on it, and what the agents did and will do next. All
# of it read from the store; nothing here parses prose except the top of
# the brief file.
# ---------------------------------------------------------------------------

_BRIEF_HEADER_LINE = re.compile(r"^\*\*([^*]+?):\*\*\s*(.*)$")


def parse_brief(rel_path):
    """
    The header block (`**Key:** value` lines above the first rule) and the
    `## Goal` and `## Deliverables` sections. Nothing else in the file is
    read: the whole brief is one click away in Obsidian, not on the page.
    """
    result = {"path": rel_path, "obsidianUrl": obsidian_url(rel_path),
              "exists": False, "title": "", "header": {}, "goal": "",
              "deliverables": ""}
    if not rel_path:
        return result
    path = MELLONHEAD_ROOT / rel_path
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return result
    result["exists"] = True
    lines = text.splitlines()
    for line in lines:
        if line.startswith("# "):
            result["title"] = line[2:].strip()
            break
    head = text.split("\n---", 1)[0]
    for line in head.splitlines():
        m = _BRIEF_HEADER_LINE.match(line.strip())
        if m:
            # Values are displayed as text, so bold and backticks come off.
            value = re.sub(r"\*\*|`", "", m.group(2)).strip()
            result["header"][m.group(1).strip()] = value

    def section(name):
        out, inside = [], False
        for line in lines:
            if re.match(r"^##\s+", line):
                if inside:
                    break
                inside = re.match(rf"^##\s+{name}\b", line, re.I) is not None
                continue
            if inside:
                out.append(line)
        return "\n".join(out).strip()

    result["goal"] = section("Goal")
    result["deliverables"] = section("Deliverables")
    return result


def _task_tag(task):
    if task["project_key"] != mhstore.ONE_OFF and task["display_ord"]:
        return f"{task['project_key']}#{task['display_ord']}"
    return f"#{task['id']}"


def _question_item(q, store):
    task = store.task(q["task_id"]) if q["task_id"] else None
    return {
        "id": q["id"], "taskId": q["task_id"], "projectKey": q["project_key"],
        "task": _task_tag(task) if task else None,
        "taskTitle": task["title"] if task else None,
        "text": q["text"], "proposed": q["proposed"], "blocks": q["blocks"],
        "askedBy": q["asked_by"], "askedAt": q["asked_at"],
        "status": q["status"], "answer": q["answer"],
    }


def _event_item(e):
    return {
        "id": e["id"], "ts": e["ts"], "actor": e["actor"], "kind": e["kind"],
        "summary": e["summary"], "agent": e["agent"], "verdict": e["verdict"],
        "artifactPath": e["artifact_path"],
        "artifactUrl": obsidian_url(e["artifact_path"]) if e["artifact_path"] else None,
        "sessionId": e["session_id"], "itermId": e["iterm_id"],
    }


# The board's session list is expensive (AppleScript, ps, a transcript scan)
# and already computed every five seconds for /api/sessions. The task page
# joins against that result rather than computing its own.
_SESSIONS_CACHE = {"at": 0, "sessions": []}
SESSIONS_CACHE_TTL = 10


def cached_sessions():
    if time.time() - _SESSIONS_CACHE["at"] > SESSIONS_CACHE_TTL:
        _SESSIONS_CACHE["sessions"] = get_all_sessions()
        _SESSIONS_CACHE["at"] = time.time()
    return _SESSIONS_CACHE["sessions"]


def task_conversations(store, task, sessions_list, state):
    """
    Every conversation on this task: those that wrote to it through mh,
    cards linked to it in sessions.json, and the transcripts the board
    already discovered, joined on the Claude session id. Live first.
    """
    by_sid = {}
    for s in sessions_list:
        sid = s.get("claudeSessionId") or s.get("sessionId")
        if sid:
            by_sid[sid] = s

    seen, rows = set(), []

    def add(sid, iterm_id=None, source=None, info=None, last_event=None):
        if not sid or sid in seen:
            return
        seen.add(sid)
        card = by_sid.get(sid)
        info = info or {}
        live = bool(card and not card.get("isInactive"))
        kind = info.get("kind")
        if card and card.get("isAutomation"):
            kind = "scheduled"
        actor = info.get("actor") or ("sweep" if kind == "scheduled" else None)
        rows.append({
            "sessionId": sid,
            "itermId": (card or {}).get("itermId") or iterm_id or info.get("iterm_id"),
            "live": live,
            "state": (card or {}).get("cardState", "inactive" if card else "unknown"),
            "name": (card or {}).get("name") or info.get("name") or sid[:8],
            "cwd": (card or {}).get("cwd") or info.get("cwd") or "",
            "actor": actor,
            "kind": kind or ("interactive" if card else "unknown"),
            "isAutomation": bool(card and card.get("isAutomation")),
            "age": (card or {}).get("uptime") or "",
            "lastSeen": info.get("last_seen"),
            "lastEvent": _event_item(last_event) if last_event else None,
            "eventCount": info.get("event_count", 0),
            "source": source,
        })

    # Linked only. A conversation is about a task because someone said so,
    # not because it wrote to it; the writes are all in the timeline below.
    for info in store.sessions_for_task(task["id"]):
        if not info.get("linked"):
            continue
        add(info["session_id"], info.get("iterm_id"), "events", info,
            info.get("last_event"))
    for key, value in (state.get("taskAssignments") or {}).items():
        if any(link.get("taskId") == task["id"] for link in assignment_list(value)):
            card = by_sid.get(key)
            if card:
                add(key, card.get("itermId"), "link", None, None)
            else:
                # Keyed by iTerm id for a live card whose Claude id we know.
                for s in sessions_list:
                    if s.get("itermId") == key:
                        add(s.get("claudeSessionId") or key, key, "link")
                        break
                else:
                    add(key, None, "link")
    rows.sort(key=lambda r: (not r["live"], r["lastSeen"] or "", ), reverse=False)
    live = [r for r in rows if r["live"]]
    rest = sorted([r for r in rows if not r["live"]],
                  key=lambda r: r["lastSeen"] or "", reverse=True)
    return live + rest


def task_view_payload(task_id, sessions_list=None, state=None):
    """GET /api/task/<id>: everything the page needs, in one read."""
    store = get_store()
    if store is None:
        return None
    task = store.task(task_id)
    if task is None:
        return None
    project = store.project(task["project_key"]) or {}
    sessions_list = cached_sessions() if sessions_list is None else sessions_list
    state = load_store() if state is None else state

    task_qs = store.questions(task_id=task["id"])
    project_qs = [q for q in store.questions(project_key=task["project_key"])
                  if q["task_id"] != task["id"]]
    dispatches = store.open_dispatches(task["id"])
    events = store.events(task_id=task["id"], limit=50)
    brief = parse_brief(task["brief_path"])

    return {
        "task": {
            "id": task["id"], "tag": _task_tag(task), "title": task["title"],
            "status": task["status"], "statusRaw": task["status_raw"],
            "owner": task["owner"], "load": task["load"], "due": task["due"],
            "plannedDay": task["planned_day"], "waitingOn": task["waiting_on"],
            "notes": task["notes"], "notePath": task["note_path"],
            "noteUrl": obsidian_url(task["note_path"]) if task["note_path"] else None,
            "briefPath": task["brief_path"], "confirmed": bool(task["confirmed"]),
            "doneAt": task["done_at"], "updatedAt": task["updated_at"],
        },
        "project": {"key": project.get("key"), "name": project.get("name"),
                    "status": project.get("status"), "dir": project.get("dir")},
        "brief": brief,
        "dispatchReady": store.dispatch_ready(task["id"]),
        "questions": {"task": [_question_item(q, store) for q in task_qs],
                      "project": [_question_item(q, store) for q in project_qs]},
        "conversations": task_conversations(store, task, sessions_list, state),
        "next": {
            "dispatches": [_event_item(d) for d in dispatches],
            "waitingOn": [q["id"] for q in task_qs if q["blocks"]],
        },
        "events": [_event_item(e) for e in events],
    }


def inflight_tasks(sessions_list=None, state=None):
    """
    The Tasks tab: open rows that have a brief, an open question, an open
    dispatch, or a live conversation, ordered by when they bite.
    """
    store = get_store()
    if store is None:
        return []
    sessions_list = cached_sessions() if sessions_list is None else sessions_list
    state = load_store() if state is None else state

    live_sids = {s.get("claudeSessionId") or s.get("sessionId")
                 for s in sessions_list if not s.get("isInactive")}
    live_sids.discard(None)
    linked_by_task = {}
    for row in store.conn.execute(
            "SELECT DISTINCT task_id FROM events WHERE kind = 'link' AND task_id IS NOT NULL"):
        linked_by_task[row["task_id"]] = set(store.linked_sessions(row["task_id"]))
    for key, value in (state.get("taskAssignments") or {}).items():
        for link in assignment_list(value):
            if link.get("taskId"):
                linked_by_task.setdefault(link["taskId"], set()).add(key)
    live_by_task = {tid: len(sids & live_sids) for tid, sids in linked_by_task.items()}

    open_q = {}
    for q in store.questions():
        if q["task_id"] is not None:
            open_q[q["task_id"]] = open_q.get(q["task_id"], 0) + 1
    open_dispatch = {}
    for row in store.conn.execute(
            "SELECT DISTINCT task_id FROM events WHERE kind = 'dispatch'"):
        if row["task_id"] is not None and store.open_dispatches(row["task_id"]):
            open_dispatch[row["task_id"]] = True
    last_activity = {r["task_id"]: r["ts"] for r in store.conn.execute(
        "SELECT task_id, MAX(ts) AS ts FROM events WHERE task_id IS NOT NULL "
        "GROUP BY task_id")}

    rows = []
    for task in store.tasks(open_only=True):
        tid = task["id"]
        if not (task["brief_path"] or open_q.get(tid) or open_dispatch.get(tid)
                or live_by_task.get(tid)):
            continue
        rows.append({
            "id": tid, "tag": _task_tag(task), "title": task["title"],
            "project": task["project_key"], "status": task["status"],
            "plannedDay": task["planned_day"], "due": task["due"],
            "load": task["load"], "hasBrief": bool(task["brief_path"]),
            "openQuestions": open_q.get(tid, 0),
            "openDispatches": bool(open_dispatch.get(tid)),
            "liveConversations": live_by_task.get(tid, 0),
            "lastActivity": last_activity.get(tid),
        })
    rows.sort(key=lambda r: (r["plannedDay"] is None, r["plannedDay"] or "",
                             r["due"] is None, r["due"] or "",
                             r["lastActivity"] or ""))
    return rows


def find_task_session(store, task_id, max_age_days=14):
    """
    The most recently linked conversation whose transcript is still on
    disk, or None. Linked, not "wrote to": a capture sweep writes to many
    tasks and is about none of them, and resuming it would put MQ in a
    conversation about nothing in particular.
    """
    cutoff = time.time() - max_age_days * 86400
    linked = set(store.linked_sessions(task_id))
    if not linked:
        return None
    for row in store.conn.execute(
            """SELECT session_id, MAX(ts) AS last FROM events
               WHERE task_id = ? AND kind = 'link' AND session_id IS NOT NULL
               GROUP BY session_id ORDER BY last DESC""", (task_id,)):
        sid = row["session_id"]
        if sid not in linked:
            continue
        for jsonl in CLAUDE_PROJECTS_DIR.glob(f"*/{sid}.jsonl"):
            try:
                if jsonl.stat().st_mtime >= cutoff:
                    return sid, extract_cwd_fast(str(jsonl)) or str(MELLONHEAD_ROOT)
            except OSError:
                continue
    return None


def task_orca_prompt(store, task, resuming=False):
    """The opening line for `claude --agent orca` on one task."""
    tag = _task_tag(task)
    lines = [f"Work on {tag}: {task['title']}", ""]
    if resuming:
        lines += ["You have worked on this task in this conversation before, so "
                  "you still hold your own reasoning. Re-read the current state "
                  "before saying anything:", ""]
    else:
        lines += ["Read these first, then talk to me:", ""]
    if task["brief_path"]:
        lines.append(f"- the brief at `{task['brief_path']}`")
    else:
        lines.append("- there is no brief yet: run `/scope-and-start` on this "
                     "task and record the brief with `./operations/mh task "
                     f"brief {tag} <path>` before anything else")
    lines += [
        f"- `./operations/mh question list {tag}` for what is still waiting on me",
        f"- `./operations/mh task list {task['project_key']} --json` for the "
        "open dispatches and the last review",
    ]
    if task["note_path"]:
        lines.append(f"- the note file at `{task['note_path']}`")
    lines += [
        "",
        "Then continue the work on this task. Record what you do through "
        "`mh` (`task dispatch`, `task deliver`, `task review`, `question add`), "
        "always with `--actor orca`, and ask me nothing that you can propose "
        "an answer to instead.",
    ]
    if not resuming:
        lines[1:1] = [
            "Before anything else, so the session manager shows this "
            f"conversation on the task: `./operations/mh task link {tag} --actor orca`",
            "",
        ]
    return "\n".join(lines)


def parse_priorities():
    """Entry point used by the handlers. Store first, markdown as a fallback."""
    return load_priorities()


def parse_priorities_markdown():
    """Parse priorities.md to extract daily goals for current + next week."""
    if not PRIORITIES_FILE.exists():
        return {"days": [], "blocked": [], "weeks": [], "source": "markdown"}

    with open(PRIORITIES_FILE) as f:
        content = f.read()

    week_matches = list(re.finditer(r"\*\*Week of (.+?)\*\*", content))
    # Reject passing-reference matches whose label ends in ':' (e.g. inside
    # the Cognitive Budget block: `**Week of Aug 3, 2026:**`). Real section
    # headers look like `**Week of Aug 3, 2026**` or `... 2026** -- suffix`.
    week_matches = [m for m in week_matches if not m.group(1).rstrip().endswith(":")]
    if not week_matches:
        return {"days": [], "blocked": [], "weeks": [], "source": "markdown"}

    import datetime
    today = datetime.date.today()

    # Pair each match with its parsed date (drop ones we can't parse)
    parsed = []
    for m in week_matches:
        d = _parse_week_date(m.group(1))
        if d:
            parsed.append((d, m))
    if not parsed:
        parsed = [(today, week_matches[-1])]

    # Find current week: latest start date <= today
    current_idx = None
    for i, (d, _) in enumerate(parsed):
        if d <= today:
            current_idx = i

    if current_idx is None:
        # All planned weeks are in the future — surface the nearest one as
        # "upcoming", not "current"
        selected_indices = [0]
        has_current = False
    else:
        selected_indices = [current_idx]
        if current_idx + 1 < len(parsed):
            selected_indices.append(current_idx + 1)
        has_current = True

    weeks = []
    all_blocked = []
    for pos, i in enumerate(selected_indices):
        _, match = parsed[i]
        title = match.group(1).strip()
        days, blocked = _parse_week_section(content, match)
        is_current = has_current and (pos == 0)
        weeks.append({"title": title, "days": days, "isCurrent": is_current})
        all_blocked.extend(blocked)

    current_week = weeks[0]
    return {
        "weekTitle": current_week["title"],
        "days": current_week["days"],
        "blocked": all_blocked,
        "weeks": weeks,
        "noCurrentWeek": not has_current,
        "source": "markdown",
    }


def get_today_day_name():
    """Return today's day name (e.g., 'Monday')."""
    import datetime
    return datetime.datetime.now().strftime("%A")


def match_priority_to_session(item, session, task_assignments):
    """
    Match a priority item to a session.

    The store gives every row a project, so a day-card line and a session
    working in that project's directory match exactly. That runs before the
    Notion id and well before the keyword guess below it.
    """
    project_key = item.get("project")
    if project_key and project_key != "one-off":
        session_project = (session.get("taskList") or {}).get("projectKey")
        if session_project:
            return session_project == project_key

    notion_id = item.get("notionTaskId", "")

    if notion_id:
        # Exact ID match against this session's task assignment
        iterm_id = session.get("itermId", "")
        wanted = notion_id.replace("-", "")
        return any((a.get("notionTaskId") or "").replace("-", "") == wanted
                   for a in assignment_list(task_assignments.get(iterm_id)))

    # Fallback: fuzzy keyword matching
    item_lower = item.get("text", "").lower()
    stop = {"the", "and", "for", "from", "with", "that", "this", "into",
            "about", "been", "have", "will", "need", "done", "next", "start",
            "new", "get", "out", "she", "was", "her", "his", "day", "week"}
    words = [w for w in re.findall(r"[a-z]+", item_lower) if len(w) > 2 and w not in stop]

    corpus = " ".join([
        session.get("name", ""),
        session.get("cwd", ""),
        " ".join(session.get("allTags", [])),
        (session.get("taskList") or {}).get("projectName", ""),
        (session.get("taskList") or {}).get("file", ""),
        session.get("nextStepOverride", ""),
    ]).lower()

    if not corpus.strip() or not words:
        return False

    matches = [w for w in words if w in corpus]
    long_matches = [w for w in matches if len(w) >= 6]
    return len(matches) >= 2 or len(long_matches) >= 1


def auto_assign_priorities(sessions_list, priorities_data, store):
    """Auto-assign Today/This Week/Next Week priorities based on weekly priorities file."""
    weeks = priorities_data.get("weeks") or []
    if not weeks and priorities_data.get("days"):
        weeks = [{"days": priorities_data["days"], "isCurrent": True}]
    if not weeks:
        return

    today_name = get_today_day_name()
    manually_set = store.get("priorities", {})
    task_assignments = store.get("taskAssignments", {})

    today_items = []
    week_items = []
    next_week_items = []
    for week in weeks:
        is_current = week.get("isCurrent", False)
        for day in week.get("days", []):
            day_lower = day["day"].lower()
            undone = [i for i in day["items"] if not i["done"]]
            if is_current and today_name.lower() in day_lower:
                today_items.extend(undone)
            elif is_current:
                week_items.extend(undone)
            else:
                next_week_items.extend(undone)

    for session in sessions_list:
        iterm_id = session.get("itermId", "")
        existing = manually_set.get(iterm_id, "")

        if existing in ("Later", "__cleared__"):
            continue

        matched_today = any(match_priority_to_session(i, session, task_assignments) for i in today_items)
        if matched_today:
            session["priorityLabel"] = "Today"
            continue

        matched_week = any(match_priority_to_session(i, session, task_assignments) for i in week_items)
        if matched_week:
            session["priorityLabel"] = "This Week"
            continue

        matched_next = any(match_priority_to_session(i, session, task_assignments) for i in next_week_items)
        if matched_next:
            session["priorityLabel"] = "Next Week"
            continue

        if existing in ("Today", "This Week", "Next Week"):
            session["priorityLabel"] = "Later"


# ---------------------------------------------------------------------------
# Session todos
# ---------------------------------------------------------------------------

def todo_path(iterm_id):
    return TODOS_DIR / f"{iterm_id}.md"


def read_todo(iterm_id):
    """Read a session's todo file. Returns metadata dict + items list."""
    path = todo_path(iterm_id)
    if not path.exists():
        return None

    with open(path) as f:
        content = f.read()

    metadata = {}
    items = []
    in_frontmatter = False
    past_frontmatter = False

    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "---" and not past_frontmatter:
            if in_frontmatter:
                in_frontmatter = False
                past_frontmatter = True
            else:
                in_frontmatter = True
            continue
        if in_frontmatter:
            m = re.match(r"^(\w[\w_]*)\s*:\s*(.+)$", stripped)
            if m:
                metadata[m.group(1)] = m.group(2).strip()
            continue
        # Parse todo items
        m = re.match(r"^-\s*\[([ xX])\]\s*(.+)$", stripped)
        if m:
            items.append({
                "done": m.group(1).lower() == "x",
                "text": m.group(2).strip(),
            })

    # Find next step (first unchecked item)
    next_step = None
    for item in items:
        if not item["done"]:
            next_step = item["text"]
            break

    return {
        "metadata": metadata,
        "items": items,
        "nextStep": next_step,
    }


def write_todo(iterm_id, metadata, items):
    """Write a session's todo file."""
    TODOS_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in metadata.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    for item in items:
        check = "x" if item.get("done") else " "
        lines.append(f"- [{check}] {item['text']}")
    lines.append("")

    with open(todo_path(iterm_id), "w") as f:
        f.write("\n".join(lines))


def update_todo_index(sessions_list):
    """Write index.json mapping Claude session IDs and PIDs to iTerm IDs."""
    index = {}
    for s in sessions_list:
        iterm_id = s.get("itermId", "")
        pid = s.get("pid")
        session_id = s.get("sessionId", "")
        if iterm_id:
            if pid:
                index[str(pid)] = iterm_id
            if session_id:
                index[session_id] = iterm_id
    TODOS_DIR.mkdir(parents=True, exist_ok=True)
    with open(TODOS_INDEX, "w") as f:
        json.dump(index, f, indent=2)


# ---------------------------------------------------------------------------
# Inactive session discovery (recover past Claude conversations after reboot)
# ---------------------------------------------------------------------------

_CWD_RE = re.compile(rb'"cwd"\s*:\s*"([^"]+)"')
_AI_TITLE_RE = re.compile(rb'"aiTitle"\s*:\s*"((?:[^"\\]|\\.)*)"')
# Match only first-turn user messages with simple string content (not tool_result / command wrappers).
# Requires "userType":"external" to filter out tool result entries that also have type=user.
_USER_MSG_RE = re.compile(
    rb'"type"\s*:\s*"user"[^\n]{0,2000}?"content"\s*:\s*"((?!<)[^"\\<{][^"\\<]{3,150})"[^\n]*?"userType"\s*:\s*"external"'
)
# The unattended jobs all open with a prompt whose first line names them:
# "# Scheduled capture sweep", "# Weekly proposal (Friday 1pm, unattended)",
# and so on, plus the one-line Slack posts. Matching the convention rather
# than a list means a new job added later is recognised without a code change.
# Matched against the raw opening of the transcript rather than the parsed
# first user message: these prompts run to thousands of characters and the
# first-message regex only captures short ones, so it picks up a later line
# instead. That is also why these cards are titled with a bare timestamp.
AUTOMATION_PROMPT = re.compile(
    rb"# (?:Scheduled [\w/ -]+ sweep|Nightly [\w ]+|Weekly proposal"
    rb"|Unattended [\w ]+)|Post exactly this", re.I)


# Cache of (mtime -> {cwd, title, firstMessage}) keyed by jsonl path
_JSONL_META_CACHE = {}


def _decode_json_str(raw_bytes):
    """Decode a regex-captured JSON string body (handles \\n, \\", \\\\, \\u escapes)."""
    try:
        return json.loads(b'"' + raw_bytes + b'"')
    except Exception:
        return raw_bytes.decode("utf-8", errors="ignore")


def extract_jsonl_metadata(jsonl_path, mtime):
    """Read the first ~128KB of a jsonl and pull out cwd, ai-title, and first user msg.
    Caches by (path, mtime) so we don't re-scan unchanged files on every poll."""
    cached = _JSONL_META_CACHE.get(jsonl_path)
    if cached and cached["mtime"] == mtime:
        return cached
    cwd = ""
    title = ""
    first_msg = ""
    is_automation = False
    try:
        with open(jsonl_path, "rb") as f:
            chunk = f.read(131072)  # 128KB — cwd is in line 1-3, ai-title within first few turns
        m = _CWD_RE.search(chunk)
        if m:
            cwd = m.group(1).decode("utf-8", errors="ignore")
        # ai-title may appear multiple times; take the last one in our window (most recent)
        for m in _AI_TITLE_RE.finditer(chunk):
            title = _decode_json_str(m.group(1))
        # Look for the job's own prompt in the opening of the transcript.
        # Bounded to the first 32KB so a later mention in a human
        # conversation about the automation is not mistaken for a run of it.
        is_automation = bool(AUTOMATION_PROMPT.search(chunk[:32768]))
        # First user message (fallback when ai-title hasn't been generated yet)
        if not title:
            m = _USER_MSG_RE.search(chunk)
            if m:
                first_msg = _decode_json_str(m.group(1))[:80]
    except OSError:
        pass
    meta = {"mtime": mtime, "cwd": cwd, "title": title,
            "firstMessage": first_msg, "isAutomation": is_automation}
    _JSONL_META_CACHE[jsonl_path] = meta
    return meta


def extract_cwd_fast(jsonl_path):
    """Backward-compat wrapper. Prefer extract_jsonl_metadata."""
    try:
        return extract_jsonl_metadata(jsonl_path, Path(jsonl_path).stat().st_mtime)["cwd"]
    except OSError:
        return ""


def decode_flattened_path(name):
    """Best-effort decode of `.claude/projects/-Users-mariena-Projects-foo` → `/Users/mariena/Projects/foo`.
    Returns the decoded path if it exists on disk, else empty string."""
    if not name.startswith("-"):
        return ""
    candidate = "/" + name[1:].replace("-", "/")
    if Path(candidate).is_dir():
        return candidate
    return ""


def humanize_age(seconds):
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def discover_inactive_sessions(active_claude_ids):
    """Walk ~/.claude/projects/*/*.jsonl for files modified in INACTIVE_WINDOW_DAYS.
    Returns list of dicts: claudeSessionId, cwd, lastActivity (epoch), jsonlPath."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return []
    cutoff = time.time() - INACTIVE_WINDOW_DAYS * 86400
    results = []
    seen = set()
    for proj_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                st = jsonl.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            sid = jsonl.stem
            if sid in active_claude_ids or sid in seen:
                continue
            seen.add(sid)
            meta = extract_jsonl_metadata(str(jsonl), st.st_mtime)
            cwd = meta["cwd"] or decode_flattened_path(proj_dir.name)
            results.append({
                "claudeSessionId": sid,
                "cwd": cwd,
                "title": meta["title"],
                "firstMessage": meta["firstMessage"],
                "isAutomation": meta.get("isAutomation", False),
                "lastActivity": st.st_mtime,
                "jsonlPath": str(jsonl),
            })
    results.sort(key=lambda r: -r["lastActivity"])
    return results[:INACTIVE_MAX]


def assignment_list(value):
    """
    A card's task links as a list. The slot held one dict per card until
    the task view; a conversation is about several tasks often enough that
    it is a list now, and old single entries read as a list of one.
    """
    if not value:
        return []
    if isinstance(value, dict):
        return [value]
    return [v for v in value if isinstance(v, dict)]


def lookup_state(category_map, claude_session_id, iterm_id, default=None):
    """Prefer claudeSessionId, fall back to itermId."""
    if claude_session_id and claude_session_id in category_map:
        return category_map[claude_session_id]
    if iterm_id and iterm_id in category_map:
        return category_map[iterm_id]
    return default


def migrate_iterm_to_claude(store, iterm_id, claude_session_id):
    """Move any state keyed under iterm_id to claude_session_id. Returns True if anything changed."""
    if not iterm_id or not claude_session_id or iterm_id == claude_session_id:
        return False
    changed = False
    for cat in STATE_CATEGORIES:
        m = store.get(cat, {})
        if iterm_id in m:
            if claude_session_id not in m:
                m[claude_session_id] = m[iterm_id]
            del m[iterm_id]
            changed = True
    # Migrate per-iterm tags structure (which has a "manual" subkey)
    # Migrate todo file
    old_todo = todo_path(iterm_id)
    new_todo = todo_path(claude_session_id)
    if old_todo.exists() and not new_todo.exists():
        try:
            old_todo.rename(new_todo)
            changed = True
        except OSError:
            pass
    return changed


def build_inactive_card(inactive, store):
    """Build a session card dict for an inactive (on-disk only) Claude conversation."""
    sid = inactive["claudeSessionId"]
    cwd = inactive["cwd"]
    short_cwd = cwd.replace(str(Path.home()), "~") if cwd else ""
    auto = auto_tags_from_path(cwd) if cwd else []
    manual_tags = store.get("tags", {}).get(sid, {}).get("manual", [])
    # Name resolution: manual rename → Claude's ai-title → first user message → dir name → uuid
    rename = store.get("renames", {}).get(sid, "")
    title = (inactive.get("title") or "").strip()
    first_msg = (inactive.get("firstMessage") or "").strip()
    display_name = rename or title or first_msg or (Path(cwd).name if cwd else sid[:8])
    priority_raw = store.get("priorities", {}).get(sid, "")
    priority_label = "" if priority_raw == "__cleared__" else priority_raw

    age_str = humanize_age(time.time() - inactive["lastActivity"])

    # Read todo (file named by Claude session ID for inactive sessions)
    todo = read_todo(sid)

    return {
        "id": f"inactive-{sid[:8]}",
        "itermId": sid,  # Use Claude session ID as the action key
        "claudeSessionId": sid,
        "name": display_name,
        "originalName": Path(cwd).name if cwd else sid[:8],
        "tty": "",
        "pid": None,
        "cwd": cwd,
        "shortCwd": short_cwd,
        "sessionId": sid,
        "startedAt": int(inactive["lastActivity"] * 1000),
        "uptime": age_str,
        "isActive": False,
        "isAutomation": inactive.get("isAutomation", False),
        "isInactive": True,
        "cardState": "inactive",
        "colorGroup": "default",
        "colorRaw": "",
        "autoTags": auto,
        "manualTags": manual_tags,
        "allTags": list(set(auto + manual_tags)),
        "taskList": None,
        "isAutoLinked": False,
        "priorityLabel": priority_label,
        "nextStepOverride": store.get("nextSteps", {}).get(sid, ""),
        "needsReview": False,
        "taskAssignments": assignment_list(store.get("taskAssignments", {}).get(sid)),
        "taskAssignment": (assignment_list(store.get("taskAssignments", {}).get(sid)) or [None])[0],
        "todo": todo,
    }


def resolve_task_assignments(state):
    """
    One-time pass: a task assignment made before the store carried a Notion
    task id and a path, and nothing joined it to a store row. Resolve each
    to `taskId` and `projectKey` where the Notion id matches a row, and
    mark the rest so they are not retried every poll.
    """
    store = get_store()
    if store is None:
        return False
    changed = False
    assignments = state.get("taskAssignments") or {}
    for key, value in list(assignments.items()):
        if isinstance(value, dict):
            assignments[key] = [value]       # the slot is a list now
            changed = True
    links = [link for value in assignments.values()
             for link in assignment_list(value)]
    for link in links:
        if "taskId" in link:
            continue
        notion = (link.get("notionTaskId") or "").replace("-", "").lower()
        task = None
        if notion:
            for row in store.conn.execute(
                    "SELECT id, project_key, notion_task_id FROM tasks "
                    "WHERE notion_task_id IS NOT NULL"):
                if (row["notion_task_id"] or "").replace("-", "").lower() == notion:
                    task = row
                    break
        if task is None and link.get("taskFile") and link.get("taskTitle"):
            project = project_for_cwd(str(Path(link["taskFile"]).parent))
            if project:
                for row in store.tasks(project_key=project["key"]):
                    if row["title"].strip().lower() == link["taskTitle"].strip().lower():
                        task = row
                        break
        link["taskId"] = task["id"] if task else None
        link["projectKey"] = task["project_key"] if task else None
        changed = True
    return changed


# Cache mapping iTerm session ID -> Claude session ID for the current live sessions.
# Refreshed at the end of get_all_sessions. Allows mutation endpoints to resolve the
# canonical (Claude-session-id) key from whatever key the frontend passes.
_LIVE_KEY_MAP = {}


def canonical_key(key):
    """Resolve an incoming session key (iTermID for live cards, claudeSessionID for inactive)
    to its canonical Claude session ID when known. Falls through otherwise."""
    if not key:
        return key
    return _LIVE_KEY_MAP.get(key, key)


# Pending resumes: claude --resume creates a new session ID, so we need to track
# (old_sid -> cwd, timestamp) and migrate state when the new live session appears.
_PENDING_RESUMES = {}
PENDING_RESUME_TTL = 300  # 5 minutes


def process_pending_resumes(sessions_list, store):
    """Match new live sessions to their resume source. Migrate state. Returns True if store changed."""
    if not _PENDING_RESUMES:
        return False
    now = time.time()
    changed = False
    expired = []
    for old_sid, pending in list(_PENDING_RESUMES.items()):
        # Pending entries can be (cwd, ts) or (cwd, ts, display_name) — support both
        cwd = pending[0]
        ts = pending[1]
        display_name = pending[2] if len(pending) > 2 else ""
        if now - ts > PENDING_RESUME_TTL:
            expired.append(old_sid)
            # Un-archive in case claude never started — user can try again
            arch = store.get("archivedSessions", {})
            if arch.pop(old_sid, None) is not None:
                changed = True
            continue
        # Find a live session matching the resume — claude --resume usually preserves
        # the session ID, but we also accept a new sid in the same cwd as a fallback.
        for s in sessions_list:
            if s.get("isInactive"):
                continue
            new_sid = s.get("sessionId", "")
            if not new_sid:
                continue
            same_sid = (new_sid == old_sid)
            if not same_sid:
                # Different sid — only match if it's in the same cwd and started recently
                if s.get("cwd") != cwd:
                    continue
                started_sec = s.get("startedAt", 0) / 1000
                if started_sec and started_sec < ts - 5:
                    continue
            # Match. Migrate state if the sid changed.
            if not same_sid:
                for cat in STATE_CATEGORIES:
                    m = store.get(cat, {})
                    if old_sid in m and new_sid not in m:
                        m[new_sid] = m[old_sid]
                        changed = True
                old_todo = todo_path(old_sid)
                new_todo = todo_path(new_sid)
                if old_todo.exists() and not new_todo.exists():
                    try:
                        old_todo.rename(new_todo)
                        changed = True
                    except OSError:
                        pass
            # Carry over the inactive's display name (ai-title) as a rename so the
            # new live card keeps the same friendly title
            renames = store.setdefault("renames", {})
            if display_name and new_sid not in renames:
                renames[new_sid] = display_name
                changed = True
            # Clear the archive flag — the session is live again, and when it goes
            # idle later we want it to reappear as an inactive card
            arch = store.get("archivedSessions", {})
            if arch.pop(old_sid, None) is not None:
                changed = True
            if not same_sid and arch.pop(new_sid, None) is not None:
                changed = True
            expired.append(old_sid)
            break
    for sid in expired:
        _PENDING_RESUMES.pop(sid, None)
    return changed


PROPOSAL_MARKER = "Weekly proposal (Friday 1pm, unattended)"


def find_proposal_session(week_start, max_age_days=14):
    """
    The transcript of the run that built this week, if it is still on disk.

    Resuming it beats starting fresh: the agent still holds why it placed
    each row, what it could not check, and what it left out.

    Identified by evidence that it actually proposed this week, not by the
    prompt text alone — a conversation that merely discussed the job matches
    that. File mtimes are unreliable here (they get bulk-touched, and every
    transcript in this directory currently shares one), so ordering comes
    from the last timestamp inside each transcript.
    """
    import time

    project_dir = CLAUDE_PROJECTS_DIR / decode_project_dir_name(MELLONHEAD_ROOT)
    if not project_dir.exists():
        return None
    cutoff = time.time() - max_age_days * 86400
    hits = []
    for path in project_dir.glob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        # It proposed this week if it ran the command that does so.
        if f"plan propose {week_start}" not in text:
            continue
        last = ""
        for line in text.splitlines():
            marker = '"timestamp":"'
            at = line.find(marker)
            if at != -1:
                last = max(last, line[at + len(marker):at + len(marker) + 24])
        hits.append((last, path.stem))
    if not hits:
        return None
    return sorted(hits, reverse=True)[0][1]


def decode_project_dir_name(root):
    """~/Projects/mellonhead -> -Users-mariena-Projects-mellonhead"""
    return str(root).replace("/", "-")


def week_review_prompt(store, week_start, resuming=False):
    """
    The opening prompt for a conversation about a proposed week.

    The proposal itself is not the point: MQ can already read the rows. What
    she cannot see is what the job assumed, what it could not check, and
    which rules it applied. The prompt asks for that first, and only then for
    a decision.
    """
    import datetime

    row = store.week(week_start) or {}
    notes = (row.get("notes") or "").strip()
    end = (datetime.date.fromisoformat(week_start)
           + datetime.timedelta(days=6)).isoformat()
    rows = store.conn.execute(
        """SELECT * FROM tasks WHERE planned_day >= ? AND planned_day <= ?
           ORDER BY planned_day, seq IS NULL, seq, id""",
        (week_start, end)).fetchall()

    lines = [
        f"I want to review the proposed week of {week_start} before I lock it.",
        "",
    ]
    if resuming:
        lines += [
            "You built this proposal earlier in this session, so you still "
            "have your own reasoning. Re-read the current rows with "
            f"`./operations/mh plan show {week_start}` in case anything moved, "
            "then talk to me.",
            "",
        ]
    else:
        lines += [
            "Read these first, then talk to me:",
            "",
            f"- `./operations/mh plan show {week_start}` for the rows as they stand",
            "- the `### Commitment Rules` section at the top of `priorities.md`",
            "- `operations/projects-dashboard.md` for what each project has waiting",
            "",
        ]
    lines += [
        "Open by telling me, in this order and without me having to ask:",
        "",
        "1. **The shape of the week and why.** What each day is for, which "
        "rules drove it, and where a rule was bent or missed.",
        "2. **What you assumed that you could not verify.** The job has no "
        "calendar, no Gamma, and no email or Slack beyond what is already "
        "swept into files. Name every item whose placement depends on "
        "something you could not check, so I can confirm or correct it.",
        "3. **What you deliberately left out**, and why it did not make the "
        "week.",
        "",
        "Then ask me for:",
        "",
        "- corrections to any of those assumptions",
        "- changes in my capacity this week that you could not know about",
        "- anything new that has to be in the week, or any project whose "
        "priority has moved",
        "",
        "Work through it with me one topic at a time rather than in one long "
        "block. As I answer, make the changes:",
        "",
        "```",
        f"./operations/mh task plan <key#id> <YYYY-MM-DD> --actor orca",
        f"./operations/mh task plan <key#id> --actor orca      # pull it out",
        f"./operations/mh task load <key#id> deep|medium|shallow --actor orca",
        f"./operations/mh task add <project> \"<title>\" --day <date> --actor orca",
        "```",
        "",
        f"**Do not run `mh plan lock {week_start}` until I say yes in so many "
        "words.** Proposing is yours; locking is mine. When I do say yes, "
        "lock it and tell me what changed between the proposal and the "
        "locked week.",
        "",
        f"There are {len(rows)} rows on day cards for this week.",
    ]
    if notes and not resuming:
        lines += ["", "The job's own reasoning, verbatim:", "", "---",
                  notes, "---"]
    return "\n".join(lines)


def launch_claude_session(cwd, prompt, resume_id=None, agent=None,
                          prompt_name="week-review-prompt.md"):
    """
    Open an iTerm tab running `claude` with a prepared opening prompt.

    The prompt goes through a file rather than the command line: it runs to
    several thousand characters and carries quotes, backticks and newlines,
    all of which have to survive Python, AppleScript and the shell intact.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    prompt_file = STATE_DIR / prompt_name
    prompt_file.write_text(prompt)

    safe_cwd = shlex.quote(cwd)
    safe_prompt = shlex.quote(str(prompt_file))
    flags = f" --agent {shlex.quote(agent)}" if agent else ""
    if resume_id:
        command = (f"cd {safe_cwd} && claude{flags} --resume {shlex.quote(resume_id)} "
                   f"\"$(cat {safe_prompt})\"")
    else:
        command = f"cd {safe_cwd} && claude{flags} \"$(cat {safe_prompt})\""
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "iTerm2"
        if (count of windows) = 0 then
            create window with default profile
            tell current session of current window
                write text "{escaped}"
            end tell
        else
            tell current window
                create tab with default profile
                tell current session
                    write text "{escaped}"
                end tell
            end tell
        end if
        activate
    end tell
    '''
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            print(f"launch failed: {result.stderr}")
            return False
        return True
    except Exception as exc:
        print(f"launch error: {exc}")
        return False


def resume_claude_session(cwd, claude_session_id):
    """Open a new iTerm tab, cd to the given directory, and run `claude --resume <id>`."""
    safe_cwd = shlex.quote(cwd) if cwd else "~"
    safe_id = shlex.quote(claude_session_id)
    command = f"cd {safe_cwd} && claude --resume {safe_id}"
    # AppleScript needs double-quote escaping for the command text
    escaped_command = command.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
    tell application "iTerm2"
        if (count of windows) = 0 then
            create window with default profile
            tell current session of current window
                write text "{escaped_command}"
            end tell
        else
            tell current window
                create tab with default profile
                tell current session
                    write text "{escaped_command}"
                end tell
            end tell
        end if
        activate
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            print(f"Resume failed: {result.stderr}")
            return False
        return True
    except Exception as e:
        print(f"Resume error: {e}")
        return False


# ---------------------------------------------------------------------------
# Color mapping — iTerm2 RGB to named color groups
# ---------------------------------------------------------------------------

COLOR_MAP = {
    "purple": (200, 150, 255),
    "green": (150, 255, 150),
    "blue": (150, 200, 255),
    "red": (255, 150, 150),
    "orange": (255, 200, 130),
    "yellow": (255, 255, 150),
    "pink": (255, 180, 220),
    "teal": (150, 230, 220),
}


def classify_color(rgb_string):
    if not rgb_string:
        return "default"
    try:
        # iTerm2 reports in 16-bit color (0-65535)
        parts = [int(x.strip()) for x in rgb_string.split(",")]
        if len(parts) != 3:
            return "default"
        r, g, b = [x / 256 for x in parts]  # scale to 8-bit
        best = "default"
        best_dist = float("inf")
        for name, (cr, cg, cb) in COLOR_MAP.items():
            dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
            if dist < best_dist:
                best_dist = dist
                best = name
        return best if best_dist < 20000 else "default"
    except Exception:
        return "default"


# ---------------------------------------------------------------------------
# Main session assembly
# ---------------------------------------------------------------------------

def get_all_sessions():
    iterm_sessions = get_iterm_sessions()
    claude_procs = get_claude_processes()
    store = load_store()

    # Index claude procs by tty
    proc_by_tty = {}
    for proc in claude_procs:
        proc_by_tty[proc["tty"]] = proc

    sessions = []
    store_dirty = False
    for iterm in iterm_sessions:
        proc = proc_by_tty.get(iterm["tty"])
        if not proc:
            continue  # Not a Claude session

        session_data = get_session_file(proc["pid"])
        original_cwd = session_data.get("cwd", "") if session_data else ""
        session_id = session_data.get("sessionId", "") if session_data else ""
        started_at = session_data.get("startedAt", 0) if session_data else 0

        # Once we know both keys, lazily migrate any iterm-keyed state to claude-keyed
        if session_id and migrate_iterm_to_claude(store, iterm["itermId"], session_id):
            store_dirty = True

        # Allow overriding the working directory (prefer claudeSessionId key)
        cwd_override = lookup_state(store.get("cwdOverrides", {}), session_id, iterm["itermId"], "")
        cwd = cwd_override or original_cwd

        # Parse task list — explicit link takes priority, "none" suppresses auto
        task_list = None
        is_auto_linked = False
        linked_task_file = lookup_state(store.get("taskLinks", {}), session_id, iterm["itermId"], "")
        if linked_task_file == "__none__":
            pass  # Explicitly unlinked, skip auto-discovery
        elif linked_task_file and Path(linked_task_file).exists():
            try:
                # An explicit link points at a markdown file; resolve it to
                # the owning project so the panel still reads from the store.
                linked = project_for_cwd(str(Path(linked_task_file).parent))
                task_list = (task_list_from_store(linked["key"]) if linked
                             else None) or parse_task_list(Path(linked_task_file))
            except Exception as e:
                print(f"Task parse error for {linked_task_file}: {e}")
        else:
            project = project_for_cwd(cwd) if cwd else None
            if project:
                task_list = task_list_from_store(project["key"])
                is_auto_linked = task_list is not None
            if task_list is None:
                task_file = find_task_list(cwd) if cwd else None
                if task_file:
                    try:
                        task_list = parse_task_list(task_file)
                        is_auto_linked = True
                    except Exception as e:
                        print(f"Task parse error for {task_file}: {e}")

        # Auto tags
        auto = auto_tags_from_path(cwd) if cwd else []

        # Load persisted manual tags — prefer claudeSessionId, fall back to itermId
        tags_entry = lookup_state(store.get("tags", {}), session_id, iterm["itermId"], {})
        manual_tags = tags_entry.get("manual", []) if isinstance(tags_entry, dict) else []

        # Activity detection
        is_active = proc["cpu"] > 1.0 or proc["state"].startswith("R")

        # Determine card state: working, ready, blocked
        # (needs_review is a separate manual flag, not a state)
        card_state = "ready"  # default: blue, ready for me
        if is_active:
            card_state = "working"
        elif task_list and task_list.get("tasks"):
            next_task = task_list.get("nextStep")
            is_blocked = False
            if next_task:
                s = next_task["status"].lower()
                is_blocked = s in ("on hold", "blocked")
            if not next_task:
                is_blocked = any(
                    t["status"].lower() in ("on hold", "blocked")
                    for t in task_list["tasks"]
                    if t["status"].lower() != "done"
                )
            if is_blocked:
                card_state = "blocked"

        # Manual "needs review" flag overrides visual state
        needs_review = bool(lookup_state(store.get("needsReview", {}), session_id, iterm["itermId"], False))
        if needs_review and not is_active:
            card_state = "needs_review"

        # Uptime
        uptime_str = ""
        if started_at:
            elapsed = time.time() - (started_at / 1000)
            if elapsed < 3600:
                uptime_str = f"{int(elapsed / 60)}m"
            elif elapsed < 86400:
                uptime_str = f"{elapsed / 3600:.1f}h"
            else:
                uptime_str = f"{elapsed / 86400:.1f}d"

        # Shorten CWD for display
        short_cwd = cwd.replace(str(Path.home()), "~") if cwd else ""

        # Check for renamed title — prefer claudeSessionId
        display_name = lookup_state(store.get("renames", {}), session_id, iterm["itermId"], iterm["name"])

        # Load priority label and next step override
        priority_label_raw = lookup_state(store.get("priorities", {}), session_id, iterm["itermId"], "")
        priority_label = "" if priority_label_raw == "__cleared__" else priority_label_raw
        next_step_override = lookup_state(store.get("nextSteps", {}), session_id, iterm["itermId"], "")

        task_assignments = assignment_list(
            lookup_state(store.get("taskAssignments", {}), session_id, iterm["itermId"]))
        # Todo file: prefer one keyed by claudeSessionId
        todo = read_todo(session_id) if session_id else None
        if not todo:
            todo = read_todo(iterm["itermId"])

        sessions.append({
            "id": f"{proc['pid']}",
            "itermId": iterm["itermId"],
            "claudeSessionId": session_id,
            "name": display_name,
            "originalName": iterm["name"],
            "tty": iterm["tty"],
            "pid": proc["pid"],
            "cwd": cwd,
            "shortCwd": short_cwd,
            "sessionId": session_id,
            "startedAt": started_at,
            "uptime": uptime_str,
            "isActive": is_active,
            "isInactive": False,
            "cardState": card_state,
            "colorGroup": "default",
            "colorRaw": iterm.get("color", ""),
            "autoTags": auto,
            "manualTags": manual_tags,
            "allTags": list(set(auto + manual_tags)),
            "taskList": task_list,
            "isAutoLinked": is_auto_linked,
            "priorityLabel": priority_label,
            "nextStepOverride": next_step_override,
            "needsReview": needs_review,
            "taskAssignments": task_assignments,
            "taskAssignment": task_assignments[0] if task_assignments else None,
            "todo": todo,
        })

    # Resolve any pending resumes — migrate state old_sid -> new_sid for live sessions
    # that appeared after a resume request in the same cwd
    if process_pending_resumes(sessions, store):
        store_dirty = True

    # Merge inactive (on-disk only) Claude conversations
    active_claude_ids = {s["sessionId"] for s in sessions if s.get("sessionId")}
    archived = store.get("archivedSessions", {}) or {}
    for inactive in discover_inactive_sessions(active_claude_ids):
        if archived.get(inactive["claudeSessionId"]):
            continue
        sessions.append(build_inactive_card(inactive, store))

    if resolve_task_assignments(store):
        store_dirty = True

    if store_dirty:
        save_store(store)

    # Update the index so Claude sessions can find their todo files
    update_todo_index(sessions)

    _SESSIONS_CACHE["sessions"] = sessions
    _SESSIONS_CACHE["at"] = time.time()

    # Refresh the iTermID -> claudeSessionID cache so mutation endpoints can resolve
    # incoming keys to their canonical form
    _LIVE_KEY_MAP.clear()
    for s in sessions:
        if s.get("isInactive"):
            continue
        iterm = s.get("itermId", "")
        claude = s.get("sessionId", "")
        if iterm and claude and iterm != claude:
            _LIVE_KEY_MAP[iterm] = claude

    return sessions


# ---------------------------------------------------------------------------
# Navigate to iTerm2 session
# ---------------------------------------------------------------------------

def activate_iterm_session(iterm_id):
    """Bring an existing iTerm2 session to front. Returns True on success."""
    if not iterm_id:
        return False
    script = f"""
    tell application "iTerm2"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if id of s is "{iterm_id}" then
                        select s
                        select t
                        set index of w to 1
                        activate
                        return "ok"
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=5
        )
        return "ok" in result.stdout
    except Exception as e:
        print(f"activate_iterm_session error: {e}")
        return False


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):
        if self.path == "/api/sessions":
            store = load_store()
            self.send_json({
                "sessions": get_all_sessions(),
                "priorities": parse_priorities(),
                "panels": load_panels(),
                "colorGroups": store.get("color_groups", {}),
            })
        elif self.path == "/api/task-files":
            files = []
            # Sessions almost always run at the repo root rather than inside a
            # project, so linking is how a session gets a project, not
            # discovery. Offer the store's registered projects first: they
            # carry status and open counts, and rglob over ~/Projects returns
            # archived and unregistered lists indiscriminately.
            store = get_store()
            if store is not None and store_is_live():
                for project in store.projects():
                    if project["key"] == mhstore.ONE_OFF:
                        continue
                    if project["status"] in mhstore.ARCHIVED_PROJECT_STATUSES:
                        continue
                    open_count = len(store.tasks(project_key=project["key"],
                                                 open_only=True))
                    path = (MELLONHEAD_ROOT / project["dir"] / "task-list.md"
                            if project["dir"] else MELLONHEAD_ROOT)
                    files.append({
                        "path": str(path),
                        "shortPath": f"{project['key']}  ({open_count} open)",
                        "projectName": project["name"],
                        "projectKey": project["key"],
                        "status": project["status"],
                    })
                files.sort(key=lambda f: (f["status"] != "active", f["projectKey"]))
                self.send_json(files)
                return

            projects = Path.home() / "Projects"
            if projects.exists():
                for tf in projects.rglob("task-list.md"):
                    try:
                        parsed = parse_task_list(tf)
                        files.append({
                            "path": str(tf),
                            "shortPath": str(tf).replace(str(Path.home()), "~"),
                            "projectName": parsed.get("projectName", ""),
                        })
                    except Exception:
                        files.append({
                            "path": str(tf),
                            "shortPath": str(tf).replace(str(Path.home()), "~"),
                            "projectName": "",
                        })
            self.send_json(files)
        elif self.path == "/api/tasks":
            self.send_json({"tasks": inflight_tasks(),
                            "available": get_store() is not None})
        elif self.path.startswith("/api/task/"):
            try:
                task_id = int(self.path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_json({"error": "Bad task id"}, 400)
                return
            payload = task_view_payload(task_id)
            if payload is None:
                self.send_json({"error": "No such task"}, 404)
                return
            self.send_json(payload)
        elif self.path.split("?", 1)[0] == "/":
            self.send_html(HTML_PAGE)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/navigate":
            body = self.read_body()
            iterm_id = body.get("itermId", "")
            activated = activate_iterm_session(iterm_id)
            if not activated:
                # Fallback: iTerm tab is gone (closed, iTerm restarted, etc.).
                # Spawn a new tab with `claude --resume <id>` if we know the Claude session.
                claude_id = body.get("claudeSessionId", "").strip()
                cwd = body.get("cwd", "").strip()
                display_name = body.get("displayName", "").strip()
                if claude_id:
                    resumed = resume_claude_session(cwd, claude_id)
                    if resumed:
                        # Track for state migration when the new live session appears
                        _PENDING_RESUMES[claude_id] = (cwd, time.time(), display_name)
                        # Archive the source so we don't see two cards for the same conversation
                        store = load_store()
                        store.setdefault("archivedSessions", {})[claude_id] = True
                        save_store(store)
                    self.send_json({"ok": resumed, "resumed": True})
                    return
            self.send_json({"ok": activated})

        elif self.path == "/api/resume":
            body = self.read_body()
            cwd = body.get("cwd", "").strip()
            claude_session_id = body.get("claudeSessionId", "").strip()
            # Capture the inactive card's display name so we can carry it over to the new live session
            display_name = body.get("displayName", "").strip()
            if not claude_session_id:
                self.send_json({"ok": False, "error": "Missing claudeSessionId"}, 400)
                return
            ok = resume_claude_session(cwd, claude_session_id)
            if ok:
                # Archive the source — claude --resume creates a NEW session ID,
                # so the inactive card would otherwise hang around as a duplicate.
                store = load_store()
                store.setdefault("archivedSessions", {})[claude_session_id] = True
                save_store(store)
                # Track for state migration when the new live session appears
                _PENDING_RESUMES[claude_session_id] = (cwd, time.time(), display_name)
            self.send_json({"ok": ok})
            return

        elif self.path == "/api/archive":
            body = self.read_body()
            store = load_store()
            sid = body.get("claudeSessionId", "").strip() or body.get("itermId", "").strip()
            archived = bool(body.get("archived", True))
            if sid:
                arch_map = store.setdefault("archivedSessions", {})
                if archived:
                    arch_map[sid] = True
                else:
                    arch_map.pop(sid, None)
                save_store(store)
            self.send_json({"ok": True})
            return

        elif self.path == "/api/tag":
            body = self.read_body()
            print(f"TAG API: {body}")
            store = load_store()
            raw_key = body.get("key", "") or body.get("cwd", "")
            # CWD-keyed tags pass through unchanged; iTermID-keyed tags get canonicalized to claudeSessionID
            key = canonical_key(raw_key)
            action = body.get("action", "add")
            tag = body.get("tag", "").strip()
            print(f"  key={key!r} (raw={raw_key!r}) tag={tag!r} action={action!r}")
            if key and tag:
                tags_entry = store.setdefault("tags", {}).setdefault(key, {"manual": []})
                if action == "add" and tag not in tags_entry["manual"]:
                    tags_entry["manual"].append(tag)
                elif action == "remove" and tag in tags_entry["manual"]:
                    tags_entry["manual"].remove(tag)
                save_store(store)
            self.send_json({"ok": True})

        elif self.path == "/api/toggle-priority-item":
            body = self.read_body()
            text = body.get("text", "").strip()
            done = body.get("done", False)
            task_id = body.get("id")

            # The store path is exact: the checkbox carries the task's id, so
            # there is no text matching and no ambiguity when two day-card
            # lines read alike.
            store = get_store()
            if task_id is not None and store is not None:
                try:
                    task = store.task(int(task_id))
                except (TypeError, ValueError):
                    task = None
                if task is None:
                    self.send_json({"ok": False, "error": "No such task"}, 404)
                    return
                if done:
                    store.complete_task(task["id"], actor="mq")
                else:
                    store.reopen_task(task["id"], actor="mq",
                                      status="planned" if task["planned_day"]
                                      else "backlog")
                regenerate_views([task["project_key"]])
                self.send_json({"ok": True, "id": task["id"]})
                return

            if text and PRIORITIES_FILE.exists():
                with open(PRIORITIES_FILE) as f:
                    content = f.read()
                today = time.strftime("%Y-%m-%d")
                # Find the line matching this task text
                escaped = re.escape(text)
                if done:
                    # Mark as done: [ ] -> [x], append date
                    pattern = r"- \[ \] " + escaped
                    replacement = f"- [x] {text} (done {today})"
                else:
                    # Mark as undone: [x] -> [ ], strip date note
                    pattern = r"- \[[xX]\] " + escaped + r"(?:\s*\(done [0-9-]+\))?"
                    replacement = f"- [ ] {text}"
                new_content = re.sub(pattern, replacement, content, count=1)
                if new_content != content:
                    with open(PRIORITIES_FILE, "w") as f:
                        f.write(new_content)
                    self.send_json({"ok": True})
                else:
                    self.send_json({"ok": False, "error": "Line not found"}, 400)
            else:
                self.send_json({"ok": False}, 400)
            return

        elif self.path in ("/api/question/answer", "/api/question/accept"):
            # MQ's answers, written with actor=mq so the record says she
            # decided, not the dashboard. The trimmed list comes back so the
            # page can redraw without another round trip.
            body = self.read_body()
            store = get_store()
            if store is None:
                self.send_json({"ok": False, "error": "No store"}, 503)
                return
            try:
                qid = int(body.get("id"))
            except (TypeError, ValueError):
                self.send_json({"ok": False, "error": "Bad question id"}, 400)
                return
            try:
                if self.path.endswith("/accept"):
                    q = store.answer_question(qid, actor="mq", accept=True,
                                              source="dashboard")
                else:
                    q = store.answer_question(qid, (body.get("answer") or "").strip(),
                                              actor="mq", source="dashboard")
            except mhstore.StoreError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
                return
            regenerate_views([q["project_key"]])
            remaining = {"task": [], "project": []}
            if q["task_id"]:
                remaining["task"] = [_question_item(x, store)
                                     for x in store.questions(task_id=q["task_id"])]
            remaining["project"] = [_question_item(x, store)
                                    for x in store.questions(project_key=q["project_key"])
                                    if x["task_id"] != q["task_id"]]
            self.send_json({"ok": True, "question": _question_item(q, store),
                            "questions": remaining})
            return

        elif self.path == "/api/task/link":
            # A conversation is about this task. Written beside the path the
            # old link keeps, and recorded as a link event so the task page
            # lists the conversation even before it has written anything.
            body = self.read_body()
            store = get_store()
            state = load_store()
            key = canonical_key(body.get("itermId", ""))
            try:
                task = store.task(int(body.get("taskId"))) if store else None
            except (TypeError, ValueError):
                task = None
            if not key or task is None:
                self.send_json({"ok": False, "error": "Need itermId and a real taskId"}, 400)
                return
            project = store.project(task["project_key"]) or {}
            path = (str(MELLONHEAD_ROOT / project["dir"] / "task-list.md")
                    if project.get("dir") else "")
            assignments = state.setdefault("taskAssignments", {})
            current = [a for a in assignment_list(assignments.get(key))
                       if a.get("taskId") != task["id"]]
            current.append({
                "taskFile": path, "notionTaskId": task["notion_task_id"] or "",
                "taskTitle": task["title"], "taskId": task["id"],
                "projectKey": task["project_key"],
            })
            assignments[key] = current
            if path and state.get("taskLinks", {}).get(key) in (None, "__none__"):
                state.setdefault("taskLinks", {})[key] = path
            save_store(state)
            iterm_id = next((s.get("itermId") for s in cached_sessions()
                             if (s.get("claudeSessionId") or s.get("itermId")) == key), None)
            sid = key if len(key) == 36 and key.count("-") == 4 else None
            try:
                store.link_session(task["id"], actor="mq", session_id=sid,
                                   iterm_id=iterm_id or body.get("itermId"))
            except mhstore.StoreError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
                return
            self.send_json({"ok": True, "taskId": task["id"]})
            return

        elif self.path == "/api/task/unlink":
            # Take one task off a card, and say so on the task: an unlink
            # event supersedes the link, so the page stops listing it.
            body = self.read_body()
            state = load_store()
            key = canonical_key(body.get("itermId", ""))
            try:
                task_id = int(body.get("taskId"))
            except (TypeError, ValueError):
                self.send_json({"ok": False, "error": "Bad taskId"}, 400)
                return
            assignments = state.setdefault("taskAssignments", {})
            remaining = [a for a in assignment_list(assignments.get(key))
                         if a.get("taskId") != task_id]
            if remaining:
                assignments[key] = remaining
            else:
                assignments.pop(key, None)
            save_store(state)
            store = get_store()
            sid = key if len(key) == 36 and key.count("-") == 4 else None
            if store is not None and sid and store.task(task_id):
                try:
                    store.unlink_session(task_id, actor="mq", session_id=sid)
                except mhstore.StoreError as exc:
                    print(f"unlink event failed: {exc}")
            self.send_json({"ok": True, "remaining": len(remaining)})
            return

        elif self.path == "/api/task/orca":
            # Resume the conversation that last touched the task, or open a
            # fresh Orca on it. Same shape as "Review with Orca" for a week.
            body = self.read_body()
            store = get_store()
            try:
                task = store.task(int(body.get("taskId"))) if store else None
            except (TypeError, ValueError):
                task = None
            if task is None:
                self.send_json({"ok": False, "error": "No such task"}, 404)
                return
            found = find_task_session(store, task["id"])
            resume_id, cwd = (found if found else (None, str(MELLONHEAD_ROOT)))
            prompt = task_orca_prompt(store, task, resuming=bool(resume_id))
            ok = launch_claude_session(cwd or str(MELLONHEAD_ROOT), prompt,
                                       resume_id=resume_id, agent="orca",
                                       prompt_name=f"task-{task['id']}-prompt.md")
            self.send_json({"ok": ok, "taskId": task["id"], "resumed": resume_id})
            return

        # -------------------------------------------------------------
        # Store write paths (2.4). Everything here addresses a row by id,
        # so nothing depends on matching the text of a line.
        # -------------------------------------------------------------
        elif self.path.startswith("/api/task/") or self.path.startswith("/api/plan/"):
            body = self.read_body()
            store = get_store()
            if store is None:
                self.send_json({"ok": False, "error": "No store"}, 503)
                return
            action = self.path.rsplit("/", 1)[-1]

            def wanted_task():
                try:
                    return store.task(int(body.get("id")))
                except (TypeError, ValueError):
                    return None

            try:
                if action == "add":
                    title = (body.get("title") or "").strip()
                    project_key = body.get("projectKey") or mhstore.ONE_OFF
                    if not title:
                        self.send_json({"ok": False, "error": "No title"}, 400)
                        return
                    if not store.project(project_key):
                        self.send_json({"ok": False,
                                        "error": f"No project {project_key}"}, 404)
                        return
                    task = store.add_task(
                        project_key, title, actor="mq", source="manual",
                        status="planned" if body.get("plannedDay") else "backlog",
                        planned_day=body.get("plannedDay"),
                        load=body.get("load"), due=body.get("due"))
                    regenerate_views([project_key])
                    self.send_json({"ok": True, "id": task["id"]})
                    return

                if action == "lock":
                    # A week, not a task: no id to look up.
                    week_start = body.get("weekStart")
                    if not week_start:
                        self.send_json({"ok": False, "error": "No week"}, 400)
                        return
                    store.lock_week(week_start, actor="mq")
                    regenerate_views(
                        {t["project_key"] for t in store.tasks() if t["planned_day"]})
                    self.send_json({"ok": True, "weekStart": week_start})
                    return

                task = wanted_task()
                if task is None:
                    self.send_json({"ok": False, "error": "No such task"}, 404)
                    return

                if action == "plan":
                    day = body.get("day")
                    if day:
                        store.plan_task(task["id"], day, actor="mq")
                    else:
                        store.unplan_task(task["id"], actor="mq")
                elif action == "done":
                    store.complete_task(task["id"], actor="mq")
                elif action == "reopen":
                    store.reopen_task(
                        task["id"], actor="mq",
                        status="planned" if task["planned_day"] else "backlog")
                elif action == "confirm":
                    store.confirm_task(task["id"], actor="mq")
                elif action == "dismiss":
                    # A proposal MQ does not want. Cancelled, not deleted:
                    # the row and its audit line stay.
                    store.update_task(task["id"], actor="mq", status="canceled")
                elif action == "load":
                    store.update_task(task["id"], actor="mq",
                                      load=body.get("load") or None)
                else:
                    self.send_json({"ok": False, "error": "Unknown action"}, 404)
                    return
                regenerate_views([task["project_key"]])
                self.send_json({"ok": True, "id": task["id"]})
            except mhstore.StoreError as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return

        elif self.path == "/api/review-week":
            body = self.read_body()
            store = get_store()
            week = body.get("weekStart")
            if store is None or not week:
                self.send_json({"ok": False, "error": "No store or week"}, 400)
                return
            session_id = find_proposal_session(week)
            prompt = week_review_prompt(store, week, resuming=bool(session_id))
            ok = launch_claude_session(str(MELLONHEAD_ROOT), prompt,
                                       resume_id=session_id)
            self.send_json({"ok": ok, "weekStart": week,
                            "resumed": session_id})
            return

        elif self.path == "/api/set-review":
            body = self.read_body()
            store = load_store()
            iterm_id = canonical_key(body.get("itermId", ""))
            flag = bool(body.get("flag", False))
            if iterm_id:
                reviews = store.setdefault("needsReview", {})
                if flag:
                    reviews[iterm_id] = True
                else:
                    reviews.pop(iterm_id, None)
                save_store(store)
            self.send_json({"ok": True})

        elif self.path == "/api/set-next-step":
            body = self.read_body()
            store = load_store()
            iterm_id = canonical_key(body.get("itermId", ""))
            text = body.get("text", "").strip()
            if iterm_id:
                steps = store.setdefault("nextSteps", {})
                if text:
                    steps[iterm_id] = text
                else:
                    steps.pop(iterm_id, None)
                save_store(store)
            self.send_json({"ok": True})

        elif self.path == "/api/set-cwd":
            body = self.read_body()
            store = load_store()
            iterm_id = canonical_key(body.get("itermId", ""))
            new_cwd = body.get("cwd", "").strip()
            if iterm_id:
                overrides = store.setdefault("cwdOverrides", {})
                if new_cwd:
                    expanded = str(Path(new_cwd).expanduser())
                    if Path(expanded).is_dir():
                        overrides[iterm_id] = expanded
                        save_store(store)
                        self.send_json({"ok": True})
                    else:
                        self.send_json({"ok": False, "error": "Not a valid directory"}, 400)
                else:
                    overrides.pop(iterm_id, None)
                    save_store(store)
                    self.send_json({"ok": True})
            else:
                self.send_json({"ok": False}, 400)
            return

        elif self.path == "/api/todo":
            body = self.read_body()
            raw_key = body.get("itermId", "")
            iterm_id = canonical_key(raw_key)  # Resolve to claudeSessionId when live
            action = body.get("action", "")
            if not iterm_id:
                self.send_json({"ok": False}, 400)
                return

            # Read from canonical key first; fall back to legacy iTerm-ID-keyed file
            todo = read_todo(iterm_id) or (read_todo(raw_key) if raw_key != iterm_id else None)
            metadata = todo["metadata"] if todo else {}
            items = todo["items"] if todo else []

            if action == "add":
                text = body.get("text", "").strip()
                if text:
                    # Insert before first unchecked item, or at end
                    insert_at = len(items)
                    for i, item in enumerate(items):
                        if not item["done"]:
                            insert_at = i + 1  # after the current next
                            break
                    items.insert(insert_at, {"done": False, "text": text})

            elif action == "check":
                idx = body.get("index", -1)
                if 0 <= idx < len(items):
                    items[idx]["done"] = True

            elif action == "uncheck":
                idx = body.get("index", -1)
                if 0 <= idx < len(items):
                    items[idx]["done"] = False

            elif action == "edit":
                idx = body.get("index", -1)
                text = body.get("text", "").strip()
                if 0 <= idx < len(items) and text:
                    items[idx]["text"] = text

            elif action == "remove":
                idx = body.get("index", -1)
                if 0 <= idx < len(items):
                    items.pop(idx)

            elif action == "reorder":
                from_idx = body.get("from", -1)
                to_idx = body.get("to", -1)
                if 0 <= from_idx < len(items) and 0 <= to_idx < len(items):
                    item = items.pop(from_idx)
                    items.insert(to_idx, item)

            elif action == "set_metadata":
                # Update metadata (e.g., when linking to a Notion task)
                new_meta = body.get("metadata", {})
                metadata.update(new_meta)

            elif action == "init":
                # Initialize a todo file with metadata (called on task link)
                new_meta = body.get("metadata", {})
                metadata.update(new_meta)
                # Don't overwrite existing items

            write_todo(iterm_id, metadata, items)
            # Clean up the legacy iTerm-ID-keyed file if it differs from canonical
            if raw_key and raw_key != iterm_id:
                legacy = todo_path(raw_key)
                if legacy.exists():
                    try:
                        legacy.unlink()
                    except OSError:
                        pass
            self.send_json({"ok": True})
            return

        elif self.path == "/api/complete-task":
            body = self.read_body()
            task_file = body.get("taskFile", "").strip()
            task_number = body.get("taskNumber", 0)

            # Same single close path as the checkbox: one write, by id.
            store = get_store()
            task_id = body.get("id")
            if task_id is not None and store is not None:
                try:
                    task = store.task(int(task_id))
                except (TypeError, ValueError):
                    task = None
                if task is None:
                    self.send_json({"ok": False, "error": "No such task"}, 404)
                    return
                store.complete_task(task["id"], actor="mq")
                nxt = store.next_task(task["project_key"])
                regenerate_views([task["project_key"]])
                self.send_json({"ok": True, "id": task["id"],
                                "next": nxt["title"] if nxt else None})
                return

            if task_file and task_number:
                expanded = str(Path(task_file).expanduser())
                if Path(expanded).exists():
                    with open(expanded) as f:
                        content = f.read()
                    # Find the row for this task number and change status to Done
                    pattern = re.compile(
                        r"(\|\s*" + str(task_number) + r"\s*\|[^|]+\|\s*)(Not started|In progress|On hold)(\s*\|)",
                        re.IGNORECASE
                    )
                    new_content = pattern.sub(r"\1Done\3", content, count=1)
                    if new_content != content:
                        with open(expanded, "w") as f:
                            f.write(new_content)
                        self.send_json({"ok": True})
                    else:
                        self.send_json({"ok": False, "error": "Task not found or already done"}, 400)
                else:
                    self.send_json({"ok": False, "error": "File not found"}, 404)
            else:
                self.send_json({"ok": False}, 400)
            return

        elif self.path == "/api/create-task":
            body = self.read_body()
            task_file = body.get("taskFile", "").strip()
            title = body.get("title", "").strip()

            store = get_store()
            project_key = body.get("projectKey")
            if store is not None and title and (project_key or task_file):
                if not project_key:
                    project = project_for_cwd(str(Path(task_file).expanduser().parent))
                    project_key = project["key"] if project else None
                if project_key and store.project(project_key):
                    task = store.add_task(
                        project_key, title, actor="mq", source="manual",
                        status="backlog", planned_day=body.get("plannedDay"),
                        load=body.get("load"), due=body.get("due"))
                    self.send_json({"ok": True, "id": task["id"],
                                    "taskNumber": task["id"],
                                    "projectKey": project_key})
                    return

            if task_file and title:
                expanded = str(Path(task_file).expanduser())
                if Path(expanded).exists():
                    with open(expanded) as f:
                        content = f.read()
                    # Find the highest task number
                    numbers = [int(m.group(1)) for m in re.finditer(r"\|\s*(\d+)\s*\|", content)]
                    next_num = max(numbers) + 1 if numbers else 1
                    # Insert new row before the first blank line after the table
                    new_row = f"| {next_num} | {title} | Not started | `PENDING-NOTION-SYNC` | Needs Notion sync |\n"
                    # Find end of table (last row starting with |)
                    lines = content.split("\n")
                    insert_idx = len(lines)
                    in_table = False
                    for i, line in enumerate(lines):
                        if line.strip().startswith("|") and "---" not in line and "#" not in line[:3]:
                            in_table = True
                        elif in_table and not line.strip().startswith("|"):
                            insert_idx = i
                            break
                    lines.insert(insert_idx, new_row.rstrip())
                    with open(expanded, "w") as f:
                        f.write("\n".join(lines))
                    self.send_json({"ok": True, "taskNumber": next_num})
                else:
                    self.send_json({"ok": False, "error": "File not found"}, 404)
            else:
                self.send_json({"ok": False, "error": "Missing taskFile or title"}, 400)
            return

        elif self.path == "/api/task-tasks":
            body = self.read_body()
            task_file = body.get("taskFile", "").strip()

            project_key = body.get("projectKey")
            if not project_key and task_file:
                project = project_for_cwd(str(Path(task_file).expanduser().parent))
                project_key = project["key"] if project else None
            if project_key:
                payload = task_list_from_store(project_key)
                if payload is not None:
                    self.send_json(payload)
                    return

            if task_file:
                expanded = str(Path(task_file).expanduser())
                if Path(expanded).exists():
                    parsed = parse_task_list(Path(expanded))
                    self.send_json(parsed)
                else:
                    self.send_json({"error": "File not found"}, 404)
            else:
                self.send_json({"error": "No file"}, 400)
            return

        elif self.path == "/api/link-task":
            body = self.read_body()
            store = load_store()
            iterm_id = canonical_key(body.get("itermId", ""))
            task_file = body.get("taskFile", "").strip()
            notion_task_id = body.get("notionTaskId", "").strip()
            task_title = body.get("taskTitle", "").strip()
            if iterm_id:
                links = store.setdefault("taskLinks", {})
                assignments = store.setdefault("taskAssignments", {})
                if task_file == "__none__":
                    links[iterm_id] = "__none__"
                    assignments.pop(iterm_id, None)
                    save_store(store)
                    self.send_json({"ok": True})
                elif task_file:
                    expanded = str(Path(task_file).expanduser())
                    if Path(expanded).exists():
                        links[iterm_id] = expanded
                        if notion_task_id or body.get("taskId"):
                            entry = {
                                "taskFile": expanded,
                                "notionTaskId": notion_task_id,
                                "taskTitle": task_title,
                            }
                            # The store row, when the picker came from the
                            # store: the task page joins on this.
                            task_store = get_store()
                            linked = None
                            if task_store is not None:
                                try:
                                    linked = task_store.task(int(body.get("taskId")))
                                except (TypeError, ValueError):
                                    linked = None
                                if linked is None and notion_task_id:
                                    wanted = notion_task_id.replace("-", "").lower()
                                    for row in task_store.tasks():
                                        if (row["notion_task_id"] or "").replace("-", "").lower() == wanted:
                                            linked = row
                                            break
                            entry["taskId"] = linked["id"] if linked else None
                            entry["projectKey"] = linked["project_key"] if linked else None
                            # One card, several tasks: add rather than replace.
                            current = [a for a in assignment_list(assignments.get(iterm_id))
                                       if not (linked is not None and a.get("taskId") == linked["id"])
                                       and not (linked is None and a.get("taskTitle") == task_title)]
                            current.append(entry)
                            assignments[iterm_id] = current
                            if linked is not None:
                                sid = iterm_id if len(iterm_id) == 36 and iterm_id.count("-") == 4 else None
                                try:
                                    task_store.link_session(linked["id"], actor="mq",
                                                            session_id=sid,
                                                            iterm_id=body.get("itermId", ""))
                                except Exception as exc:      # noqa: BLE001
                                    print(f"link event failed: {exc}")
                        # "Link project only" leaves the task links alone;
                        # "__none__" above is what clears them.
                        save_store(store)
                        # Auto-init todo file with Notion metadata
                        todo = read_todo(iterm_id)
                        meta = todo["metadata"] if todo else {}
                        items = todo["items"] if todo else []
                        # Get project name from task list
                        try:
                            parsed = parse_task_list(Path(expanded))
                            meta["project"] = parsed.get("projectName", "")
                            meta["notion_project_id"] = parsed.get("notionProjectId", "")
                        except Exception:
                            pass
                        if notion_task_id:
                            meta["notion_task_id"] = notion_task_id
                            meta["notion_task"] = task_title
                        write_todo(iterm_id, meta, items)
                        self.send_json({"ok": True, "resolved": expanded})
                    else:
                        self.send_json({"ok": False, "error": f"File not found: {expanded}"}, 400)
                else:
                    links.pop(iterm_id, None)
                    assignments.pop(iterm_id, None)
                    save_store(store)
                    self.send_json({"ok": True})
            else:
                self.send_json({"ok": False}, 400)
            return

        elif self.path == "/api/map-priorities":
            store = load_store()
            sessions_list = get_all_sessions()
            priorities_data = parse_priorities()
            auto_assign_priorities(sessions_list, priorities_data, store)
            # Save the auto-assigned priorities
            prio_store = store.setdefault("priorities", {})
            mapped = []
            for s in sessions_list:
                label = s.get("priorityLabel", "")
                iterm_id = s.get("itermId", "")
                if label and iterm_id:
                    old = prio_store.get(iterm_id, "")
                    if old != label and old != "__cleared__":
                        prio_store[iterm_id] = label
                        mapped.append({"name": s["name"][:50], "priority": label})
            save_store(store)
            self.send_json({"ok": True, "mapped": mapped})

        elif self.path == "/api/priority":
            body = self.read_body()
            store = load_store()
            iterm_id = canonical_key(body.get("itermId", ""))
            label = body.get("label", "").strip()
            if iterm_id:
                priorities = store.setdefault("priorities", {})
                if label:
                    priorities[iterm_id] = label
                else:
                    # Store "__cleared__" to prevent auto-assign from overriding
                    priorities[iterm_id] = "__cleared__"
                save_store(store)
            self.send_json({"ok": True})

        elif self.path == "/api/close":
            body = self.read_body()
            pid = body.get("id", "")
            iterm_id = body.get("itermId", "")
            claude_session_id = body.get("claudeSessionId", "").strip()
            # Send interrupt to the process, then close the tab
            if pid:
                try:
                    subprocess.run(["kill", "-INT", str(pid)], capture_output=True, timeout=3)
                except Exception:
                    pass
            if iterm_id:
                close_script = f"""
                tell application "iTerm2"
                    repeat with w in windows
                        repeat with t in tabs of w
                            repeat with s in sessions of t
                                if id of s is "{iterm_id}" then
                                    close s
                                    return "ok"
                                end if
                            end repeat
                        end repeat
                    end repeat
                end tell
                """
                try:
                    subprocess.run(["osascript", "-e", close_script], capture_output=True, timeout=5)
                except Exception:
                    pass
            # Archive so the conversation doesn't reappear as an inactive card
            if claude_session_id:
                store = load_store()
                store.setdefault("archivedSessions", {})[claude_session_id] = True
                save_store(store)
            self.send_json({"ok": True})

        elif self.path == "/api/rename":
            body = self.read_body()
            store = load_store()
            session_id = canonical_key(body.get("id", ""))
            new_name = body.get("name", "").strip()
            if session_id and new_name:
                store.setdefault("renames", {})[session_id] = new_name
                save_store(store)
            self.send_json({"ok": True})

        elif self.path == "/api/color":
            body = self.read_body()
            store = load_store()
            tag = body.get("tag", "").strip()
            color = body.get("color", "").strip()
            if tag and color:
                store.setdefault("color_groups", {})[tag] = color
                save_store(store)
            self.send_json({"ok": True})

        else:
            self.send_response(404)
            self.end_headers()


# ---------------------------------------------------------------------------
# Embedded HTML/CSS/JS Frontend
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Sessions</title>
<style>
:root {
    --bg: #f5f5f7;
    --surface: #ffffff;
    --surface-hover: #f0f0f3;
    --border: #d8d8dc;
    --text: #1d1d1f;
    --text-dim: #6e6e73;
    --text-faint: #aeaeb2;
    --accent: #6e56cf;
    --accent-dim: rgba(110,86,207,0.1);
    --green: #34c759;
    --red: #ff3b30;
    --orange: #ff9500;
    --yellow: #ffcc00;
    --blue: #007aff;
    --pink: #ff2d55;
    --teal: #30b0c7;
    --purple: #8944e1;
    --ready-bg: #dbeafe;
    --review-bg: #fed7aa;
    --blocked-bg: #f0f0f0;
    --blocked-border: #c0c0c0;
    --working-bg: rgba(52,199,89,0.1);
    --next-step-bg: rgba(110,86,207,0.05);
}
[data-theme="dark"] {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface-hover: #232636;
    --border: #2a2d3a;
    --text: #e4e4e7;
    --text-dim: #8b8d98;
    --text-faint: #5a5c6a;
    --accent: #a78bfa;
    --accent-dim: rgba(167,139,250,0.15);
    --green: #4ade80;
    --red: #f87171;
    --orange: #fb923c;
    --yellow: #fbbf24;
    --blue: #60a5fa;
    --pink: #f472b6;
    --teal: #2dd4bf;
    --purple: #a78bfa;
    --ready-bg: rgba(96,165,250,0.15);
    --review-bg: rgba(251,146,60,0.15);
    --blocked-bg: rgba(255,255,255,0.04);
    --blocked-border: #4a4d5a;
    --working-bg: rgba(74,222,128,0.1);
    --next-step-bg: rgba(167,139,250,0.08);
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
}

/* Header */
.header {
    padding: 20px 24px 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.header h1 {
    font-size: 20px;
    font-weight: 600;
    color: var(--text);
}
.header h1 span { color: var(--accent); }
.theme-toggle {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 4px 10px;
    font-size: 18px;
    cursor: pointer;
    transition: all 0.15s;
    color: var(--text-dim);
    line-height: 1;
}
.theme-toggle:hover { border-color: var(--accent); color: var(--accent); }
.header-meta {
    font-size: 13px;
    color: var(--text-dim);
}

/* In-flight tasks: one strip between the week and the session cards */
.tasks-strip { display: flex; gap: 8px; overflow-x: auto; padding: 0 24px 12px; align-items: stretch; }
.tasks-strip:empty { display: none; }
.task-chip {
    flex: 0 0 auto; width: 230px;
    border: 1px solid var(--border); border-radius: 8px; background: var(--surface);
    padding: 8px 10px; cursor: pointer; font-size: 12px; line-height: 1.35;
    display: flex; flex-direction: column; gap: 4px;
}
.task-chip:hover { border-color: var(--accent); }
.task-chip .tag { font-family: ui-monospace, Menlo, monospace; font-size: 10px; color: var(--accent); }
.task-chip .title { color: var(--text); overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.task-chip .chip-meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 10px; color: var(--text-dim); margin-top: auto; }
.task-chip .chip-meta .decide { color: var(--orange); font-weight: 600; }
.task-chip .chip-meta .card-status { margin-top: 0; }
.tasks-strip-label { flex: 0 0 auto; writing-mode: vertical-rl; transform: rotate(180deg); font-size: 10px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-faint); align-self: center; }

/* The task popup */
.task-modal-overlay {
    position: fixed; inset: 0; z-index: 900;
    background: rgba(0,0,0,0.35); display: flex; align-items: center; justify-content: center;
    padding: 24px;
}
.task-modal-overlay[hidden] { display: none; }
.task-modal {
    position: relative; width: min(960px, 100%); max-height: 88vh; overflow-y: auto;
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.25);
}
.task-modal-close {
    position: absolute; top: 10px; right: 14px; font-size: 22px; line-height: 1;
    color: var(--text-faint); cursor: pointer; z-index: 1;
}
.task-modal-close:hover { color: var(--text); }
.task-check { font-size: 18px; color: var(--text-faint); cursor: pointer; line-height: 1; }
.task-check:hover, .task-check.done { color: var(--green); }
.priority-text { cursor: pointer; }
.priority-text:hover { color: var(--accent); }
.priority-check { cursor: pointer; }
.conv-pick { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.conv-pick .panel-btn { font-size: 11px; padding: 3px 8px; }

/* The Tasks list and the task page */
.task-page { padding: 18px 24px 28px; }
.task-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px 14px; margin-bottom: 4px; }
.task-head h2 { font-size: 20px; font-weight: 600; }
.task-head .tag { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--accent); }
.task-meta { font-size: 12px; color: var(--text-dim); display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
.task-meta .status { text-transform: capitalize; }
.task-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }
.task-actions a.map-btn { text-decoration: none; display: inline-block; }
.task-block { margin-bottom: 22px; }
.task-block h3 {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-faint);
    margin-bottom: 8px;
    display: flex; align-items: center; gap: 8px;
}
.task-block h3 .hint { text-transform: none; letter-spacing: 0; font-weight: 400; }
.brief-header { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; color: var(--text-dim); margin-bottom: 8px; }
.brief-header b { color: var(--text); font-weight: 500; }
.brief-section { font-size: 13px; line-height: 1.5; white-space: pre-wrap; margin-bottom: 8px; }
.brief-section h4 { font-size: 12px; margin-bottom: 2px; color: var(--text-dim); }
.brief-missing { font-size: 13px; color: var(--orange); }
.q-row {
    display: flex; align-items: flex-start; gap: 10px;
    padding: 8px 10px; margin: 0 -10px 4px;
    border-radius: 6px; font-size: 13px; line-height: 1.45;
    background: var(--next-step-bg);
}
.q-row .qid { font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: var(--accent); flex-shrink: 0; padding-top: 2px; }
.q-row .qbody { flex: 1; min-width: 0; }
.q-row .qmeta { font-size: 11px; color: var(--text-dim); }
.q-row .qmeta b { color: var(--text); font-weight: 600; }
.q-row .qactions { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }
.q-row.project { background: transparent; border: 1px dashed var(--border); }
.q-rule { border-top: 1px solid var(--border); margin: 10px 0 8px; font-size: 11px; color: var(--text-faint); padding-top: 6px; }
.answer-input {
    width: 100%; font: inherit; font-size: 13px;
    background: var(--bg); border: 1px solid var(--accent); color: var(--text);
    padding: 4px 8px; border-radius: 4px; outline: none; margin-top: 6px;
}
.conv-row {
    display: grid; grid-template-columns: 14px 1fr auto auto; gap: 10px; align-items: center;
    padding: 6px 10px; margin: 0 -10px; border-radius: 6px; font-size: 13px; cursor: pointer;
}
.conv-row:hover { background: var(--accent-dim); }
.conv-row .who { font-size: 11px; color: var(--text-dim); }
.conv-row .last { font-size: 11px; color: var(--text-dim); grid-column: 2 / span 3; margin-top: -2px; }
.conv-row.dead { opacity: 0.75; }
.agents-cols { display: grid; grid-template-columns: 1fr 1.4fr; gap: 24px; }
@media (max-width: 720px) { .agents-cols { grid-template-columns: 1fr; } }
.event-row { display: grid; grid-template-columns: 82px 1fr; gap: 10px; font-size: 12px; padding: 3px 0; line-height: 1.4; }
.event-row .when { color: var(--text-faint); font-family: ui-monospace, Menlo, monospace; font-size: 11px; }
.event-row .actor { color: var(--text-dim); }
.event-row .kind { color: var(--accent); }
.event-row a { color: var(--accent); text-decoration: none; }
.event-row a:hover { text-decoration: underline; }
.next-row { font-size: 13px; padding: 4px 0; }
.next-row .who { font-weight: 600; }
.next-row .path { font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: var(--text-dim); }
.task-empty { font-size: 12px; color: var(--text-faint); padding: 2px 0; }
.priority-open, .panel-open { font-size: 10px; color: var(--accent); text-decoration: none; opacity: 0.6; margin-left: 4px; flex-shrink: 0; }
.priority-open:hover, .panel-open:hover { opacity: 1; }
.task-link-name a { color: inherit; text-decoration: none; border-bottom: 1px dotted var(--accent); }
.task-link-row { margin-top: -4px; padding-left: 18px; }
.ready-pill, .blocked-pill { font-size: 10px; padding: 1px 7px; border-radius: 10px; font-weight: 600; }
.ready-pill { background: var(--working-bg); color: var(--green); }
.blocked-pill { background: var(--review-bg); color: var(--orange); }

/* Filter bar */
.filter-bar {
    padding: 12px 24px;
    display: flex;
    align-items: center;
    gap: 8px;
}
.automation-toggle {
    display: inline-block;
    font-size: 10px;
    padding: 2px 8px;
    margin-left: 6px;
    border: 1px dashed var(--border);
    border-radius: 10px;
    color: var(--text-dim);
    opacity: 0.6;
    cursor: pointer;
    user-select: none;
}
.automation-toggle:hover { opacity: 1; border-color: var(--accent); color: var(--accent); }
.filter-tag {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background: var(--accent-dim);
    color: var(--accent);
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 13px;
}
.filter-tag .clear {
    cursor: pointer;
    opacity: 0.6;
    font-size: 15px;
}
.filter-tag .clear:hover { opacity: 1; }
.search-input {
    flex: 1;
    max-width: 300px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 12px;
    border-radius: 8px;
    font-size: 13px;
    outline: none;
}
.search-input:focus { border-color: var(--accent); }

.legend {
    display: flex;
    gap: 14px;
    margin-left: auto;
    font-size: 11px;
    color: var(--text-dim);
}
.legend-item { display: flex; align-items: center; gap: 5px; }
.legend-dot {
    width: 10px;
    height: 10px;
    border-radius: 3px;
    border-left: 3px solid;
    background: var(--surface);
}
.legend-dot.working { border-color: var(--green); background: var(--working-bg); }
.legend-dot.ready { border-color: var(--blue); background: var(--ready-bg); }
.legend-dot.needs_review { border-color: var(--orange); background: var(--review-bg); }
.legend-dot.blocked { border-color: var(--blocked-border); background: var(--blocked-bg); }

/* Priorities Bar */
.priorities-bar {
    padding: 12px 24px;
}
.priorities-week + .priorities-week {
    margin-top: 14px;
    padding-top: 14px;
    border-top: 1px dashed var(--border);
}
.priorities-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
}
.priorities-title {
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-dim);
}
.map-btn {
    font-size: 11px;
    padding: 4px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text-dim);
    cursor: pointer;
    transition: all 0.15s;
}
.map-btn:hover {
    border-color: var(--accent);
    color: var(--accent);
    background: var(--accent-dim);
}
.priorities-days {
    display: flex;
    gap: 12px;
    overflow-x: auto;
    padding-bottom: 4px;
}
.priority-day {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 8px 12px;
    flex: 1;
    min-width: 0;
}
.priority-day-name {
    font-size: 12px;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 6px;
}
.priority-item {
    font-size: 11px;
    color: var(--text-dim);
    padding: 2px 4px;
    margin: 0 -4px;
    border-radius: 4px;
    display: flex;
    align-items: flex-start;
    gap: 5px;
    line-height: 1.3;
    cursor: pointer;
}
.priority-item:hover { background: var(--accent-dim); color: var(--text); }
.priority-item.done { text-decoration: line-through; opacity: 0.45; }
.priority-check {
    flex-shrink: 0;
    font-size: 10px;
    width: 16px;
    height: 16px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    transition: all 0.15s;
}
.priority-check:hover { background: var(--accent-dim); color: var(--accent); transform: scale(1.2); }
.priority-text { flex: 1; min-width: 0; }
.priority-item[draggable="true"] { cursor: grab; }
.priority-item[draggable="true"]:active { cursor: grabbing; }
.priority-day.drop-target {
    outline: 1px dashed var(--accent);
    outline-offset: 2px;
    border-radius: 4px;
    background: var(--accent-dim);
}
.week-proposed {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-left: 6px;
    color: var(--accent);
    opacity: 0.85;
}
.week-notes {
    font-size: 11px;
    margin: 4px 0 8px;
    color: var(--text-dim);
}
.week-notes > summary {
    cursor: pointer;
    opacity: 0.55;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}
.week-notes > div {
    margin-top: 6px;
    padding: 8px 10px;
    border-left: 2px solid var(--border);
    line-height: 1.5;
    max-height: 220px;
    overflow-y: auto;
    white-space: pre-wrap;
}
.week-locked {
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    opacity: 0.55;
    margin-left: 6px;
}
.priorities-actions { display: flex; gap: 6px; }
.map-btn.primary {
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
}
.quick-add {
    font-size: 10px;
    opacity: 0.35;
    padding: 2px 4px;
    margin: 2px -4px 0;
    cursor: pointer;
    border-radius: 4px;
}
.quick-add:hover { opacity: 0.9; background: var(--accent-dim); }
.quick-add-input {
    width: 100%;
    font: inherit;
    font-size: 11px;
    padding: 2px 4px;
    border: 1px solid var(--accent);
    border-radius: 4px;
    background: var(--bg);
    color: var(--text);
}

/* Backlog / Proposed / Projects panels */
.panels-bar { display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 14px; }
.panel {
    flex: 1 1 240px;
    min-width: 220px;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 9px;
    font-size: 11px;
}
.panel[data-empty="1"] { opacity: 0.55; }
.panel > summary {
    cursor: pointer;
    font-weight: 600;
    letter-spacing: 0.02em;
    list-style: none;
}
.panel > summary::-webkit-details-marker { display: none; }
.panel-count { opacity: 0.5; font-weight: 400; margin-left: 4px; }
.panel-body { margin-top: 6px; max-height: 260px; overflow-y: auto; }
.panel-item {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    padding: 3px 4px;
    margin: 0 -4px;
    border-radius: 4px;
    line-height: 1.35;
    cursor: grab;
}
.panel-item:hover { background: var(--accent-dim); }
.panel-item-text { flex: 1; min-width: 0; }
.panel-item-key { flex-shrink: 0; opacity: 0.45; font-size: 9px; align-self: center; }
.panel-next { opacity: 0.75; }
.panel-btn {
    flex-shrink: 0;
    font: inherit;
    font-size: 9px;
    padding: 1px 5px;
    border: 1px solid var(--border);
    border-radius: 3px;
    background: transparent;
    color: var(--text-dim);
    cursor: pointer;
    text-decoration: none;
}
.panel-btn:hover { border-color: var(--accent); color: var(--accent); }
.panel-empty { opacity: 0.45; padding: 3px 0; }
.priority-load {
    flex-shrink: 0;
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    opacity: 0.5;
    align-self: center;
}

/* Inline Tag Cloud */
.tag-cloud-inline {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    flex: 1;
}
.inline-tag {
    cursor: pointer;
    padding: 3px 10px;
    border-radius: 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text-dim);
    font-size: 12px;
    transition: all 0.15s;
    white-space: nowrap;
}
.inline-tag:hover {
    background: var(--accent-dim);
    border-color: var(--accent);
    color: var(--accent);
}
.inline-tag.active {
    background: var(--accent-dim);
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
}

/* Card Grid */
.card-grid {
    padding: 16px 24px;
    display: block;
}

.priority-section { margin-bottom: 24px; }
.priority-section-header {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-dim);
    padding: 8px 0;
    cursor: pointer;
    user-select: none;
    display: flex;
    align-items: center;
    gap: 8px;
}
.priority-section-header:hover { color: var(--text); }
.priority-section-caret { font-size: 10px; width: 12px; }
.priority-section-count {
    font-size: 11px;
    color: var(--text-faint);
    background: var(--surface);
    padding: 1px 8px;
    border-radius: 10px;
    margin-left: auto;
}
.priority-section-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 10px;
}

/* Session Card */
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    cursor: pointer;
    transition: all 0.2s;
    position: relative;
    border-left: 3px solid transparent;
}
.card:hover { background: var(--surface-hover); }
.card.working { border-left-color: var(--green); }
.card.ready { background: var(--ready-bg); border-left: 6px solid var(--blue); }
.card.needs_review { background: var(--review-bg); border-left: 6px solid var(--orange); }
.card.blocked { background: var(--blocked-bg); border-left-color: var(--blocked-border); color: var(--text-dim); }
.card.blocked .card-name { color: var(--text-dim); }
.card.blocked .card-cwd { color: var(--text-faint); }
.card.inactive {
    background: var(--blocked-bg);
    border-left: 6px dashed var(--blocked-border);
    opacity: 0.85;
}
.card.inactive .card-name { color: var(--text-dim); }
.card.inactive .card-cwd { color: var(--text-faint); }
.card.inactive:hover { opacity: 1; }
.inactive-badge {
    display: inline-block;
    font-size: 10px;
    padding: 1px 6px;
    background: var(--text-faint);
    color: var(--surface);
    border-radius: 4px;
    margin-left: 6px;
    vertical-align: middle;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

/* Color stripe */
.card[data-color="purple"] { border-left-color: var(--purple); }
.card[data-color="green"] { border-left-color: var(--green); }
.card[data-color="blue"] { border-left-color: var(--blue); }
.card[data-color="red"] { border-left-color: var(--red); }
.card[data-color="orange"] { border-left-color: var(--orange); }
.card[data-color="yellow"] { border-left-color: var(--yellow); }
.card[data-color="pink"] { border-left-color: var(--pink); }
.card[data-color="teal"] { border-left-color: var(--teal); }
.card.active[data-color="purple"] { background: rgba(137,68,225,0.06); }
.card.active[data-color="green"] { background: rgba(52,199,89,0.06); }
.card.active[data-color="blue"] { background: rgba(0,122,255,0.06); }
.card.active[data-color="red"] { background: rgba(255,59,48,0.06); }
.card.active[data-color="orange"] { background: rgba(255,149,0,0.06); }
.card.active[data-color="pink"] { background: rgba(255,45,85,0.06); }
.card.active[data-color="teal"] { background: rgba(48,176,199,0.06); }

.priority-label {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 2px 8px;
    border-radius: 4px;
    background: var(--accent-dim);
    color: var(--accent);
    display: inline-block;
    margin-bottom: 6px;
    cursor: pointer;
}
.priority-label.unset {
    background: transparent;
    border: 1px dashed var(--border);
    color: var(--text-faint);
    opacity: 0;
    transition: opacity 0.15s;
}
.card:hover .priority-label.unset { opacity: 1; }

.card-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
    font-size: 12px;
}
.card-meta .card-cwd {
    font-family: 'SF Mono', 'Fira Code', monospace;
    color: var(--text-dim);
}
.card-meta .card-uptime {
    color: var(--text-faint);
    font-size: 11px;
    margin-left: auto;
}

.card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 8px;
    gap: 8px;
}
.card-name {
    font-size: 14px;
    font-weight: 600;
    line-height: 1.3;
    flex: 1 1 auto;
    min-width: 0;
    overflow-wrap: anywhere;
    word-break: break-word;
}
.card-header > div:last-child { flex-shrink: 0; }
.card-status {
    width: 8px; height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 4px;
}
.card-status.working { background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 2s infinite; }
.card-status.ready { background: var(--blue); }
.card-status.needs_review { background: var(--orange); }
.card-status.blocked { background: var(--text-faint); }
.card-status.inactive { background: transparent; border: 1px dashed var(--text-faint); }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }


/* Tags */
.card-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-bottom: 10px;
}
.tag-pill {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    background: var(--accent-dim);
    color: var(--accent);
}
.tag-pill.auto { background: var(--accent-dim); color: var(--text-dim); }
.tag-pill.manual { background: var(--accent-dim); color: var(--accent); }
.tag-remove {
    margin-left: 4px;
    cursor: pointer;
    opacity: 0.5;
    font-size: 12px;
}
.tag-remove:hover { opacity: 1; }
.tag-add {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    background: transparent;
    border: 1px dashed var(--border);
    color: var(--text-faint);
    cursor: pointer;
}
.tag-add:hover { border-color: var(--accent); color: var(--accent); }

/* Next step */
.next-step {
    font-size: 12px;
    color: var(--text-dim);
    padding: 8px 10px;
    background: var(--next-step-bg);
    border-radius: 6px;
    border-left: 2px solid var(--accent);
    margin-bottom: 8px;
}
.next-step.unset {
    opacity: 0;
    transition: opacity 0.15s;
    border-left-color: var(--border);
    background: transparent;
}
.card:hover .next-step.unset { opacity: 1; }
.next-step-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-faint);
    margin-bottom: 2px;
    display: flex;
    align-items: center;
    gap: 4px;
}
.next-step-done {
    cursor: pointer;
    color: var(--text-faint);
    font-size: 12px;
    width: 18px;
    height: 18px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    border: 1px solid var(--border);
    transition: all 0.15s;
}
.next-step-done:hover {
    color: var(--green);
    border-color: var(--green);
    background: rgba(52,199,89,0.1);
}

/* Task list expandable */
.task-list {
    margin-top: 10px;
    border-top: 1px solid var(--border);
    padding-top: 10px;
    display: none;
}
.card.expanded .task-list { display: block; }
.task-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 0;
    font-size: 12px;
}
.task-status-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
}
.task-status-dot.done { background: var(--green); }
.task-status-dot.not-started { background: var(--text-faint); }
.task-status-dot.on-hold { background: var(--orange); }
.task-status-dot.in-progress { background: var(--blue); }
.task-title { flex: 1; color: var(--text-dim); }
.task-title.done { text-decoration: line-through; opacity: 0.5; }

.notion-link {
    font-size: 11px;
    color: var(--accent);
    text-decoration: none;
    display: inline-block;
    margin-top: 6px;
}
.notion-link:hover { text-decoration: underline; }

.review-toggle {
    font-size: 14px;
    color: var(--text-faint);
    cursor: pointer;
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 4px;
    opacity: 0;
    transition: all 0.15s;
}
.card:hover .review-toggle { opacity: 0.6; }
.review-toggle:hover { background: rgba(255,149,0,0.15); color: var(--orange); opacity: 1; }
.review-toggle.active { opacity: 1; color: var(--orange); }

.card-close, .card-open {
    font-size: 14px;
    color: var(--text-faint);
    cursor: pointer;
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 4px;
    opacity: 0;
    transition: all 0.15s;
}
.card-close { font-size: 16px; }
.card:hover .card-close, .card:hover .card-open { opacity: 1; }
.card-close:hover { background: rgba(248,113,113,0.2); color: var(--red); }
.card-open:hover { background: var(--accent-dim); color: var(--accent); }
.card.inactive .card-open { opacity: 0.9; color: var(--accent); }

.rename-input {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--accent);
    color: var(--text);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 14px;
    font-weight: 600;
    outline: none;
}

/* Session todos */
.session-todo {
    margin: 6px 0;
    font-size: 12px;
}
.todo-header {
    color: var(--text-faint);
    font-size: 11px;
    cursor: pointer;
    padding: 2px 0;
}
.todo-header:hover { color: var(--accent); }
.todo-items { padding-top: 4px; }
.todo-item {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    padding: 3px 0;
    line-height: 1.3;
}
.todo-item.done { opacity: 0.45; }
.todo-item.done .todo-text { text-decoration: line-through; }
.todo-check {
    flex-shrink: 0;
    cursor: pointer;
    font-size: 11px;
    width: 16px;
    height: 16px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    transition: all 0.15s;
}
.todo-check:hover { background: var(--accent-dim); color: var(--accent); }
.todo-text {
    flex: 1;
    color: var(--text-dim);
}
.todo-remove {
    color: var(--text-faint);
    cursor: pointer;
    font-size: 12px;
    opacity: 0;
    transition: opacity 0.15s;
    padding: 0 2px;
}
.todo-item:hover .todo-remove { opacity: 1; }
.todo-remove:hover { color: var(--red); }
.todo-add {
    color: var(--text-faint);
    cursor: pointer;
    padding: 4px 0;
    font-size: 11px;
}
.todo-add:hover { color: var(--accent); }

.expand-toggle {
    font-size: 11px;
    color: var(--text-faint);
    cursor: pointer;
    text-align: center;
    padding: 4px;
}
.expand-toggle:hover { color: var(--accent); }

/* Tag input overlay */
.tag-input-overlay {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
}
.tag-input-box {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    min-width: 300px;
}
.tag-input-box input {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 14px;
    outline: none;
}
.tag-input-box input:focus { border-color: var(--accent); }

.task-link-btn {
    font-size: 11px;
    color: var(--text-faint);
    border: 1px dashed var(--border);
    border-radius: 6px;
    padding: 4px 10px;
    text-align: center;
    cursor: pointer;
    margin-bottom: 8px;
    opacity: 0;
    transition: opacity 0.15s;
}
.card:hover .task-link-btn { opacity: 1; }
.task-link-btn:hover { border-color: var(--accent); color: var(--accent); }

.task-link-info {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    margin-bottom: 8px;
}
.task-link-name { color: var(--text-dim); font-weight: 500; flex: 1; }
.task-link-change {
    color: var(--accent);
    cursor: pointer;
    font-size: 14px;
    opacity: 0;
    transition: opacity 0.15s;
}
.card:hover .task-link-change { opacity: 1; }
.task-link-change:hover { color: var(--accent); }
.task-link-remove {
    color: var(--text-faint);
    cursor: pointer;
    font-size: 14px;
    padding: 2px 4px;
    border-radius: 4px;
    opacity: 0.5;
    transition: all 0.15s;
}
.task-link-remove:hover { color: var(--red); opacity: 1; background: rgba(255,59,48,0.1); }

.priority-option {
    padding: 10px 14px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 13px;
    margin-bottom: 4px;
    transition: background 0.1s;
}
.priority-option:hover { background: var(--bg); }
.priority-option.current { background: var(--accent-dim); color: var(--accent); font-weight: 600; }
.priority-option.clear { color: var(--red); font-size: 12px; margin-top: 8px; border-top: 1px solid var(--border); padding-top: 10px; }

.task-file-option {
    padding: 10px 12px;
    border-radius: 8px;
    cursor: pointer;
    margin-bottom: 4px;
    transition: background 0.1s;
}
.task-file-option:hover { background: var(--bg); }

.color-dot {
    display: inline-block;
    width: 10px; height: 10px;
    border-radius: 50%;
    margin-right: 6px;
    vertical-align: middle;
    cursor: pointer;
    flex-shrink: 0;
}
.color-dot.unset {
    border: 1px dashed var(--text-faint);
    background: transparent;
}
.palette-dot {
    display: inline-block;
    width: 28px; height: 28px;
    border-radius: 50%;
    cursor: pointer;
    transition: transform 0.1s;
    text-align: center;
    line-height: 28px;
    color: var(--text-faint);
    font-size: 14px;
}
.palette-dot:hover { transform: scale(1.2); }

.empty-state {
    text-align: center;
    padding: 80px 24px;
    color: var(--text-dim);
}
.empty-state h2 { font-size: 18px; margin-bottom: 8px; color: var(--text); }
</style>
</head>
<body>

<div class="header">
    <div>
        <h1><span>Claude</span> Sessions</h1>
    </div>
    <div class="legend">
        <span class="legend-item"><span class="legend-dot working"></span>Waiting on AI</span>
        <span class="legend-item"><span class="legend-dot ready"></span>MQ has next step</span>
        <span class="legend-item"><span class="legend-dot needs_review"></span>Needs review</span>
        <span class="legend-item"><span class="legend-dot blocked"></span>Waiting on someone else</span>
    </div>
    <button class="theme-toggle" onclick="toggleTheme()" title="Toggle dark mode">
        <span id="themeIcon">&#9789;</span>
    </button>
</div>

<div id="boardView">
<div id="prioritiesBar" class="priorities-bar"></div>
<div id="panelsBar" class="panels-bar"></div>
<div id="tasksStrip" class="tasks-strip"></div>

<div class="filter-bar">
    <span id="activeFilter"></span>
    <div id="tagCloudInline" class="tag-cloud-inline"></div>
    <input type="text" class="search-input" id="searchInput"
           placeholder="Search..." oninput="renderAll()">
</div>

<div id="cardGridView" class="card-grid"></div>
</div>
<div id="taskModal" class="task-modal-overlay" hidden onclick="closeTaskModal(event)">
    <div class="task-modal" onclick="event.stopPropagation()">
        <span class="task-modal-close" onclick="closeTaskModal()" title="Close (Esc)">×</span>
        <div id="taskView" class="task-page"></div>
    </div>
</div>
<div id="tagInputOverlay" class="tag-input-overlay" style="display:none"
     onclick="closeTagInput(event)"></div>

<script>
// Theme: apply saved preference on load
(function() {
    const saved = localStorage.getItem('theme') || 'light';
    if (saved === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
})();
function toggleTheme() {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    if (isDark) {
        document.documentElement.removeAttribute('data-theme');
        localStorage.setItem('theme', 'light');
        document.getElementById('themeIcon').innerHTML = '&#9789;';
    } else {
        document.documentElement.setAttribute('data-theme', 'dark');
        localStorage.setItem('theme', 'dark');
        document.getElementById('themeIcon').innerHTML = '&#9788;';
    }
}
// Set icon on load
document.addEventListener('DOMContentLoaded', () => {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    document.getElementById('themeIcon').innerHTML = isDark ? '&#9788;' : '&#9789;';
});

let sessions = [];
let colorGroups = {};
let priorities = {};
let panels = {available: false, backlog: [], proposed: [], projects: []};
let activeFilterTag = null;
let showAutomation = localStorage.getItem('showAutomation') === '1';
let tagInputTarget = null;
let isEditing = false;
const expandedSections = new Set();

const PALETTE = ['purple','green','blue','red','orange','pink','teal','yellow'];

let lastPrioritiesJson = null;
let lastPanelsJson = null;

// --- the task popup ---------------------------------------------------------
//
// One board, and a popup for one task, opened by #task/<id> so a task has a
// URL that can be bookmarked or pasted. The board keeps polling underneath,
// since its session list is what the task page joins against.

let openTaskId = null;
let lastTaskJson = null;
let lastTasksJson = null;
let taskData = null;

function routeFromHash() {
    const h = location.hash || '';
    const id = h.startsWith('#task/') ? Number(h.slice(6)) || null : null;
    if (id === openTaskId) return;
    openTaskId = id;
    lastTaskJson = null;
    taskData = null;
    const overlay = document.getElementById('taskModal');
    overlay.hidden = !openTaskId;
    if (openTaskId) {
        document.getElementById('taskView').innerHTML = '<div class="task-empty">Loading…</div>';
        fetchTask(true);
    }
}
window.addEventListener('hashchange', routeFromHash);
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && openTaskId && !document.querySelector('#taskView .answer-input')) closeTaskModal();
});

function openTask(id) {
    if (!id) return;
    location.hash = '#task/' + id;
}

function closeTaskModal(ev) {
    if (ev && ev.target !== ev.currentTarget) return;
    if (location.hash.startsWith('#task/')) history.replaceState(null, '', location.pathname);
    openTaskId = null;
    taskData = null;
    document.getElementById('taskModal').hidden = true;
    fetchSessions(true);
}

async function pollView() {
    fetchTasks();
    if (openTaskId) fetchTask();
}

async function fetchSessions(force = false) {
    if (isEditing && !force) return;
    try {
        const res = await fetch('/api/sessions');
        const data = await res.json();
        sessions = data.sessions;
        colorGroups = data.colorGroups || {};
        priorities = data.priorities || {};
        panels = data.panels || {available: false, backlog: [], proposed: [], projects: []};

        // The poll exists for the session cards, which really do change every
        // few seconds. The priorities bar and the panels almost never do, and
        // redrawing them anyway replaces innerHTML twelve times a minute:
        // that is what destroyed an open quick-add box and aborted a drag.
        // Only redraw a section when its data actually differs.
        const prioritiesJson = JSON.stringify(data.priorities || {});
        const panelsJson = JSON.stringify(data.panels || {});
        const prioritiesChanged = prioritiesJson !== lastPrioritiesJson;
        const panelsChanged = panelsJson !== lastPanelsJson;
        lastPrioritiesJson = prioritiesJson;
        lastPanelsJson = panelsJson;

        renderAll({priorities: prioritiesChanged, panels: panelsChanged});
    } catch(e) {
        console.error('Fetch error:', e);
    }
}

function getTagColor(tag) {
    return colorGroups[tag] || 'default';
}

function getCardColor(session) {
    // Use the first tag that has a color assigned
    for (const tag of session.allTags) {
        if (colorGroups[tag]) return colorGroups[tag];
    }
    return 'default';
}

async function mapPriorities() {
    const res = await fetch('/api/map-priorities', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'}
    });
    const data = await res.json();
    if (data.mapped?.length) {
        console.log('Mapped:', data.mapped);
    }
    fetchSessions();
}

async function togglePriorityItem(text, done, id) {
    const res = await fetch('/api/toggle-priority-item', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text, done, id})
    });
    fetchSessions(true);
    return res.ok;
}

function togglePriorityItemFromEl(el) {
    const text = el.dataset.text || '';
    // The store gives every row an id, so the write targets a row rather
    // than matching on the line's text.
    const id = el.dataset.id ? Number(el.dataset.id) : null;
    const wasDone = el.dataset.done === '1';
    // Optimistic UI flip so the user sees immediate feedback
    el.classList.toggle('done', !wasDone);
    el.dataset.done = wasDone ? '0' : '1';
    const check = el.querySelector('.priority-check');
    if (check) check.textContent = wasDone ? '○' : '✓';
    togglePriorityItem(text, !wasDone, id).then(ok => {
        if (ok) return;
        // Put it back rather than showing a state the store does not hold.
        el.classList.toggle('done', wasDone);
        el.dataset.done = wasDone ? '1' : '0';
        if (check) check.textContent = wasDone ? '✓' : '○';
    });
}

function toggleAutomation() {
    showAutomation = !showAutomation;
    try { localStorage.setItem('showAutomation', showAutomation ? '1' : '0'); } catch (e) {}
    renderAll();
}

function setFilter(tag) {
    activeFilterTag = (activeFilterTag === tag) ? null : tag;
    renderAll();
}

function clearFilter() {
    activeFilterTag = null;
    renderAll();
}

function getFilteredSessions() {
    let filtered = sessions;
    const search = document.getElementById('searchInput')?.value?.toLowerCase() || '';

    // The unattended jobs run every two hours and leave a card each time,
    // titled with a bare timestamp. They outnumber real conversations and
    // push them off screen, so they are hidden unless asked for. A search
    // still reaches them: if you are looking for a sweep by name, you want it.
    if (!showAutomation && !search) {
        filtered = filtered.filter(s => !s.isAutomation);
    }

    if (activeFilterTag) {
        filtered = filtered.filter(s =>
            s.allTags.some(t => t.toLowerCase() === activeFilterTag.toLowerCase())
        );
    }
    if (search) {
        filtered = filtered.filter(s =>
            s.name.toLowerCase().includes(search) ||
            s.shortCwd.toLowerCase().includes(search) ||
            s.allTags.some(t => t.toLowerCase().includes(search))
        );
    }
    filtered.sort((a, b) => a.name.localeCompare(b.name));
    return filtered;
}

function renderAll(changed) {
    // No argument means "redraw everything": the direct callers after a write
    // want the new state on screen regardless.
    if (!changed || changed.priorities) renderPriorities();
    if (!changed || changed.panels) renderPanels();
    renderTagCloudInline();
    renderFilterBar();
    renderCards();
}

function renderPriorities() {
    const container = document.getElementById('prioritiesBar');
    // A redraw replaces innerHTML, which destroys any open quick-add box and
    // whatever was typed into it. Flags have proved too easy to clear from
    // elsewhere, so ask the DOM directly.
    if (container.querySelector('.quick-add-input')) return;
    if (isDragging) return;
    const weeks = priorities.weeks?.length
        ? priorities.weeks
        : (priorities.days?.length ? [{title: priorities.weekTitle || 'This Week', days: priorities.days, isCurrent: true}] : []);
    if (!weeks.length) { container.innerHTML = ''; return; }

    // Filter out fully-done days, then drop weeks that ended up empty
    const visibleWeeks = weeks
        .map(w => ({...w, days: (w.days || []).filter(d => d.items.some(i => !i.done))}))
        .filter(w => w.days.length);
    if (!visibleWeeks.length) { container.innerHTML = ''; return; }

    const noCurrent = priorities.noCurrentWeek;
    const live = priorities.source === 'store';

    // The circle checks the row off; the name opens the task. Without a
    // store id (markdown fallback) the whole row still toggles.
    const renderItem = (item) => `<div class="priority-item ${item.done ? 'done' : ''}"
        data-text="${escAttr(item.text)}"${item.id != null ? ` data-id="${item.id}"` : ''}
        data-done="${item.done ? '1' : '0'}"
        ${live && item.id != null ? 'draggable="true" ondragstart="dragTaskStart(event)"' : ''}
        ${live && item.id != null ? `onclick="openTask(${item.id})"` : 'onclick="togglePriorityItemFromEl(this)"'} title="${escAttr(item.key || item.text)}">
        <span class="priority-check" ${live && item.id != null ? 'onclick="event.stopPropagation();togglePriorityItemFromEl(this.parentElement)"' : ''}>${item.done ? '✓' : '○'}</span>
        <span class="priority-text">${escHtml(item.text)}</span>
        ${item.load ? `<span class="priority-load">${escHtml(item.load)}</span>` : ''}
    </div>`;

    const renderWeek = (w, showMapBtn, idx) => {
        let suffix = '';
        if (noCurrent && idx === 0) suffix = ' <span style="opacity:0.6">· upcoming (no plan for this week)</span>';
        else if (!w.isCurrent) suffix = ' <span style="opacity:0.6">· next week</span>';
        if (w.proposed) suffix += ' <span class="week-proposed">proposed, not confirmed</span>';
        else if (w.locked) suffix += ' <span class="week-locked">locked</span>';
        return `<div class="priorities-week">
        <div class="priorities-header">
            <div class="priorities-title">${escHtml(w.title || (w.isCurrent ? 'This Week' : 'Next Week'))}${suffix}</div>
            <div class="priorities-actions">
                ${live && w.weekStart && !w.locked ? `<button class="map-btn primary" onclick="reviewWeek('${w.weekStart}')">Review with Orca</button>` : ''}
                ${live && w.weekStart && !w.locked ? `<button class="map-btn" onclick="lockWeek('${w.weekStart}')">Lock week</button>` : ''}
                ${showMapBtn ? '<button class="map-btn" onclick="mapPriorities()">Map to sessions</button>' : ''}
            </div>
        </div>
        ${w.notes ? `<details class="week-notes"><summary>why this week looks like this</summary><div>${escHtml(w.notes)}</div></details>` : ''}
        <div class="priorities-days">
            ${w.days.map(d => `<div class="priority-day"
                ${live && d.date ? `ondragover="dragOverDay(event)" ondragleave="dragLeaveDay(event)" ondrop="dropOnDay(event, '${d.date}')"` : ''}>
                <div class="priority-day-name">${escHtml(d.day)}</div>
                ${d.items.map(renderItem).join('')}
                ${live && d.date ? `<div class="quick-add" onclick="startQuickAdd(this, '${d.date}')">+ add</div>` : ''}
            </div>`).join('')}
        </div>
    </div>`;
    };

    container.innerHTML = visibleWeeks.map((w, i) => renderWeek(w, i === 0, i)).join('');
}

// --- drag a task onto a different day ------------------------------------

let isDragging = false;

function dragTaskStart(ev) {
    const id = ev.currentTarget.dataset.id;
    if (!id) return;
    ev.dataTransfer.setData('text/plain', id);
    ev.dataTransfer.effectAllowed = 'move';
    // A drag lasts seconds. The five-second poll redraws by replacing
    // innerHTML, which destroys the element under the cursor and aborts the
    // drag: the day highlights on dragover but the drop never lands.
    isDragging = true;
    ev.currentTarget.addEventListener('dragend', () => {
        isDragging = false;
        document.querySelectorAll('.drop-target')
            .forEach(el => el.classList.remove('drop-target'));
    }, {once: true});
}

function dragOverDay(ev) {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    ev.currentTarget.classList.add('drop-target');
}

function dragLeaveDay(ev) {
    ev.currentTarget.classList.remove('drop-target');
}

async function dropOnDay(ev, day) {
    ev.preventDefault();
    ev.currentTarget.classList.remove('drop-target');
    const id = Number(ev.dataTransfer.getData('text/plain'));
    if (!id) return;
    await storeAction('task/plan', {id, day});
    fetchSessions(true);
}

// --- quick add ------------------------------------------------------------

function startQuickAdd(el, day) {
    if (el.querySelector('input')) return;
    el.innerHTML = '<input class="quick-add-input" placeholder="New task, Enter to save">';
    const input = el.querySelector('input');
    isEditing = true;
    // The click that opened this box is still bubbling. A document-level
    // listener clears isEditing whenever the tag overlay is hidden, and it
    // runs after this handler, so setting the flag here alone is not enough:
    // the next poll would replace the DOM and destroy the box mid-word.
    // renderPriorities also refuses to redraw while this input exists.
    setTimeout(() => { isEditing = true; input.focus(); }, 0);

    let closed = false;
    const finish = async (save) => {
        if (closed) return;
        closed = true;
        const title = input.value.trim();
        isEditing = false;
        // Take the input out of the DOM before refreshing. renderPriorities
        // refuses to redraw while one exists, so leaving it in place blocks
        // the very refresh that would show the saved task: it saves, then
        // sits there as an open box forever.
        el.innerHTML = '+ add';
        if (save && title) {
            await storeAction('task/add', {title, plannedDay: day});
            fetchSessions(true);
        }
    };
    input.onkeydown = (e) => {
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        if (e.key === 'Escape') { e.preventDefault(); closed = true; isEditing = false;
                                  el.innerHTML = '+ add'; }
    };
    // Clicking away keeps what was typed rather than discarding it. Losing a
    // half-written task to an incidental focus change is the worse outcome.
    input.onblur = () => finish(true);
}

async function storeAction(action, payload) {
    const res = await fetch('/api/' + action, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        console.warn('store action failed', action, err);
    }
    return res.ok;
}

async function reviewWeek(weekStart) {
    const res = await fetch('/api/review-week', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({weekStart})
    });
    const data = await res.json().catch(() => ({}));
    if (!data.ok) alert('Could not open a session. Is iTerm running?');
}

async function lockWeek(weekStart) {
    if (!confirm('Lock this week? Planned rows are committed.\n\n' +
                 'If you have not talked it through yet, "Review with Orca" ' +
                 'opens a session that walks you through the assumptions first.')) return;
    await storeAction('plan/lock', {weekStart});
    fetchSessions(true);
}

async function taskAction(action, id) {
    await storeAction('task/' + action, {id});
    fetchSessions(true);
}

function renderPanels() {
    const container = document.getElementById('panelsBar');
    if (!container) return;
    if (isDragging) return;   // dragging out of a panel must survive a poll
    if (!panels.available) { container.innerHTML = ''; return; }

    const itemRow = (item, actions) => `<div class="panel-item" draggable="true"
        data-id="${item.id}" ondragstart="dragTaskStart(event)"
        title="${escAttr(item.key || item.text)}">
        <span class="panel-item-text" ${item.id != null ? `onclick="openTask(${item.id})" style="cursor:pointer"` : ''}>${escHtml(item.text)}</span>
        ${item.key ? `<span class="panel-item-key">${escHtml(item.key)}</span>` : ''}
        ${actions}
    </div>`;

    const section = (title, body, count) => `<details class="panel" ${count ? '' : 'data-empty="1"'}>
        <summary>${escHtml(title)} <span class="panel-count">${count}</span></summary>
        <div class="panel-body">${body}</div>
    </details>`;

    const proposed = panels.proposed.map(i => itemRow(i,
        `<button class="panel-btn" onclick="event.stopPropagation();taskAction('confirm',${i.id})">keep</button>
         <button class="panel-btn" onclick="event.stopPropagation();taskAction('dismiss',${i.id})">drop</button>`
    )).join('') || '<div class="panel-empty">Nothing waiting on you.</div>';

    const backlog = panels.backlog.map(i => itemRow(i, '')).join('')
        || '<div class="panel-empty">Backlog is clear.</div>';

    const projects = panels.projects.map(p => `<div class="panel-item">
        <span class="panel-item-text">
            <strong>${escHtml(p.name)}</strong>
            ${p.next ? `<br><span class="panel-next" draggable="true" data-id="${p.next.id}" ondragstart="dragTaskStart(event)" onclick="openTask(${p.next.id})" style="cursor:pointer">${escHtml(p.next.text)}</span>`
                     : '<br><span class="panel-empty">no next action</span>'}
        </span>
        <span class="panel-item-key">${p.openCount} open${p.status !== 'active' ? ' · ' + escHtml(p.status) : ''}</span>
        ${p.notePath ? `<a class="panel-btn" href="${p.notePath}" onclick="event.stopPropagation()">note</a>` : ''}
    </div>`).join('') || '<div class="panel-empty">No active projects.</div>';

    container.innerHTML =
        section('Proposed', proposed, panels.proposed.length) +
        section('Backlog', backlog, panels.backlog.length) +
        section('Projects', projects, panels.projects.length);
}

function renderTagCloudInline() {
    const container = document.getElementById('tagCloudInline');
    // Count tags and track activity
    const tagData = {};
    sessions.forEach(s => {
        s.allTags.forEach(tag => {
            if (!tagData[tag]) tagData[tag] = { count: 0, hasActive: false };
            tagData[tag].count++;
            if (s.isActive) tagData[tag].hasActive = true;
        });
    });

    const entries = Object.entries(tagData).sort((a, b) => b[1].count - a[1].count);
    if (entries.length === 0) { container.innerHTML = ''; return; }

    container.innerHTML = entries.map(([tag, data]) => {
        const isActive = activeFilterTag?.toLowerCase() === tag.toLowerCase();
        return `<span class="inline-tag ${isActive ? 'active' : ''}"
                      onclick="setFilter('${tag.replace(/'/g, "\\'")}')">${tag}
                      <span style="opacity:0.5">${data.count}</span></span>`;
    }).join('');
}

function automationToggleHtml() {
    const n = sessions.filter(s => s.isAutomation).length;
    if (!n) return '';
    return `<span class="automation-toggle" onclick="toggleAutomation()"
        title="Scheduled runs: capture sweeps, /do-work, the Friday proposal, the nightly check">
        ${showAutomation ? '&#10003; ' : ''}${n} automated run${n === 1 ? '' : 's'}
        ${showAutomation ? '' : '&middot; show'}</span>`;
}

function renderFilterBar() {
    const el = document.getElementById('activeFilter');
    const tag = activeFilterTag
        ? `<span class="filter-tag">${activeFilterTag}
            <span class="clear" onclick="clearFilter()">×</span></span>`
        : '';
    el.innerHTML = tag + automationToggleHtml();
}

function renderCards() {
    const container = document.getElementById('cardGridView');
    const filtered = getFilteredSessions();

    if (filtered.length === 0) {
        container.innerHTML = '<div class="empty-state"><h2>No matching sessions</h2></div>';
        return;
    }

    // Group by priority
    const groups = {'Today':[],'This Week':[],'Ongoing':[],'Next Week':[],'Later':[]};
    filtered.forEach(s => {
        const p = s.priorityLabel || 'Ongoing';
        if (groups[p]) groups[p].push(s);
        else groups['Ongoing'].push(s);
    });

    const visibleByDefault = ['Today','This Week','Ongoing'];
    const collapsedByDefault = ['Next Week','Later'];
    const order = [...visibleByDefault, ...collapsedByDefault];

    let html = '';
    for (const section of order) {
        const items = groups[section];
        if (!items.length) continue;
        const collapsed = collapsedByDefault.includes(section) && !expandedSections.has(section);
        html += `<div class="priority-section">
            <div class="priority-section-header" onclick="toggleSection('${section}')">
                <span class="priority-section-caret">${collapsed ? '▸' : '▾'}</span>
                <span>${section}</span>
                <span class="priority-section-count">${items.length}</span>
            </div>
            <div class="priority-section-grid" style="display:${collapsed ? 'none' : 'grid'}">
                ${items.map(renderCard).join('')}
            </div>
        </div>`;
    }
    container.innerHTML = html;
}

function renderCard(s) {
        const state = s.cardState || 'ready';
        const cardColor = getCardColor(s);
        const taskHtml = renderTaskList(s);

        // Priority box
        const priorityHtml = s.priorityLabel
            ? `<div class="priority-label" onclick="event.stopPropagation();pickPriority('${s.itermId}')">${s.priorityLabel}</div>`
            : `<div class="priority-label unset" onclick="event.stopPropagation();pickPriority('${s.itermId}')">+ priority</div>`;

        // Next step — priority: session todo > manual override > assigned task > task list
        let nextStepHtml = '';
        const todoNextStep = s.todo?.nextStep || '';
        const hasTodo = s.todo && s.todo.items.length > 0;

        if (todoNextStep) {
            // Session todo is the primary source
            const todoIdx = s.todo.items.findIndex(i => !i.done);
            nextStepHtml = `<div class="next-step">
                 <div class="next-step-label">
                     <span class="next-step-done" onclick="event.stopPropagation();todoAction('${s.itermId}','check',${todoIdx})" title="Mark done">✓</span>
                     Next step
                 </div>
                 <span class="next-step-text">${escHtml(todoNextStep)}</span>
               </div>`;
        } else if (s.nextStepOverride) {
            nextStepHtml = `<div class="next-step">
                 <div class="next-step-label">Next step
                     <span class="task-link-remove" onclick="event.stopPropagation();clearNextStep('${s.itermId}')" title="Clear">×</span>
                 </div>
                 <span class="next-step-text">${escHtml(s.nextStepOverride)}</span>
               </div>`;
        } else {
            // Fall back to assigned task or task list
            let ns = null;
            const linkedIds = (s.taskAssignments || []).map(a => a.taskId).filter(Boolean);
            if (linkedIds.length && s.taskList?.tasks) {
                // The first linked task that is still open is this card's next step.
                ns = s.taskList.tasks.find(t => linkedIds.includes(t.id) && t.status.toLowerCase() !== 'done')
                    || s.taskList?.nextStep || null;
            } else if (s.taskAssignment?.notionTaskId && s.taskList?.tasks) {
                const assignedId = s.taskAssignment.notionTaskId.replace(/-/g,'');
                ns = s.taskList.tasks.find(t => (t.notionTaskId||'').replace(/-/g,'') === assignedId);
                if (ns && ns.status.toLowerCase() === 'done') ns = s.taskList?.nextStep || null;
            } else {
                ns = s.taskList?.nextStep || null;
            }
            if (ns) {
                const notionId = ns.notionTaskId || s.taskAssignment?.notionTaskId || '';
                const taskLink = notionId ? `<a class="notion-link" href="https://notion.so/${notionId.replace(/-/g,'')}" target="_blank" onclick="event.stopPropagation()">open task ↗</a>` : '';
                const doneBtn = s.taskList?.file ? `<span class="next-step-done" onclick="event.stopPropagation();completeTask('${s.taskList.file.replace(/'/g,"\\'")}',${ns.number})" title="Mark done">✓</span>` : '';
                nextStepHtml = `<div class="next-step">
                     <div class="next-step-label">${doneBtn} Next step ${taskLink}</div>
                     <span class="next-step-text">${escHtml(ns.title)}</span>
                   </div>`;
            } else {
                nextStepHtml = `<div class="next-step unset">
                     <div class="next-step-label">Next step</div>
                     <span class="next-step-text" style="color:var(--text-faint)">Add a todo below</span>
                   </div>`;
            }
        }

        // Session todo list (expandable)
        const todoItems = s.todo?.items || [];
        const todoListHtml = `<div class="session-todo">
            <div class="todo-header" onclick="event.stopPropagation();toggleExpand('todo-${s.itermId}')">
                ${todoItems.length ? `${todoItems.filter(i=>i.done).length}/${todoItems.length} done` : 'No todos'}
            </div>
            <div class="todo-items" id="todo-${s.itermId}" style="display:none">
                ${todoItems.map((item, idx) => `<div class="todo-item ${item.done ? 'done' : ''}">
                    <span class="todo-check" onclick="event.stopPropagation();todoAction('${s.itermId}','${item.done ? 'uncheck' : 'check'}',${idx})">${item.done ? '✓' : '○'}</span>
                    <span class="todo-text" ondblclick="event.stopPropagation();startTodoEdit('${s.itermId}',${idx},this)">${escHtml(item.text)}</span>
                    <span class="todo-remove" onclick="event.stopPropagation();todoAction('${s.itermId}','remove',${idx})">×</span>
                </div>`).join('')}
                <div class="todo-add" onclick="event.stopPropagation();startTodoAdd('${s.itermId}')">+ Add todo</div>
            </div>
        </div>`;

        // Notion project link
        const notionProjectHtml = s.taskList?.notionProjectId
            ? `<a class="notion-link" href="https://notion.so/${s.taskList.notionProjectId.replace(/-/g,'')}"
                  target="_blank" onclick="event.stopPropagation()">project ↗</a>`
            : '';

        const tagsHtml = s.autoTags.map(t =>
            `<span class="tag-pill auto">${t}</span>`
        ).join('') + s.manualTags.map(t =>
            `<span class="tag-pill manual">${t}<span class="tag-remove" onclick="event.stopPropagation();removeTag('${s.itermId}','${t.replace(/'/g, "\\'")}')">×</span></span>`
        ).join('') +
        `<span class="tag-add" onclick="event.stopPropagation();openTagInput('${s.itermId}','${s.id}')">+</span>`;

        const expandHtml = s.taskList?.tasks?.length
            ? `<div class="expand-toggle" onclick="event.stopPropagation();toggleExpand('card-${s.id}')">
                 ${s.taskList.tasks.length} tasks — click to expand
               </div>`
            : '';

        // Link rows: the project, then one row per linked task. A card can be
        // about several tasks; each has its own ×, and the project's × clears
        // the whole link.
        let linkHtml;
        const links = (s.taskAssignments || []).filter(a => a && (a.taskId || a.taskTitle));
        if (s.taskList || links.length) {
            const rows = links.map(ta => {
                const notion = ta.notionTaskId
                    ? `<a class="notion-link" href="https://notion.so/${ta.notionTaskId.replace(/-/g,'')}" target="_blank" onclick="event.stopPropagation()">↗</a>`
                    : '';
                const name = ta.taskId
                    ? `<a href="#task/${ta.taskId}" onclick="event.stopPropagation();openTask(${ta.taskId})" title="Open the task">${escHtml(ta.taskTitle)}</a>`
                    : escHtml(ta.taskTitle);
                const remove = ta.taskId
                    ? `<span class="task-link-remove" onclick="event.stopPropagation();unlinkOneTask('${s.itermId}',${ta.taskId})" title="Unlink this task">×</span>`
                    : '';
                return `<div class="task-link-info task-link-row">
                     <span class="task-link-name">→ ${name} ${notion}</span>${remove}
                   </div>`;
            }).join('');
            linkHtml = `<div class="task-link-info">
                 <span class="task-link-change" onclick="event.stopPropagation();openTaskLink('${s.itermId}')" title="Link another task">&#x21D7;</span>
                 <span class="task-link-name">${escHtml(s.taskList?.projectName || links[0]?.projectKey || '')}${s.isAutoLinked && !links.length ? ' <span style="font-size:10px;color:var(--text-faint)">(auto)</span>' : ''}</span>
                 <span class="task-link-remove" onclick="event.stopPropagation();unlinkTask('${s.itermId}')" title="Remove all links">×</span>
               </div>${rows}`;
        } else {
            linkHtml = `<div class="task-link-btn" onclick="event.stopPropagation();openTaskLink('${s.itermId}')">Link task list</div>`;
        }

        const stateLabels = {working:'Waiting on AI',ready:'MQ has next step',needs_review:'Needs review',blocked:'Waiting on someone else',inactive:'Inactive — click to resume'};
        const isInactive = state === 'inactive';
        const inactiveBadge = isInactive ? `<span class="inactive-badge" title="Inactive — open to resume">inactive</span>` : '';
        const reviewToggle = isInactive ? '' : `<div class="review-toggle ${s.needsReview ? 'active' : ''}"
                         onclick="event.stopPropagation();toggleReview('${s.itermId}',${!s.needsReview})"
                         title="${s.needsReview ? 'Clear needs review' : 'Mark needs review'}">${s.needsReview ? '●' : '○'}</div>`;
        const openBtn = `<div class="card-open" onclick="event.stopPropagation();navigate('${s.itermId}')" title="${isInactive ? 'Resume in new tab' : 'Open this session in iTerm2'}">↗</div>`;
        const closeBtn = isInactive
            ? `<div class="card-close" onclick="event.stopPropagation();archiveSession('${s.claudeSessionId || s.itermId}')" title="Hide from dashboard">×</div>`
            : `<div class="card-close" onclick="event.stopPropagation();closeSession('${s.id}','${s.itermId}','${s.claudeSessionId || ''}')" title="Close and archive session">×</div>`;
        return `<div class="card ${state}" data-color="${cardColor}" id="card-${s.id}">
            ${priorityHtml}
            <div class="card-header">
                <div class="card-name" ondblclick="event.stopPropagation();startRename('${s.itermId}',this)" title="Double-click to rename">${escHtml(s.name)}${inactiveBadge}</div>
                <div style="display:flex;align-items:center;gap:6px">
                    ${reviewToggle}
                    <div class="card-status ${state}" title="${stateLabels[state] || ''}"></div>
                    ${openBtn}
                    ${closeBtn}
                </div>
            </div>
            <div class="card-meta">
                <span class="card-cwd" ondblclick="event.stopPropagation();startCwdEdit('${s.itermId}',this,'${s.cwd.replace(/'/g,"\\'")}')">
                    ${escHtml(s.shortCwd || '(no session file)')}
                </span>
                ${notionProjectHtml}
                <span class="card-uptime">${s.uptime ? s.uptime : ''}</span>
            </div>
            <div class="card-tags">${tagsHtml}</div>
            ${nextStepHtml}
            ${todoListHtml}
            ${linkHtml}
            ${expandHtml}
            <div class="task-list">${taskHtml}</div>
        </div>`;
}

function toggleSection(section) {
    if (expandedSections.has(section)) expandedSections.delete(section);
    else expandedSections.add(section);
    renderCards();
}

function renderTaskList(session) {
    if (!session.taskList?.tasks?.length) return '';
    return session.taskList.tasks.map(t => {
        const statusCls = t.status.toLowerCase().replace(/\s+/g, '-');
        const titleCls = statusCls === 'done' ? 'done' : '';
        return `<div class="task-row">
            <div class="task-status-dot ${statusCls}"></div>
            <div class="task-title ${titleCls}">${escHtml(t.title)}</div>
        </div>`;
    }).join('');
}

function toggleExpand(id) {
    const el = document.getElementById(id);
    if (!el) return;
    // Card-level expand (for project task lists)
    if (id.startsWith('card-')) {
        el.classList.toggle('expanded');
    } else {
        // Inline toggle (for todo lists)
        el.style.display = el.style.display === 'none' ? '' : 'none';
    }
}

async function navigate(itermId) {
    if (isEditing) return;
    const session = sessions.find(s => s.itermId === itermId);
    if (session?.cardState === 'inactive' || session?.isInactive) {
        return resumeSession(session.claudeSessionId || itermId, session.cwd || '');
    }
    try {
        const r = await fetch('/api/navigate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                itermId,
                claudeSessionId: session?.claudeSessionId || '',
                cwd: session?.cwd || '',
                displayName: session?.name || ''
            })
        });
        const data = await r.json();
        // If iTerm couldn't find the tab, the backend spawned a fresh one — refresh after a moment
        if (data.resumed) {
            setTimeout(() => fetchSessions(true), 2000);
            setTimeout(() => fetchSessions(true), 5000);
        }
    } catch(e) {
        console.error('Navigate failed:', e);
    }
}

async function resumeSession(claudeSessionId, cwd) {
    if (!claudeSessionId) return;
    // Capture the inactive card's display name so the backend can preserve it across resume
    const session = sessions.find(s => s.claudeSessionId === claudeSessionId || s.itermId === claudeSessionId);
    const displayName = session?.name || '';
    if (session) {
        const cardEl = document.getElementById('card-' + session.id);
        if (cardEl) cardEl.style.display = 'none';
        sessions = sessions.filter(s => s !== session);
    }
    try {
        const r = await fetch('/api/resume', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({claudeSessionId, cwd, displayName})
        });
        const data = await r.json();
        if (!data.ok) {
            alert('Could not resume session. Is iTerm2 running?');
            fetchSessions(true);  // bring the card back if resume failed
        } else {
            // Poll a few times to catch the new live session as it starts
            setTimeout(() => fetchSessions(true), 2000);
            setTimeout(() => fetchSessions(true), 5000);
        }
    } catch(e) {
        console.error('Resume failed:', e);
        fetchSessions(true);
    }
}

async function archiveSession(claudeSessionId) {
    if (!claudeSessionId) return;
    // Optimistically hide the card so the UI feels responsive even if a stray edit-flag is stuck
    const session = sessions.find(s => s.claudeSessionId === claudeSessionId || s.itermId === claudeSessionId);
    if (session) {
        const cardEl = document.getElementById('card-' + session.id);
        if (cardEl) cardEl.style.display = 'none';
        sessions = sessions.filter(s => s !== session);
    }
    try {
        await fetch('/api/archive', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({claudeSessionId, archived: true})
        });
        fetchSessions(true);
    } catch(e) {
        console.error('Archive failed:', e);
    }
}

function openTagInput(key, sessionId) {
    isEditing = true;
    tagInputTarget = { key, sessionId };
    const overlay = document.getElementById('tagInputOverlay');
    overlay.style.display = 'flex';
    overlay.innerHTML = `<div class="tag-input-box" onclick="event.stopPropagation()">
        <input type="text" placeholder="Add tag..." id="tagInputField">
    </div>`;
    const input = document.getElementById('tagInputField');
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); addTag(input.value); }
        if (e.key === 'Escape') { overlay.style.display = 'none'; tagInputTarget = null; isEditing = false; }
    });
    setTimeout(() => input.focus(), 50);
}

function closeTagInput(event) {
    if (event.target === document.getElementById('tagInputOverlay')) {
        document.getElementById('tagInputOverlay').style.display = 'none';
        tagInputTarget = null;
        isEditing = false;
    }
}

// Safety: reset isEditing if overlay is hidden but flag is stuck
document.addEventListener('click', () => {
    const overlay = document.getElementById('tagInputOverlay');
    // A quick-add box is an editor too, and it does not use this overlay.
    if (document.querySelector('.quick-add-input')) return;
    if (isEditing && overlay && overlay.style.display === 'none') {
        isEditing = false;
    }
});

async function addTag(tag) {
    if (!tag.trim() || !tagInputTarget) return;
    await fetch('/api/tag', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key: tagInputTarget.key, tag: tag.trim(), action: 'add'})
    });
    document.getElementById('tagInputOverlay').style.display = 'none';
    tagInputTarget = null;
    isEditing = false;
    fetchSessions();
}

async function removeTag(key, tag) {
    await fetch('/api/tag', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key, tag, action: 'remove'})
    });
    fetchSessions();
}

function startRename(sessionId, el) {
    isEditing = true;
    const current = el.textContent;
    el.innerHTML = `<input type="text" class="rename-input" value="${escHtml(current)}">`;
    const input = el.querySelector('input');
    input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') finishRename(sessionId, input.value);
        if (e.key === 'Escape') { isEditing = false; fetchSessions(); }
    });
    input.addEventListener('blur', () => {
        setTimeout(() => finishRename(sessionId, input.value), 100);
    });
    input.addEventListener('click', (e) => e.stopPropagation());
    input.focus();
    input.select();
}

async function finishRename(sessionId, name) {
    if (!isEditing) return;
    isEditing = false;
    if (!name.trim()) { fetchSessions(); return; }
    await fetch('/api/rename', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: sessionId, name: name.trim()})
    });
    fetchSessions();
}

async function closeSession(sessionId, itermId, claudeSessionId) {
    if (!confirm('Close this Claude session? It will be archived and won\'t reappear as inactive.')) return;
    // Optimistically hide the card immediately
    const cardEl = document.getElementById('card-' + sessionId);
    if (cardEl) cardEl.style.display = 'none';
    sessions = sessions.filter(s => s.id !== sessionId);
    // Interrupt the Claude process, close the iTerm2 session, and archive the conversation
    await fetch('/api/close', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: sessionId, itermId, claudeSessionId})
    });
    setTimeout(() => fetchSessions(true), 1000);
}

function startNextStepEdit(itermId, el) {
    isEditing = true;
    const textEl = el.querySelector('.next-step-text');
    const current = textEl?.textContent?.trim() || '';
    textEl.innerHTML = `<input type="text" class="rename-input" value="${escHtml(current)}" style="font-size:12px" placeholder="What's the next step?">`;
    const input = textEl.querySelector('input');
    input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') finishNextStepEdit(itermId, input.value);
        if (e.key === 'Escape') { isEditing = false; fetchSessions(); }
    });
    input.addEventListener('blur', () => {
        setTimeout(() => finishNextStepEdit(itermId, input.value), 100);
    });
    input.addEventListener('click', (e) => e.stopPropagation());
    input.focus();
    input.select();
}

async function finishNextStepEdit(itermId, text) {
    if (!isEditing) return;
    isEditing = false;
    await fetch('/api/set-next-step', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, text: text.trim()})
    });
    fetchSessions();
}

async function todoAction(itermId, action, index) {
    await fetch('/api/todo', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, action, index})
    });
    fetchSessions();
}

function startTodoAdd(itermId) {
    isEditing = true;
    const container = document.getElementById('todo-' + itermId);
    const addEl = container?.querySelector('.todo-add');
    if (!addEl) return;
    addEl.innerHTML = `<input type="text" class="rename-input" placeholder="New todo..." style="font-size:12px">`;
    const input = addEl.querySelector('input');
    input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter' && input.value.trim()) {
            isEditing = false;
            todoAddItem(itermId, input.value.trim());
        }
        if (e.key === 'Escape') { isEditing = false; fetchSessions(); }
    });
    input.addEventListener('blur', () => {
        setTimeout(() => {
            if (isEditing && input.value.trim()) todoAddItem(itermId, input.value.trim());
            else { isEditing = false; fetchSessions(); }
        }, 100);
    });
    input.addEventListener('click', (e) => e.stopPropagation());
    input.focus();
}

async function todoAddItem(itermId, text) {
    await fetch('/api/todo', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, action: 'add', text})
    });
    fetchSessions();
}

function startTodoEdit(itermId, index, el) {
    isEditing = true;
    const current = el.textContent.trim();
    el.innerHTML = `<input type="text" class="rename-input" value="${escHtml(current)}" style="font-size:12px">`;
    const input = el.querySelector('input');
    input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') { isEditing = false; finishTodoEdit(itermId, index, input.value); }
        if (e.key === 'Escape') { isEditing = false; fetchSessions(); }
    });
    input.addEventListener('blur', () => {
        setTimeout(() => { isEditing = false; finishTodoEdit(itermId, index, input.value); }, 100);
    });
    input.addEventListener('click', (e) => e.stopPropagation());
    input.focus();
    input.select();
}

async function finishTodoEdit(itermId, index, text) {
    if (!text.trim()) return fetchSessions();
    await fetch('/api/todo', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, action: 'edit', index, text: text.trim()})
    });
    fetchSessions();
}

async function completeTask(taskFile, taskNumber) {
    await fetch('/api/complete-task', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({taskFile, taskNumber})
    });
    fetchSessions();
}

async function clearNextStep(itermId) {
    await fetch('/api/set-next-step', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, text: ''})
    });
    fetchSessions();
}

function startCwdEdit(itermId, el, currentCwd) {
    isEditing = true;
    el.innerHTML = `<input type="text" class="rename-input" value="${escHtml(currentCwd)}" style="font-size:12px">`;
    const input = el.querySelector('input');
    input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') finishCwdEdit(itermId, input.value);
        if (e.key === 'Escape') { isEditing = false; fetchSessions(); }
    });
    input.addEventListener('blur', () => {
        setTimeout(() => finishCwdEdit(itermId, input.value), 100);
    });
    input.addEventListener('click', (e) => e.stopPropagation());
    input.focus();
    input.select();
}

async function finishCwdEdit(itermId, newCwd) {
    if (!isEditing) return;
    isEditing = false;
    if (!newCwd.trim()) { fetchSessions(); return; }
    const res = await fetch('/api/set-cwd', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, cwd: newCwd.trim()})
    });
    const data = await res.json();
    if (!data.ok) alert(data.error || 'Invalid directory');
    // Clear task file cache since auto-discovery may change
    taskFilesCache = null;
    fetchSessions();
}

let taskFilesCache = null;

async function openTaskLink(itermId) {
    isEditing = true;
    const overlay = document.getElementById('tagInputOverlay');
    overlay.style.display = 'flex';

    // Step 1: Pick a project (task-list.md)
    if (!taskFilesCache) {
        overlay.innerHTML = `<div class="tag-input-box"><div style="color:var(--text-dim)">Loading task lists...</div></div>`;
        const res = await fetch('/api/task-files');
        taskFilesCache = await res.json();
    }

    const listHtml = taskFilesCache.map(tf =>
        `<div class="task-file-option" onclick="selectTaskFile('${itermId}','${tf.path.replace(/'/g,"\\'")}')">
            <div style="font-weight:600;font-size:13px">${escHtml(tf.projectName || 'Untitled')}</div>
            <div style="font-size:11px;color:var(--text-dim)">${escHtml(tf.shortPath)}</div>
        </div>`
    ).join('');

    overlay.innerHTML = `<div class="tag-input-box" onclick="event.stopPropagation()" style="max-height:400px;overflow-y:auto">
        <div style="margin-bottom:10px;font-size:13px;color:var(--text-dim)">Step 1: Select a project</div>
        ${listHtml || '<div style="color:var(--text-faint)">No task-list.md files found</div>'}
    </div>`;
}

async function selectTaskFile(itermId, path) {
    // Step 2: Pick a specific task from this project
    const overlay = document.getElementById('tagInputOverlay');
    overlay.innerHTML = `<div class="tag-input-box"><div style="color:var(--text-dim)">Loading tasks...</div></div>`;

    const res = await fetch('/api/task-tasks', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({taskFile: path})
    });
    const data = await res.json();

    const safePath = path.replace(/'/g, "\\'");
    const taskItems = (data.tasks || []).map(t => {
        const statusCls = t.status.toLowerCase().replace(/\s+/g, '-');
        const safeTitle = t.title.replace(/'/g, "\\'").replace(/"/g, '&quot;');
        const safeId = (t.notionTaskId || '').replace(/'/g, "\\'");
        return `<div class="task-file-option" onclick="linkSpecificTask('${itermId}','${safePath}','${safeId}','${safeTitle}',${t.id != null ? t.id : 'null'})">
            <div style="display:flex;align-items:center;gap:8px">
                <span class="task-status-dot ${statusCls}" style="flex-shrink:0"></span>
                <span style="font-size:13px">${escHtml(t.title)}</span>
            </div>
            <div style="font-size:10px;color:var(--text-faint);margin-left:14px">${escHtml(t.status)}${t.notionTaskId ? ' · ' + t.notionTaskId.substring(0,8) + '...' : ''}</div>
        </div>`;
    }).join('');

    overlay.innerHTML = `<div class="tag-input-box" onclick="event.stopPropagation()" style="max-height:400px;overflow-y:auto">
        <div style="margin-bottom:10px;font-size:13px;color:var(--text-dim)">Step 2: Select a task from ${escHtml(data.projectName || 'project')}</div>
        <div style="display:flex;gap:8px;border-bottom:1px solid var(--border);margin-bottom:8px;padding-bottom:12px">
            <div class="task-file-option" onclick="linkProjectOnly('${itermId}','${safePath}')" style="flex:1">
                <div style="font-size:13px;color:var(--accent)">Link project only</div>
            </div>
            <div class="task-file-option" onclick="showCreateTask('${itermId}','${safePath}')" style="flex:1">
                <div style="font-size:13px;color:var(--green)">+ New task</div>
            </div>
        </div>
        ${taskItems}
        <div class="task-file-option" onclick="openTaskLink('${itermId}')" style="margin-top:8px;border-top:1px solid var(--border);padding-top:12px">
            <div style="font-size:12px;color:var(--text-faint)">← Back to projects</div>
        </div>
    </div>`;
}

function showCreateTask(itermId, taskFile) {
    const overlay = document.getElementById('tagInputOverlay');
    const safePath = taskFile.replace(/'/g, "\\'");
    overlay.innerHTML = `<div class="tag-input-box" onclick="event.stopPropagation()">
        <div style="margin-bottom:10px;font-size:13px;color:var(--text-dim)">New task</div>
        <input type="text" class="rename-input" id="newTaskTitle" placeholder="Task title..." style="margin-bottom:10px">
        <div style="display:flex;gap:8px;justify-content:flex-end">
            <span class="task-file-option" onclick="selectTaskFile('${itermId}','${safePath}')" style="padding:6px 12px;font-size:12px;color:var(--text-faint)">Cancel</span>
            <span class="task-file-option" onclick="createAndLinkTask('${itermId}','${safePath}')" style="padding:6px 12px;font-size:12px;color:var(--accent);font-weight:600">Create & Link</span>
        </div>
    </div>`;
    const input = document.getElementById('newTaskTitle');
    input.addEventListener('keydown', (e) => {
        e.stopPropagation();
        if (e.key === 'Enter') createAndLinkTask(itermId, taskFile);
        if (e.key === 'Escape') selectTaskFile(itermId, taskFile);
    });
    input.focus();
}

async function createAndLinkTask(itermId, taskFile) {
    const title = document.getElementById('newTaskTitle')?.value?.trim();
    if (!title) return;
    const res = await fetch('/api/create-task', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({taskFile, title})
    });
    const data = await res.json();
    if (data.ok) {
        // Link the conversation to this new task (no Notion ID yet)
        await fetch('/api/link-task', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({itermId, taskFile, notionTaskId: '', taskTitle: title})
        });
        document.getElementById('tagInputOverlay').style.display = 'none';
        isEditing = false;
        taskFilesCache = null;
        fetchSessions();
    } else {
        alert(data.error || 'Failed to create task');
    }
}

async function linkSpecificTask(itermId, taskFile, notionTaskId, taskTitle, taskId) {
    await fetch('/api/link-task', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, taskFile, notionTaskId, taskTitle, taskId})
    });
    document.getElementById('tagInputOverlay').style.display = 'none';
    isEditing = false;
    fetchSessions();
}

async function linkProjectOnly(itermId, taskFile) {
    await fetch('/api/link-task', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, taskFile})
    });
    document.getElementById('tagInputOverlay').style.display = 'none';
    isEditing = false;
    fetchSessions();
}

async function unlinkOneTask(itermId, taskId) {
    await storeAction('task/unlink', {itermId, taskId});
    fetchSessions(true);
}

async function unlinkTask(itermId) {
    await fetch('/api/link-task', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, taskFile: '__none__'})
    });
    fetchSessions();
}

function pickPriority(itermId) {
    const opts = ['Today','This Week','Next Week','Later'];
    const session = sessions.find(s => s.itermId === itermId);
    const current = session?.priorityLabel || '';
    const overlay = document.getElementById('tagInputOverlay');
    overlay.style.display = 'flex';
    const items = opts.map(o =>
        `<div class="priority-option${o===current?' current':''}" onclick="event.stopPropagation();setPriority('${itermId}','${o}')">${o}</div>`
    ).join('');
    const clearItem = current
        ? `<div class="priority-option clear" onclick="event.stopPropagation();setPriority('${itermId}','')">× Clear priority</div>`
        : '';
    overlay.innerHTML = `<div class="tag-input-box" onclick="event.stopPropagation()">
        <div style="margin-bottom:10px;font-size:13px;color:var(--text-dim)">Set priority</div>
        ${items}
        ${clearItem}
    </div>`;
}

async function setPriority(itermId, label) {
    await fetch('/api/priority', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, label})
    });
    document.getElementById('tagInputOverlay').style.display = 'none';
    fetchSessions();
}

async function toggleReview(itermId, flag) {
    await fetch('/api/set-review', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, flag})
    });
    fetchSessions();
}

function pickColor(tag) {
    const overlay = document.getElementById('tagInputOverlay');
    overlay.style.display = 'flex';
    const dots = PALETTE.map(c =>
        `<span class="palette-dot" style="background:var(--${c})"
               onclick="event.stopPropagation();assignColor('${tag.replace(/'/g,"\\'")}','${c}')"></span>`
    ).join('');
    overlay.innerHTML = `<div class="tag-input-box" onclick="event.stopPropagation()">
        <div style="margin-bottom:10px;font-size:13px;color:var(--text-dim)">Pick color for <strong>${tag}</strong></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">${dots}
            <span class="palette-dot unset" style="border:1px dashed var(--border)"
                  onclick="event.stopPropagation();assignColor('${tag.replace(/'/g,"\\'")}','default')">×</span>
        </div>
    </div>`;
}

async function assignColor(tag, color) {
    await fetch('/api/color', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tag, color})
    });
    document.getElementById('tagInputOverlay').style.display = 'none';
    fetchSessions();
}

function escHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function escAttr(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

// --- In-flight strip ---------------------------------------------------------

async function fetchTasks(force = false) {
    try {
        const res = await fetch('/api/tasks');
        const data = await res.json();
        const json = JSON.stringify(data);
        if (!force && json === lastTasksJson) return;
        lastTasksJson = json;
        renderTasksStrip(data);
    } catch (e) {
        console.error('tasks fetch failed', e);
    }
}

function renderTasksStrip(data) {
    const el = document.getElementById('tasksStrip');
    if (isDragging) return;
    const rows = (data && data.available) ? (data.tasks || []) : [];
    if (!rows.length) { el.innerHTML = ''; return; }
    const when = (t) => t.plannedDay ? shortDate(t.plannedDay) : (t.due ? 'due ' + shortDate(t.due) : '');
    el.innerHTML = `<span class="tasks-strip-label" title="Open tasks with a brief, a question for you, an agent at work, or a live conversation">In flight</span>` +
        rows.map(t => `<div class="task-chip" onclick="openTask(${t.id})" title="${escAttr(t.title)}">
            <span class="tag">${escHtml(t.tag)}</span>
            <span class="title">${escHtml(t.title)}</span>
            <span class="chip-meta">
                ${when(t) ? `<span>${escHtml(when(t))}</span>` : ''}
                ${t.openQuestions ? `<span class="decide">${t.openQuestions} to decide</span>` : ''}
                ${t.liveConversations ? `<span><span class="card-status working"></span> ${t.liveConversations} live</span>` : ''}
                ${t.openDispatches ? `<span><span class="card-status ready"></span> agent at work</span>` : ''}
                ${!t.hasBrief ? '<span>no brief</span>' : ''}
            </span>
        </div>`).join('');
}

// --- One task ---------------------------------------------------------------

async function fetchTask(force = false) {
    const id = openTaskId;
    if (!id) return;
    try {
        const res = await fetch('/api/task/' + id);
        if (id !== openTaskId) return;        // closed or switched meanwhile
        if (!res.ok) {
            document.getElementById('taskView').innerHTML = '<div class="task-empty">No such task.</div>';
            return;
        }
        const data = await res.json();
        const json = JSON.stringify(data);
        if (!force && json === lastTaskJson) return;
        lastTaskJson = json;
        taskData = data;
        renderTask();
    } catch (e) {
        console.error('task fetch failed', e);
    }
}

function briefText(text) {
    // Escaped first, then the two bits of markdown a brief header or list
    // actually uses. Anything richer stays in Obsidian.
    return escHtml(text)
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/`([^`]+)`/g, '<code>$1</code>');
}

function shortDate(iso) {
    if (!iso) return '';
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
    return m ? `${Number(m[2])}/${Number(m[3])}` : iso;
}

function shortStamp(ts) {
    if (!ts) return '';
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(ts);
    return m ? `${Number(m[2])}/${Number(m[3])} ${m[4]}:${m[5]}` : ts;
}

function renderTask() {
    const el = document.getElementById('taskView');
    if (!taskData) return;
    // The poll must never destroy an open answer box. Ask the DOM, as the
    // quick-add box does; flags have proved too easy to clear elsewhere.
    if (el.querySelector('.answer-input')) return;
    if (el.querySelector('#convPick.open')) return;
    const d = taskData;
    const t = d.task;
    const brief = d.brief || {};
    const hasBrief = !!(t.briefPath && brief.exists);

    // Header
    const meta = [];
    meta.push(`<span class="status">${escHtml((t.status || '').replace('_', ' '))}</span>`);
    if (t.plannedDay) meta.push('planned ' + shortDate(t.plannedDay));
    if (t.due) meta.push('due ' + shortDate(t.due));
    if (t.load) meta.push(escHtml(t.load));
    meta.push(escHtml(t.owner === 'mq' ? 'MQ' : (t.owner || '')));
    if (t.waitingOn) meta.push('waiting on ' + escHtml(t.waitingOn));
    meta.push(d.dispatchReady
        ? '<span class="ready-pill">dispatch-ready</span>'
        : (hasBrief ? '<span class="blocked-pill">blocked on a question</span>' : ''));
    const orcaLabel = hasBrief || t.briefPath ? 'Work on this with Orca' : 'Scope with Orca';
    const actions = [
        hasBrief ? `<a class="map-btn" href="${escAttr(brief.obsidianUrl)}">Open brief</a>` : '',
        t.noteUrl ? `<a class="map-btn" href="${escAttr(t.noteUrl)}">Open note</a>` : '',
        `<button class="map-btn primary" onclick="orcaOnTask(${t.id})">${orcaLabel}</button>`,
    ].join('');
    const isDone = t.status === 'done';
    const check = `<span class="task-check${isDone ? ' done' : ''}" onclick="toggleTaskDone(${t.id}, ${isDone})" title="${isDone ? 'Reopen' : 'Mark done'}">${isDone ? '✓' : '○'}</span>`;

    // Brief
    let briefHtml;
    if (!t.briefPath) {
        briefHtml = `<div class="brief-missing">No brief yet. "Scope with Orca" runs /scope-and-start on this task.</div>`;
    } else if (!brief.exists) {
        briefHtml = `<div class="brief-missing">The brief is recorded at <code>${escHtml(t.briefPath)}</code> but the file is missing.</div>`;
    } else {
        const skip = new Set(['Task', 'Task ID', 'Dispatch-ready', 'Status']);
        const header = Object.entries(brief.header || {})
            .filter(([k]) => !skip.has(k))
            .map(([k, v]) => `<span><b>${escHtml(k)}</b> ${escHtml(v)}</span>`).join('');
        briefHtml = `<div class="brief-header">${header}</div>` +
            (brief.goal ? `<div class="brief-section"><h4>Goal</h4>${briefText(brief.goal)}</div>` : '') +
            (brief.deliverables ? `<div class="brief-section"><h4>Deliverables</h4>${briefText(brief.deliverables)}</div>` : '') +
            (!brief.goal && !brief.deliverables ? '<div class="task-empty">The brief has no Goal or Deliverables section.</div>' : '');
    }

    // Needs you
    const qRow = (q, project) => `<div class="q-row${project ? ' project' : ''}" id="q-${q.id}">
        <span class="qid">Q${q.id}</span>
        <div class="qbody">
            <div>${escHtml(q.text)}</div>
            <div class="qmeta">
                ${q.proposed ? `Proposed: <b>${escHtml(q.proposed)}</b>` : '<span style="color:var(--orange)">no proposed answer</span>'}
                ${q.blocks ? ` · ${escHtml(q.blocks)} has been blocked since ${shortDate(q.askedAt)}` : ` · asked by ${escHtml(q.askedBy)} ${shortDate(q.askedAt)}`}
                ${project && q.task ? ` · <a href="#task/${q.taskId}" style="color:var(--accent)">${escHtml(q.task)}</a>` : ''}
            </div>
            <div class="answer-slot"></div>
        </div>
        <div class="qactions">
            ${q.proposed ? `<button class="panel-btn" onclick="acceptQuestion(${q.id})">Accept</button>` : ''}
            <button class="panel-btn" onclick="startAnswer(${q.id})">Answer</button>
            ${hasBrief ? `<a class="panel-btn" href="${escAttr(brief.obsidianUrl)}">Brief ↗</a>` : ''}
        </div>
    </div>`;
    const taskQs = (d.questions?.task || []);
    const projQs = (d.questions?.project || []);
    let needsHtml = taskQs.map(q => qRow(q, false)).join('')
        || '<div class="task-empty">Nothing waiting on you here.</div>';
    if (projQs.length) {
        needsHtml += `<div class="q-rule">Elsewhere in ${escHtml(d.project?.name || d.project?.key || 'this project')}</div>`
            + projQs.map(q => qRow(q, true)).join('');
    }

    // Conversations
    const convs = d.conversations || [];
    const convHtml = convs.map(c => {
        const who = c.actor ? c.actor : (c.kind === 'scheduled' ? 'sweep' : 'MQ');
        const state = c.live ? (c.state || 'ready') : 'inactive';
        return `<div class="conv-row${c.live ? '' : ' dead'}" onclick="openConversation('${escAttr(c.sessionId)}')" title="${c.live ? 'Focus the iTerm tab' : 'Resume in a new tab'}">
            <span class="card-status ${state}"></span>
            <span>${escHtml(c.name)}${c.isAutomation ? ' <span class="inactive-badge">scheduled</span>' : ''}${c.live ? '' : ' <span class="inactive-badge">inactive</span>'}</span>
            <span class="who">${escHtml(who)}</span>
            <span class="who">${escHtml(c.age || (c.lastSeen ? shortStamp(c.lastSeen) : ''))}</span>
            ${c.lastEvent ? `<span class="last">${shortStamp(c.lastEvent.ts)} · ${escHtml(c.lastEvent.summary || c.lastEvent.kind)}</span>` : ''}
        </div>`;
    }).join('') || '<div class="task-empty">No conversation is linked to this task. Writes from other sessions are in the timeline below; "Work on this with Orca" opens one that is.</div>';
    const convPick = `<div class="conv-pick" id="convPick"><button class="panel-btn" onclick="showConversationPicker(${t.id})">+ link a conversation</button></div>`;

    // Agents
    const nextHtml = (d.next?.dispatches || []).map(e => `<div class="next-row">
        <span class="who">${escHtml(e.agent || '?')}</span> since ${shortStamp(e.ts)}
        ${e.artifactPath ? `<div class="path">→ ${escHtml(e.artifactPath)}</div>` : ''}
    </div>`).join('') +
        ((d.next?.waitingOn || []).length
            ? `<div class="next-row">Waiting on your answer to ${d.next.waitingOn.map(id => `<a href="#q-${id}" style="color:var(--accent)">Q${id}</a>`).join(', ')}</div>`
            : '') || '<div class="task-empty">Nothing out.</div>';
    const doneHtml = (d.events || []).map(e => `<div class="event-row">
        <span class="when">${shortStamp(e.ts)}</span>
        <span><span class="actor">${escHtml(e.actor)}</span> · <span class="kind">${escHtml(e.kind)}</span>${e.verdict ? ' ' + escHtml(e.verdict) : ''}: ${escHtml(e.summary || '')}${e.artifactUrl ? ` <a href="${escAttr(e.artifactUrl)}">${escHtml(e.artifactPath.split('/').pop())} ↗</a>` : ''}</span>
    </div>`).join('') || '<div class="task-empty">No events yet.</div>';

    el.innerHTML = `
        <div class="task-head">
            ${check}
            <span class="tag">${escHtml(t.tag)}</span>
            <h2${isDone ? ' style="text-decoration:line-through;opacity:0.6"' : ''}>${escHtml(t.title)}</h2>
        </div>
        <div class="task-meta">${meta.filter(Boolean).join('<span>·</span>')}</div>
        <div class="task-actions">${actions}</div>
        <div class="task-block"><h3>Brief${hasBrief ? ` <span class="hint">${escHtml(t.briefPath.split('/').pop())}</span>` : ''}</h3>${briefHtml}</div>
        <div class="task-block"><h3>Needs you</h3>${needsHtml}</div>
        <div class="task-block"><h3>Conversations</h3>${convHtml}${convPick}</div>
        <div class="task-block"><h3>Agents</h3>
            <div class="agents-cols">
                <div><h3>Next</h3>${nextHtml}</div>
                <div><h3>Done${t.notePath ? ` <a class="hint" href="${escAttr(t.noteUrl)}" style="color:var(--accent)">history ↗</a>` : ''}</h3>${doneHtml}</div>
            </div>
        </div>`;
}

function startAnswer(qid) {
    const row = document.getElementById('q-' + qid);
    if (!row || row.querySelector('.answer-input')) return;
    const slot = row.querySelector('.answer-slot');
    slot.innerHTML = '<input class="answer-input" placeholder="Your answer, Enter to save, Esc to cancel">';
    const input = slot.querySelector('input');
    isEditing = true;
    setTimeout(() => { isEditing = true; input.focus(); }, 0);
    let closed = false;
    const finish = async (save) => {
        if (closed) return;
        closed = true;
        const answer = input.value.trim();
        isEditing = false;
        slot.innerHTML = '';
        if (save && answer) await answerQuestion(qid, answer);
    };
    input.onkeydown = (e) => {
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    };
    // Clicking away keeps what was typed rather than discarding it.
    input.onblur = () => finish(true);
}

async function answerQuestion(qid, answer) {
    const ok = await storeAction('question/answer', {id: qid, answer});
    if (!ok) alert('Could not record the answer.');
    fetchTask(true);
    fetchTasks();
}

async function acceptQuestion(qid) {
    const ok = await storeAction('question/accept', {id: qid});
    if (!ok) alert('Could not accept: the question may have no proposed answer.');
    fetchTask(true);
    fetchTasks();
}

async function toggleTaskDone(id, wasDone) {
    await storeAction('task/' + (wasDone ? 'reopen' : 'done'), {id});
    fetchTask(true);
    fetchSessions(true);
}

// Which board sessions look like they are about this task, by word overlap
// with its title: the same rule `mh task find` uses. MQ picks; nothing is
// linked on a guess.
function showConversationPicker(taskId) {
    const t = taskData?.task;
    if (!t) return;
    const linked = new Set((taskData.conversations || []).map(c => c.sessionId));
    const words = (s) => new Set((s || '').toLowerCase().match(/[a-z0-9']+/g)?.filter(w => w.length > 2) || []);
    const title = words(t.title + ' ' + (taskData.project?.name || ''));
    const scored = sessions
        .filter(s => !s.isAutomation && !linked.has(s.claudeSessionId || s.itermId))
        .map(s => {
            const w = words(s.name);
            let hits = 0; w.forEach(x => { if (title.has(x)) hits++; });
            return {s, hits};
        })
        .sort((a, b) => b.hits - a.hits || (b.s.isInactive ? 0 : 1) - (a.s.isInactive ? 0 : 1)
                        || a.s.name.localeCompare(b.s.name));
    const el = document.getElementById('convPick');
    if (!el) return;
    el.classList.add('open');          // renderTask leaves the picker alone while it is open
    el.innerHTML = `<div style="width:100%;font-size:11px;color:var(--text-dim);margin-bottom:2px">Pick the conversation that is about this task:</div>` +
        scored.slice(0, 12).map(({s, hits}) => `<button class="panel-btn" onclick="linkConversation(${taskId}, '${escAttr(s.itermId)}')"
            title="${escAttr(s.shortCwd || '')}">${s.isInactive ? '' : '<span class="card-status ' + escAttr(s.cardState || 'ready') + '" style="display:inline-block;margin-right:4px"></span>'}${escHtml(s.name.slice(0, 48))}${hits ? ' <span style="opacity:0.5">' + hits + '</span>' : ''}</button>`).join('') +
        `<button class="panel-btn" onclick="this.parentElement.classList.remove('open');renderTask()">cancel</button>`;
}

async function linkConversation(taskId, itermId) {
    const ok = await storeAction('task/link', {itermId, taskId});
    if (!ok) alert('Could not link that conversation.');
    fetchTask(true);
}

async function orcaOnTask(id) {
    const res = await fetch('/api/task/orca', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({taskId: id})
    });
    const data = await res.json().catch(() => ({}));
    if (!data.ok) alert('Could not open a session. Is iTerm running?');
    setTimeout(() => fetchTask(true), 4000);
}

async function openConversation(sessionId) {
    const c = (taskData?.conversations || []).find(x => x.sessionId === sessionId);
    if (!c) return;
    if (c.live && c.itermId) {
        const r = await fetch('/api/navigate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({itermId: c.itermId, claudeSessionId: c.sessionId,
                                  cwd: c.cwd || '', displayName: c.name || ''})
        });
        const data = await r.json().catch(() => ({}));
        if (data.resumed) setTimeout(() => fetchTask(true), 4000);
        return;
    }
    const r = await fetch('/api/resume', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({claudeSessionId: c.sessionId, cwd: c.cwd || '', displayName: c.name || ''})
    });
    const data = await r.json().catch(() => ({}));
    if (!data.ok) alert('Could not resume. Is iTerm2 running?');
    else setTimeout(() => fetchTask(true), 4000);
}

// Initial load + auto-refresh
fetchSessions().then(routeFromHash);
fetchTasks();
setInterval(fetchSessions, 5000);
setInterval(pollView, 5000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Claude Session Manager")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--root", type=Path, default=None,
                    help="content repo to read (overrides MELLONHEAD_ROOT)")
    args = ap.parse_args()

    PORT = args.port
    if args.root:
        MELLONHEAD_ROOT = args.root.expanduser()
        PRIORITIES_FILE = MELLONHEAD_ROOT / "priorities.md"
        STORE_FILE = MELLONHEAD_ROOT / "operations" / "tasks.db"

    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Claude Session Manager running at http://localhost:{PORT}")
    print(f"  repo:  {MELLONHEAD_ROOT}")
    print(f"  state: {STATE_DIR}")
    if PORT != 7433:
        print("  (dev instance — live manager is on 7433)")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
