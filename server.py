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
import time
import urllib.parse
from pathlib import Path

PORT = 7433
SESSIONS_FILE = Path.home() / ".claude-manager" / "sessions.json"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
PRIORITIES_FILE = Path.home() / "Projects" / "mellonhead" / "priorities.md"
TODOS_DIR = Path.home() / ".claude-manager" / "todos"
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


def parse_priorities():
    """Parse priorities.md to extract daily goals for current + next week."""
    if not PRIORITIES_FILE.exists():
        return {"days": [], "blocked": [], "weeks": []}

    with open(PRIORITIES_FILE) as f:
        content = f.read()

    week_matches = list(re.finditer(r"\*\*Week of (.+?)\*\*", content))
    # Reject passing-reference matches whose label ends in ':' (e.g. inside
    # the Cognitive Budget block: `**Week of Aug 3, 2026:**`). Real section
    # headers look like `**Week of Aug 3, 2026**` or `... 2026** -- suffix`.
    week_matches = [m for m in week_matches if not m.group(1).rstrip().endswith(":")]
    if not week_matches:
        return {"days": [], "blocked": [], "weeks": []}

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
    }


def get_today_day_name():
    """Return today's day name (e.g., 'Monday')."""
    import datetime
    return datetime.datetime.now().strftime("%A")


def match_priority_to_session(item, session, task_assignments):
    """Match a priority item to a session. Prefer Notion ID; fall back to fuzzy."""
    notion_id = item.get("notionTaskId", "")

    if notion_id:
        # Exact ID match against this session's task assignment
        iterm_id = session.get("itermId", "")
        assignment = task_assignments.get(iterm_id, {})
        assigned_id = assignment.get("notionTaskId", "").replace("-", "")
        return assigned_id == notion_id.replace("-", "")

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
    try:
        with open(jsonl_path, "rb") as f:
            chunk = f.read(131072)  # 128KB — cwd is in line 1-3, ai-title within first few turns
        m = _CWD_RE.search(chunk)
        if m:
            cwd = m.group(1).decode("utf-8", errors="ignore")
        # ai-title may appear multiple times; take the last one in our window (most recent)
        for m in _AI_TITLE_RE.finditer(chunk):
            title = _decode_json_str(m.group(1))
        # First user message (fallback when ai-title hasn't been generated yet)
        if not title:
            m = _USER_MSG_RE.search(chunk)
            if m:
                first_msg = _decode_json_str(m.group(1))[:80]
    except OSError:
        pass
    meta = {"mtime": mtime, "cwd": cwd, "title": title, "firstMessage": first_msg}
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
                "lastActivity": st.st_mtime,
                "jsonlPath": str(jsonl),
            })
    results.sort(key=lambda r: -r["lastActivity"])
    return results[:INACTIVE_MAX]


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
        "taskAssignment": store.get("taskAssignments", {}).get(sid),
        "todo": todo,
    }


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
                task_list = parse_task_list(Path(linked_task_file))
            except Exception as e:
                print(f"Task parse error for {linked_task_file}: {e}")
        else:
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

        task_assignment = lookup_state(store.get("taskAssignments", {}), session_id, iterm["itermId"])
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
            "taskAssignment": task_assignment,
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

    if store_dirty:
        save_store(store)

    # Update the index so Claude sessions can find their todo files
    update_todo_index(sessions)

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
                "colorGroups": store.get("color_groups", {}),
            })
        elif self.path == "/api/task-files":
            files = []
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
        elif self.path == "/":
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
                        if notion_task_id:
                            assignments[iterm_id] = {
                                "taskFile": expanded,
                                "notionTaskId": notion_task_id,
                                "taskTitle": task_title,
                            }
                        else:
                            assignments.pop(iterm_id, None)
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

/* Filter bar */
.filter-bar {
    padding: 12px 24px;
    display: flex;
    align-items: center;
    gap: 8px;
}
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

<div id="prioritiesBar" class="priorities-bar"></div>

<div class="filter-bar">
    <span id="activeFilter"></span>
    <div id="tagCloudInline" class="tag-cloud-inline"></div>
    <input type="text" class="search-input" id="searchInput"
           placeholder="Search..." oninput="renderAll()">
</div>

<div id="cardGridView" class="card-grid"></div>
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
let activeFilterTag = null;
let tagInputTarget = null;
let isEditing = false;
const expandedSections = new Set();

const PALETTE = ['purple','green','blue','red','orange','pink','teal','yellow'];

async function fetchSessions(force = false) {
    if (isEditing && !force) return;
    try {
        const res = await fetch('/api/sessions');
        const data = await res.json();
        sessions = data.sessions;
        colorGroups = data.colorGroups || {};
        priorities = data.priorities || {};
        renderAll();
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

async function togglePriorityItem(text, done) {
    await fetch('/api/toggle-priority-item', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text, done})
    });
    fetchSessions(true);
}

function togglePriorityItemFromEl(el) {
    const text = el.dataset.text || '';
    const wasDone = el.dataset.done === '1';
    // Optimistic UI flip so the user sees immediate feedback
    el.classList.toggle('done', !wasDone);
    el.dataset.done = wasDone ? '0' : '1';
    const check = el.querySelector('.priority-check');
    if (check) check.textContent = wasDone ? '○' : '✓';
    togglePriorityItem(text, !wasDone);
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

function renderAll() {
    renderPriorities();
    renderTagCloudInline();
    renderFilterBar();
    renderCards();
}

function renderPriorities() {
    const container = document.getElementById('prioritiesBar');
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
    const renderWeek = (w, showMapBtn, idx) => {
        let suffix = '';
        if (noCurrent && idx === 0) suffix = ' <span style="opacity:0.6">· upcoming (no plan for this week)</span>';
        else if (!w.isCurrent) suffix = ' <span style="opacity:0.6">· next week</span>';
        return `<div class="priorities-week">
        <div class="priorities-header">
            <div class="priorities-title">${escHtml(w.title || (w.isCurrent ? 'This Week' : 'Next Week'))}${suffix}</div>
            ${showMapBtn ? '<button class="map-btn" onclick="mapPriorities()">Map to sessions</button>' : ''}
        </div>
        <div class="priorities-days">
            ${w.days.map(d => `<div class="priority-day">
                <div class="priority-day-name">${escHtml(d.day)}</div>
                ${d.items.map(item => `<div class="priority-item ${item.done ? 'done' : ''}" data-text="${escAttr(item.text)}" data-done="${item.done ? '1' : '0'}" onclick="togglePriorityItemFromEl(this)">
                    <span class="priority-check">${item.done ? '✓' : '○'}</span>
                    <span class="priority-text">${escHtml(item.text)}</span>
                </div>`).join('')}
            </div>`).join('')}
        </div>
    </div>`;
    };

    container.innerHTML = visibleWeeks.map((w, i) => renderWeek(w, i === 0, i)).join('');
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

function renderFilterBar() {
    const el = document.getElementById('activeFilter');
    if (activeFilterTag) {
        el.innerHTML = `<span class="filter-tag">${activeFilterTag}
            <span class="clear" onclick="clearFilter()">×</span></span>`;
    } else {
        el.innerHTML = '';
    }
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
            if (s.taskAssignment?.notionTaskId && s.taskList?.tasks) {
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

        // Link task — show assignment (project + task) or just project, or link button
        let linkHtml;
        if (s.taskAssignment) {
            const ta = s.taskAssignment;
            const taskLink = ta.notionTaskId
                ? `<a class="notion-link" href="https://notion.so/${ta.notionTaskId.replace(/-/g,'')}" target="_blank" onclick="event.stopPropagation()">↗</a>`
                : '';
            linkHtml = `<div class="task-link-info">
                 <span class="task-link-change" onclick="event.stopPropagation();openTaskLink('${s.itermId}')" title="Change">&#x21D7;</span>
                 <span class="task-link-name">${escHtml(s.taskList?.projectName || '')} → ${escHtml(ta.taskTitle)} ${taskLink}</span>
                 <span class="task-link-remove" onclick="event.stopPropagation();unlinkTask('${s.itermId}')" title="Remove">×</span>
               </div>`;
        } else if (s.taskList) {
            linkHtml = `<div class="task-link-info">
                 <span class="task-link-change" onclick="event.stopPropagation();openTaskLink('${s.itermId}')" title="Change">&#x21D7;</span>
                 <span class="task-link-name">${escHtml(s.taskList.projectName)}${s.isAutoLinked ? ' <span style="font-size:10px;color:var(--text-faint)">(auto)</span>' : ''}</span>
                 <span class="task-link-remove" onclick="event.stopPropagation();unlinkTask('${s.itermId}')" title="Remove">×</span>
               </div>`;
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
        return `<div class="task-file-option" onclick="linkSpecificTask('${itermId}','${safePath}','${safeId}','${safeTitle}')">
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

async function linkSpecificTask(itermId, taskFile, notionTaskId, taskTitle) {
    await fetch('/api/link-task', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({itermId, taskFile, notionTaskId, taskTitle})
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

// Initial load + auto-refresh
fetchSessions();
setInterval(fetchSessions, 5000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Claude Session Manager running at http://localhost:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
