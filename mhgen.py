#!/usr/bin/env python3
"""
mhgen — write the markdown views back out of the store.

    python3 mhgen.py --repo ~/Projects/mellonhead-dev
    python3 mhgen.py --repo ~/Projects/mellonhead-dev --check

Surgical, not wholesale. The plan describes these files as generated, but
they are not generated end to end and never can be:

  priorities.md  carries 84 lines of hand-written preamble above
                 "## Weekly Goals" — the capacity dashboard, the cognitive
                 budget, the reactivation risk table and the commitment
                 rules. None of it is in the store.
  task-list.md   carries Goal, Dependencies, Design Context, Open Questions,
                 Session bookmark and a dozen other hand-written sections
                 around its task table.

So each generator replaces exactly the region it owns and leaves the rest of
the file byte for byte. A marker states where the boundary is rather than
claiming the whole file is generated, which would not be true.

Workstream 2.1 (generators). Stdlib only.
"""

import argparse
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import mhstore
from mhstore import OPEN_STATUSES, ONE_OFF

TASK_TABLE_HEADER = (
    "| # | Task | Status | Notion Task ID | Notes | Owner | Due | Seq |")
TASK_TABLE_DIVIDER = (
    "|---|------|--------|----------------|-------|-------|-----|-----|")

# Phase 1's column shape, kept exactly. build-dashboard.py requires "Task" and
# "Notion Task ID" in the header and an integer in the first cell; the plan's
# proposed shape (id | Seq | Task | Status | Owner | Due | Note) would break
# it, and Phase 1 keeps running through the dual-live window.
IS_TASK_HEADER = re.compile(r"^\|.*\|\s*Task\s*\|.*Notion Task ID", re.I)
IS_DIVIDER = re.compile(r"^\|[\s\-:|]+\|$")
HEADING = re.compile(r"^(#{2,4})\s+(.*)$")
WEEKLY_GOALS = re.compile(r"^##\s+Weekly Goals\b", re.I)
WEEK_HEADER = re.compile(r"^\*\*Week of ")

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")

STATUS_TEXT = {
    "backlog": "Not started",
    "planned": "Not started",
    "in_progress": "In progress",
    "waiting": "Waiting",
    "review": "Ready for MQ review",
    "done": "Done",
    "canceled": "Killed",
}

OWNER_TEXT = {"mq": "MQ"}


def atomic_write(path, text):
    """Unique temp name: two generators must not clobber each other's."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def stamp(what):
    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f"<!-- GENERATED {what} from operations/tasks.db at {when}. "
            f"Edit tasks in the session manager, not here. -->")


def owner_text(owner):
    return OWNER_TEXT.get(owner, (owner or "mq").title())


def status_text(task):
    """Prefer the original cell so a hand-written nuance is not flattened."""
    raw = (task["status_raw"] or "").strip()
    if raw and mhstore.normalize_status(raw)[0] == task["status"]:
        return raw
    return STATUS_TEXT.get(task["status"], task["status"])


def cell(value):
    """Markdown table cells cannot hold a raw pipe or a newline."""
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Project task lists
# ---------------------------------------------------------------------------

def render_task_row(task, repo_rel_note=True):
    note = ""
    if task["notes"]:
        note = task["notes"]
    if task["note_path"]:
        link = f"[note]({Path(task['note_path']).name})" if repo_rel_note else ""
        note = f"{note} {link}".strip() if note else link
    return "| " + " | ".join(cell(x) for x in (
        task["display_ord"] or task["id"],
        task["title"],
        status_text(task),
        f"`{task['notion_task_id']}`" if task["notion_task_id"] else "(no Notion task)",
        note,
        owner_text(task["owner"]),
        task["due"] or "",
        task["seq"] if task["seq"] is not None else "",
    )) + " |"


def _table_regions(lines):
    """(start, end, section) for every task table in the file."""
    regions, section = [], None
    i = 0
    while i < len(lines):
        heading = HEADING.match(lines[i])
        if heading:
            section = heading.group(2).strip()
        if IS_TASK_HEADER.match(lines[i]):
            start = i
            j = i + 1
            if j < len(lines) and IS_DIVIDER.match(lines[j]):
                j += 1
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            regions.append((start, j, section))
            i = j
            continue
        i += 1
    return regions


def generate_task_list(store, repo, project_key, dry_run=False):
    """
    Rewrite only the task tables in a project's list.

    Files with several tables keep them: rows are matched to a table by the
    sub-heading they sat under, so a Phase 1/2/3 split does not collapse into
    one list.
    """
    project = store.project(project_key)
    if not project or not project["dir"]:
        return None
    path = repo / project["dir"] / "task-list.md"
    if not path.exists():
        return None

    lines = path.read_text().splitlines()
    regions = _table_regions(lines)
    if not regions:
        return None

    tasks = store.tasks(project_key=project_key)
    by_section = {}
    for task in tasks:
        by_section.setdefault(task["section"], []).append(task)

    def sort_key(t):
        terminal = t["status"] in ("done", "canceled")
        return (terminal, t["seq"] is None, t["seq"] or 0, t["id"])

    known = {section for _, _, section in regions}
    out, consumed = [], set()
    prev_end = 0
    for start, end, section in regions:
        out.extend(lines[prev_end:start])
        rows = by_section.get(section, [])
        if section == regions[0][2]:
            # Anything whose section no longer matches a table lands in the
            # first one rather than disappearing.
            for other, group in by_section.items():
                if other not in known:
                    rows = rows + group
                    consumed.add(other)
        out.append(TASK_TABLE_HEADER)
        out.append(TASK_TABLE_DIVIDER)
        out.extend(render_task_row(t) for t in sorted(rows, key=sort_key))
        prev_end = end
    out.extend(lines[prev_end:])

    if not out or not out[0].startswith("<!-- GENERATED"):
        out.insert(0, stamp("task table"))
    else:
        out[0] = stamp("task table")

    text = "\n".join(out).rstrip("\n") + "\n"
    if dry_run:
        return text
    atomic_write(path, text)
    return text


# ---------------------------------------------------------------------------
# priorities.md
# ---------------------------------------------------------------------------

def render_day_line(task):
    bits = [f"- [{'x' if task['status'] == 'done' else ' '}]"]
    bits.append(f"**{task['title']}**")
    if task["load"]:
        bits.append(f"[{task['load'].title()}].")
    if task["notes"]:
        bits.append(task["notes"])
    if task["note_path"]:
        bits.append(f"Notes: `{task['note_path']}`.")
    if task["project_key"] != ONE_OFF and task["display_ord"]:
        bits.append(f"`{task['project_key']}#{task['display_ord']}`")
    if task["status"] == "done" and task["done_at"]:
        bits.append(f"(done {task['done_at']})")
    return " ".join(bits)


def render_week(store, week_start):
    week = store.week(week_start) or {}
    start = date.fromisoformat(week_start)
    state = ""
    if week.get("locked_at"):
        state = f" (confirmed, locked {week['locked_at'][:10]})"
    elif week.get("proposed_at"):
        state = f" (proposed {week['proposed_at'][:10]}, not confirmed)"

    lines = [f"**Week of {start.strftime('%B')} {start.day}, {start.year}**{state}",
             "", stamp("from here down"), "", "## Weekly Goals", ""]

    rows = store.conn.execute(
        """SELECT * FROM tasks
           WHERE planned_day >= ? AND planned_day <= date(?, '+6 day')
           ORDER BY planned_day, seq IS NULL, seq, id""",
        (week_start, week_start)).fetchall()
    by_day = {}
    for row in rows:
        by_day.setdefault(row["planned_day"], []).append(dict(row))

    for offset in range(7):
        day = start + timedelta(days=offset)
        items = by_day.get(day.isoformat())
        if not items:
            continue
        lines.append(f"### {DAY_NAMES[day.weekday()]} {day.month}/{day.day}")
        lines.append("")
        lines.extend(render_day_line(t) for t in items)
        lines.append("")

    blocked = [dict(r) for r in store.conn.execute(
        "SELECT * FROM tasks WHERE status = 'waiting' AND planned_day IS NULL "
        "ORDER BY project_key, id")]
    lines.append("### Blocked or waiting")
    lines.append("")
    lines.extend(render_day_line(t) for t in blocked)
    lines.append("")

    proposed = [dict(r) for r in store.conn.execute(
        "SELECT * FROM tasks WHERE confirmed = 0 AND status = 'backlog' "
        "ORDER BY project_key, id")]
    if proposed:
        lines.append("## Proposed (unconfirmed)")
        lines.append("")
        lines.append("*Captured, waiting on MQ. Keep or drop in the session "
                     "manager; nothing here is committed.*")
        lines.append("")
        lines.extend(render_day_line(t) for t in proposed)
        lines.append("")

    backlog = [dict(r) for r in store.conn.execute(
        "SELECT * FROM tasks WHERE status = 'backlog' AND confirmed = 1 "
        "AND planned_day IS NULL ORDER BY project_key, seq IS NULL, seq, id")]
    lines.append("## Backlog")
    lines.append("")
    lines.append("*One list. Items wait here until a planning pass or MQ moves "
                 "them onto a day card.*")
    lines.append("")
    lines.extend(render_day_line(t) for t in backlog)
    lines.append("")
    return "\n".join(lines)


def weeks_to_render(store, today=None):
    """
    The current week, plus any later week that has been proposed.

    Rendering only the latest week meant that proposing next week on a
    Friday deleted the running week from the file: the store still had it and
    the dashboard still showed it, but the markdown MQ works from lost
    Thursday and Friday mid-week.
    """
    today = (today or date.today()).isoformat()
    starts = [r["week_start"] for r in store.conn.execute(
        "SELECT week_start FROM weeks ORDER BY week_start")]
    if not starts:
        return []
    current = [w for w in starts if w <= today]
    chosen = [current[-1]] if current else [starts[0]]
    chosen += [w for w in starts if w > chosen[0]]
    return chosen


def generate_priorities(store, repo, week_start=None, dry_run=False):
    """
    Replace priorities.md from its week header down.

    Everything above is hand-written and is copied through untouched: the
    capacity dashboard, the cognitive budget, the reactivation risk table,
    the commitment rules, and any review section for the outgoing week.
    """
    path = repo / "priorities.md"
    if week_start is None:
        chosen = weeks_to_render(store)
        if not chosen:
            return None
    else:
        chosen = [week_start]

    preamble = []
    if path.exists():
        lines = path.read_text().splitlines()
        try:
            goals_at = next(i for i, l in enumerate(lines) if WEEKLY_GOALS.match(l))
        except StopIteration:
            goals_at = len(lines)
        cut = goals_at
        for i in range(goals_at - 1, -1, -1):
            if WEEK_HEADER.match(lines[i]):
                cut = i
                break
        preamble = lines[:cut]
        while preamble and not preamble[-1].strip():
            preamble.pop()

    rendered = []
    for i, wk in enumerate(chosen):
        block = render_week(store, wk)
        if i:
            # Only the first block carries the generated marker; a second one
            # mid-file reads like the boundary moved.
            block = "\n".join(l for l in block.splitlines()
                               if not l.startswith("<!-- GENERATED"))
            rendered.append("\n---\n")
        rendered.append(block)
    text = "\n".join(preamble + [""] + rendered)
    text = text.lstrip("\n").rstrip("\n") + "\n"
    if dry_run:
        return text
    atomic_write(path, text)
    return text


# ---------------------------------------------------------------------------
# Projects dashboard
# ---------------------------------------------------------------------------

def generate_dashboard(store, repo, dry_run=False):
    import json

    live, archived, rows = [], [], []
    for project in store.projects():
        if project["key"] == ONE_OFF:
            continue
        open_tasks = store.tasks(project_key=project["key"], open_only=True)
        nxt = store.next_task(project["key"])
        entry = {
            "key": project["key"],
            "name": project["name"],
            "status": project["status"],
            "owner": project["owner"],
            "due": project["due"],
            "dir": project["dir"],
            "openCount": len(open_tasks),
            "next": ({"id": nxt["id"], "title": nxt["title"], "seq": nxt["seq"],
                      "due": nxt["due"]} if nxt else None),
        }
        rows.append(entry)
        (archived if project["status"] in mhstore.ARCHIVED_PROJECT_STATUSES
         else live).append(entry)

    md = [stamp("projects dashboard"), "", "# Projects dashboard", "",
          f"*{sum(r['openCount'] for r in live)} open tasks across "
          f"{len(live)} live projects. {len(archived)} archived.*", ""]
    for label, group in (("Active", [r for r in live if r["status"] == "active"]),
                         ("Waiting", [r for r in live if r["status"] == "waiting"]),
                         ("Parked", [r for r in live if r["status"] == "parked"]),
                         ("Archived", archived)):
        if not group:
            continue
        md += [f"## {label}", "",
               "| Project | Next task | Due | Open |",
               "|---|---|---|---|"]
        for r in sorted(group, key=lambda r: r["key"]):
            nxt = r["next"]["title"] if r["next"] else "—"
            md.append(f"| `{r['key']}` | {cell(nxt)} | {r['due'] or ''} "
                      f"| {r['openCount']} |")
        md.append("")

    md_text = "\n".join(md).rstrip("\n") + "\n"
    json_text = json.dumps(
        {"generated_at": datetime.now().isoformat(timespec="seconds"),
         "projects": rows}, indent=2) + "\n"
    if dry_run:
        return md_text
    for target, body in ((repo / "operations" / "projects-dashboard.md", md_text),
                         (repo / "operations" / "projects-dashboard.json", json_text)):
        atomic_write(target, body)
    return md_text


# ---------------------------------------------------------------------------

def generate_all(store, repo, dry_run=False):
    repo = Path(repo)
    written = []
    for project in store.projects():
        if project["key"] == ONE_OFF:
            continue
        if generate_task_list(store, repo, project["key"], dry_run) is not None:
            written.append(f"{project['dir']}/task-list.md")
    if generate_priorities(store, repo, dry_run=dry_run) is not None:
        written.append("priorities.md")
    generate_dashboard(store, repo, dry_run)
    written += ["operations/projects-dashboard.md",
                "operations/projects-dashboard.json"]
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate markdown from the store")
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="one project key")
    args = ap.parse_args(argv)

    repo = args.repo.expanduser()
    store = mhstore.open_store(root=repo, seed_settings=False)
    try:
        if args.only:
            text = generate_task_list(store, repo, args.only, args.dry_run)
            print(text if args.dry_run else f"wrote {args.only}")
            return 0
        written = generate_all(store, repo, args.dry_run)
        head = "would write" if args.dry_run else "wrote"
        print(f"{head} {len(written)} files")
        for w in written:
            print(f"  {w}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
