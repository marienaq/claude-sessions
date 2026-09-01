#!/usr/bin/env python3
"""
mhmigrate — read the Phase 1 markdown into the store.

    python3 mhmigrate.py --from-repo ~/Projects/mellonhead-dev --dry-run
    python3 mhmigrate.py --from-repo ~/Projects/mellonhead-dev

Idempotent: it drops and rebuilds the migrated rows each run, so it can be
re-run after fixing markdown without producing duplicates.

Design rule: never silently lose prose, and never silently guess. Anything
this cannot classify goes in the review report rather than into a column.
Workstream 2.2. Stdlib only.
"""

import argparse
import csv
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

import mhstore
from mhstore import (
    clean, clean_title, has_next_marker, is_struck_through, normalize_owner,
    normalize_project_status, normalize_status, slugify, status_is_ambiguous,
)

# A Notes cell longer than this becomes a note file. The plan says "more than
# one sentence"; character count is what can actually be measured, and the
# survey found 66 rows past this line, some running to 6.8KB.
NOTE_THRESHOLD = 200

SKIP_DIR_PARTS = ("operations/backups", "/archive/", "mellonhead-archive")

TASK_HEADER = re.compile(r"^\|.*\|\s*Task\s*\|.*Notion Task ID", re.I)
# A row that loses its leading pipe stops being a table row. It is then not
# malformed, it is invisible: no parser sees it and no report mentions it.
# This catches "24 | Task | ..." so a hand-edit cannot delete work silently.
ORPHAN_ROW = re.compile(r"^\s+\S[^|\n]{0,40}\|(?:[^|\n]*\|){3,}")
DIVIDER = re.compile(r"^\|[\s\-:|]+\|$")
HEADING = re.compile(r"^(#{2,4})\s+(.*)$")
YAML_FENCE = "```yaml"
KEY_DECISIONS = re.compile(r"^##\s+Key Decisions", re.I)
# ken-yarmosh keeps each task's real content in "## N. <title>" sections.
NARRATIVE = re.compile(r"^##\s+(\d+)\.\s+(.*)$")

# priorities.md: the week, its day cards, and the checkbox lines under them.
WEEK_HEADER = re.compile(r"^\*\*Week of ([^*]+?)\*\*")
WEEKLY_GOALS = re.compile(r"^##\s+Weekly Goals\b", re.I)
BLOCKED_HEADER = re.compile(r"^#{2,3}\s+Blocked or waiting\b", re.I)
BACKLOG_HEADER = re.compile(r"^##\s+Backlog\b", re.I)
DAY_CARD = re.compile(
    r"^###\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(\d{1,2})/(\d{1,2})", re.I)
CHECKBOX = re.compile(r"^\s*-\s+\[([ xX])\]\s+(.*)$")
# `aba-academy#25` — the tag Phase 0 put on day-card lines so a checkbox has
# an exact target instead of a text match.
KEY_TAG = re.compile(r"`([a-z0-9][a-z0-9-]*)#(\d+)`")
LOAD_TAG = re.compile(r"\[(deep|medium|shallow)\b", re.I)
DONE_SUFFIX = re.compile(r"\s*\((?:done|closed)\s+[0-9-/]+\)\s*$", re.I)

MONTH_DAY_YEAR = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})")
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


CELL_SPLIT = re.compile(r"(?<!\\)\|")

# The generator renders a task's note file as "[note](79-slug.md)" in the
# Notes cell and as "Notes: `path`." on a day-card line. Reading either back
# as prose and then re-rendering it appends a second copy, so each cycle grows
# the cell: [note](x) [note](x) [note](x). Counts stay stable while the text
# quietly doubles, which is why a row-count convergence check did not catch it.
GENERATED_NOTE_LINK = re.compile(r"\s*\[note\]\([^)]*\)")
GENERATED_NOTE_PATH = re.compile(r"\s*Notes:\s*`[^`]*`\.?")


def strip_generated_markers(text):
    """Remove what the generator adds, so a round trip does not accumulate."""
    text = GENERATED_NOTE_LINK.sub("", text or "")
    text = GENERATED_NOTE_PATH.sub("", text)
    return text.strip()


def split_cells(line):
    """
    Split a table row on unescaped pipes.

    A cell may legitimately contain "\\|" (a task titled "Compare A | B"),
    and the generator writes it that way. Splitting on every pipe turns one
    such row into a cell-count mismatch, which this code then reports as
    malformed and drops. Round-tripping a pipe has to be lossless.
    """
    parts = CELL_SPLIT.split(line)
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip().replace("\\|", "|") for p in parts]


class Report:
    """Everything a human needs to check after a run."""

    def __init__(self):
        self.projects = 0
        self.tasks = 0
        self.notes_written = 0
        self.note_chars = 0
        self.decisions_written = 0
        self.ambiguous = []      # (key, title, raw_status, decided)
        self.malformed = []      # (path, line_no, cells, expected)
        self.skipped_files = []  # (path, reason)
        self.duplicates = []     # (path, same_as)
        self.unregistered = []   # (path,)
        self.non_iso_dates = []  # (key, title, value)
        self.orphan_rows = []    # (path, line_no, text) — lost its leading pipe
        self.no_seq = []         # (key, open_count)
        self.week = None
        self.week_days = 0
        self.priority_matched = 0
        self.priority_one_offs = 0
        self.priority_unmatched = []  # (title, when, why)
        self.priority_multi_day = []  # (tag, kept_day, also_day, title)
        self.week_state = None
        self.deferred_sections = []

    def add_ambiguous(self, key, title, raw, decided):
        self.ambiguous.append((key, title, raw, decided))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_header_block(lines):
    """The ```yaml block under the H1. Matches build-dashboard.py's reading."""
    try:
        start = next(i for i, l in enumerate(lines[:25]) if l.strip() == YAML_FENCE)
        end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "```")
    except StopIteration:
        return None
    block, milestones, in_milestones = {}, [], False
    for line in lines[start + 1:end]:
        if line.strip() == "milestones:":
            in_milestones = True
            continue
        if in_milestones and line.lstrip().startswith("- "):
            item = line.lstrip()[2:].strip()
            when, _, what = item.partition(":")
            milestones.append({"date": when.strip(), "label": what.strip() or when.strip()})
            continue
        if ":" in line and not line.startswith(" "):
            in_milestones = False
            k, _, v = line.partition(":")
            block[k.strip()] = v.strip()
    block["milestones"] = milestones
    return block


def parse_goal(lines):
    out, collecting = [], False
    for line in lines:
        if re.match(r"^##\s+Goal\b", line, re.I):
            collecting = True
            continue
        if collecting:
            if line.startswith("#"):
                break
            out.append(line)
    return "\n".join(out).strip() or None


def parse_section(lines, matcher):
    """Body of the first heading matching `matcher`, up to the next H2."""
    out, collecting = [], False
    for line in lines:
        if matcher.match(line):
            collecting = True
            out.append(line)
            continue
        if collecting:
            if re.match(r"^##\s+", line) and not matcher.match(line):
                break
            out.append(line)
    return "\n".join(out).strip() or None


def parse_narrative_sections(lines):
    """ken-yarmosh's '## N. <title>' blocks -> {N: (title, body)}."""
    sections, current, buf = {}, None, []
    for line in lines:
        m = NARRATIVE.match(line)
        if m:
            if current:
                sections[current[0]] = (current[1], "\n".join(buf).strip())
            current, buf = (m.group(1), m.group(2).strip()), []
            continue
        if current is not None:
            if re.match(r"^##\s+", line) and not NARRATIVE.match(line):
                sections[current[0]] = (current[1], "\n".join(buf).strip())
                current, buf = None, []
                continue
            buf.append(line)
    if current:
        sections[current[0]] = (current[1], "\n".join(buf).strip())
    return sections


def parse_task_rows(lines, report, path):
    """
    Walk only the qualifying task tables. A list may hold other tables (the
    academy ticket table, MCS's workshop-status table); their rows are not
    tasks. Tracks the nearest preceding heading as the row's section.
    """
    rows = []
    in_table, headers, section = False, [], None
    for i, line in enumerate(lines, 1):
        heading = HEADING.match(line)
        if heading:
            section = heading.group(2).strip()
            in_table = False
            continue
        if not line.startswith("|"):
            if ORPHAN_ROW.match(line):
                report.orphan_rows.append((path, i, line.strip()[:60]))
            in_table = False
            continue
        if TASK_HEADER.match(line):
            in_table = True
            headers = split_cells(line)
            continue
        if not in_table or DIVIDER.match(line):
            continue
        cells = split_cells(line)
        if len(cells) != len(headers):
            report.malformed.append((path, i, len(cells), len(headers)))
            continue
        row = dict(zip([h.strip().lower() for h in headers], cells))
        row["_line"] = i
        row["_section"] = section
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Note and decision files
# ---------------------------------------------------------------------------

def write_note_file(repo, project_dir, task_id, title, body, dry_run):
    """
    Per-task note file. Hand-written from here on; the store only holds the
    path. Content is copied verbatim under ## History, per the plan's rule
    that migration never truncates prose.
    """
    rel = Path(project_dir) / "task-notes" / f"{task_id}-{slugify(title)}.md"
    if dry_run:
        return str(rel)
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"# {title}\n\n"
        f"**Task:** `{task_id}`\n\n"
        f"<!-- Created by migration from the task-list Notes cell. "
        f"Hand-written from here on; agents append dated entries below. -->\n\n"
        f"## History\n\n{body}\n")
    return str(rel)


def write_decisions_file(repo, project_dir, body, dry_run):
    rel = Path(project_dir) / "decisions.md"
    if dry_run:
        return str(rel)
    target = repo / rel
    if target.exists():
        return str(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "<!-- Moved out of task-list.md by migration. Project-level context, "
        "hand-written. -->\n\n" + body + "\n")
    return str(rel)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def read_registry(repo):
    """key -> {name, path}. The registry is the list of projects that count."""
    path = repo / "operations" / "project-registry.md"
    if not path.exists():
        raise SystemExit(f"no registry at {path}")
    out = {}
    for line in path.read_text().splitlines():
        if not line.startswith("|"):
            continue
        cells = split_cells(line)
        if not cells or not cells[0].startswith("`"):
            continue
        key = cells[0].strip("`")
        list_path = next(
            (c.strip("`") for c in cells if c.strip("`").endswith("task-list.md")), None)
        if not list_path:
            continue
        out[key] = {"key": key, "path": list_path,
                    "name": cells[1] if len(cells) > 2 else key}
    return out


def migrate(repo, store, dry_run=False, note_threshold=NOTE_THRESHOLD):
    repo = Path(repo)
    report = Report()
    registry = read_registry(repo)

    # Match existing rows instead of deleting and re-inserting them. Ids have
    # to be stable: note files are named <id>-<slug>.md, so recreating rows
    # renames every note file's target and orphans it. Matching also stops a
    # task MQ added in the UI from being duplicated once the generator has
    # written it into the markdown that migration then reads back.
    existing = {}
    for row in store.tasks():
        ident = (row["project_key"],
                 str(row["display_ord"]) if row["display_ord"] else row["title"])
        existing[ident] = row["id"]
    seen_ids = set()

    seen_hashes = {}
    id_map = []

    for key, meta in sorted(registry.items()):
        list_path = repo / meta["path"]
        if not list_path.exists():
            report.skipped_files.append((meta["path"], "file not found"))
            continue
        if any(part in str(list_path) for part in SKIP_DIR_PARTS):
            continue

        text = list_path.read_text()
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest in seen_hashes:
            report.duplicates.append((meta["path"], seen_hashes[digest]))
            continue
        seen_hashes[digest] = meta["path"]

        lines = text.splitlines()
        header = parse_header_block(lines) or {}
        project_dir = str(Path(meta["path"]).parent)

        if not dry_run:
            fields = dict(
                dir=project_dir,
                status=normalize_project_status(header.get("status")),
                owner=normalize_owner(header.get("owner")),
                due=clean(header.get("due")),
                milestones=header.get("milestones", []),
                notion_project_id=clean(header.get("notion_project_id")),
                goal=parse_goal(lines),
            )
            name = header.get("name") or meta["name"]
            if store.project(key):
                store.update_project(key, actor="migration", name=name, **fields)
            else:
                store.add_project(key, name, actor="migration", **fields)
        report.projects += 1

        decisions = parse_section(lines, KEY_DECISIONS)
        if decisions:
            write_decisions_file(repo, project_dir, decisions, dry_run)
            report.decisions_written += 1

        narratives = parse_narrative_sections(lines)
        rows = parse_task_rows(lines, report, meta["path"])
        open_with_seq = 0

        for row in rows:
            raw_title = row.get("task", "")
            title = clean_title(raw_title)
            if not title:
                continue
            raw_status = row.get("status", "")
            status, done_at, kept = normalize_status(raw_status, default_year=2026)
            if is_struck_through(raw_title):
                status, kept = "done", kept or "~~struck through~~"

            seq_raw = clean(row.get("seq"))
            seq = int(seq_raw) if seq_raw and seq_raw.isdigit() else None
            due = clean(row.get("due"))
            if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
                report.non_iso_dates.append((key, title, due))
                due = None

            if status in mhstore.OPEN_STATUSES and seq is not None:
                open_with_seq += 1
            if status_is_ambiguous(raw_status):
                report.add_ambiguous(key, title, raw_status.strip(), status)

            report.tasks += 1

            task = None
            if not dry_run:
                fields = dict(
                    status=status, status_raw=kept, done_at=done_at,
                    owner=normalize_owner(row.get("owner")),
                    seq=seq, is_next=1 if has_next_marker(raw_title) else 0,
                    section=row.get("_section"),
                    display_ord=clean(row.get("#")),
                    due=due,
                    notion_task_id=clean(row.get("notion task id")),
                )
                ident = (key, clean(row.get("#")) or title)
                if ident in existing:
                    task = store.update_task(existing[ident], actor="migration",
                                             title=title, **fields)
                else:
                    task = store.add_task(key, title, actor="migration",
                                          source="migration", **fields)
                seen_ids.add(task["id"])
                id_map.append({
                    "project": key, "old_row": row.get("#", ""),
                    "new_id": task["id"], "title": title,
                    "source_line": row["_line"],
                })

            # Prose that does not belong in a column: the Notes cell, plus
            # ken-yarmosh's narrative section for this row. Decided in both
            # modes so a dry run reports what it would write.
            notes = strip_generated_markers(row.get("notes", ""))
            if notes and len(notes) <= note_threshold:
                # Short enough to render inline. Previously this was dropped:
                # only cells past the threshold were kept, in a note file.
                store.update_task(task["id"], actor="migration", notes=notes)
            narrative = narratives.get(str(row.get("#", "")).strip())
            body_parts = []
            if notes and len(notes) > note_threshold:
                body_parts.append(notes)
            if narrative:
                body_parts.append(f"### {narrative[0]}\n\n{narrative[1]}")
            if body_parts:
                report.notes_written += 1
                report.note_chars += sum(len(p) for p in body_parts)
                if not dry_run:
                    rel = write_note_file(repo, project_dir, task["id"], title,
                                          "\n\n".join(body_parts), dry_run)
                    store.update_task(task["id"], actor="migration", note_path=rel)

        if open_with_seq == 0 and any(
                normalize_status(r.get("status", ""))[0] in mhstore.OPEN_STATUSES
                for r in rows):
            report.no_seq.append((key, sum(
                1 for r in rows
                if normalize_status(r.get("status", ""))[0] in mhstore.OPEN_STATUSES)))

    # A task-list on disk that no registry row points at is invisible to the
    # store. That is usually correct (closed clients, empty stubs) but it must
    # be stated, not assumed.
    registered = {str((repo / m["path"]).resolve()) for m in registry.values()}
    for found in sorted(repo.rglob("task-list.md")):
        if any(part in str(found) for part in SKIP_DIR_PARTS):
            continue
        if str(found.resolve()) in registered:
            continue
        rows = parse_task_rows(found.read_text().splitlines(), Report(),
                               str(found.relative_to(repo)))
        report.unregistered.append((str(found.relative_to(repo)), len(rows)))

    migrate_priorities(repo, store, report, dry_run=dry_run)

    if not dry_run:
        store.write_snapshot()
        write_id_map(repo, id_map)
    return report


def write_id_map(repo, id_map):
    out = repo / "operations" / "migration" / "id-map.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["project", "old_row", "new_id", "title", "source_line"])
        writer.writeheader()
        writer.writerows(id_map)
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(report, dry_run):
    head = "DRY RUN — nothing written" if dry_run else "migration complete"
    print(f"\n{'=' * 66}\n{head}\n{'=' * 66}")
    print(f"  projects        {report.projects}")
    print(f"  tasks           {report.tasks}")
    print(f"  note files      {report.notes_written}"
          f"  ({report.note_chars:,} chars of prose moved out of table cells)")
    print(f"  decisions files {report.decisions_written}")
    if report.week:
        state = f", {report.week_state}" if report.week_state else ""
        print(f"  week            {report.week}{state}  ({report.week_days} day cards, "
              f"{report.priority_matched} lines matched a task, "
              f"{report.priority_one_offs} became one-offs)")
        for name in report.deferred_sections:
            print(f"                  not scheduled: \"{name}\" -> backlog")

    def section(title, items, render):
        if not items:
            return
        print(f"\n{title} ({len(items)})")
        for item in items:
            print(f"  {render(item)}")

    section("NEEDS A RULING — status cells no rule reads honestly",
            report.ambiguous,
            lambda a: f"[{a[3]:11}] {a[0]:18} {a[1][:34]:36} {a[2][:52]}")
    section("scheduled on more than one day (store keeps the first)",
            report.priority_multi_day,
            lambda m: f"{m[0]:22} kept {m[1]}  also {m[2]}  {m[3]}")
    section("day-card lines with no task to attach to", report.priority_unmatched,
            lambda u: f"{u[2]:20} {str(u[1] or '-'):12} {u[0]}")
    section("duplicate files skipped", report.duplicates,
            lambda d: f"{d[0]}  identical to  {d[1]}")
    section("ROWS THAT LOST THEIR LEADING PIPE — invisible to every parser",
            report.orphan_rows,
            lambda o: f"{o[0]}:{o[1]}  {o[2]}")
    section("malformed rows skipped", report.malformed,
            lambda m: f"{m[0]}:{m[1]}  {m[2]} cells, header has {m[3]}")
    section("non-ISO due dates dropped", report.non_iso_dates,
            lambda d: f"{d[0]:18} {d[1][:40]:42} {d[2]!r}")
    section("open work with no Seq", report.no_seq,
            lambda s: f"{s[0]:18} {s[1]} open rows, none sequenced")
    section("files skipped", report.skipped_files,
            lambda s: f"{s[0]}  ({s[1]})")
    section("task-lists on disk with no registry row (NOT migrated)",
            report.unregistered,
            lambda u: f"{u[1]:>3} rows  {u[0]}")
    print()


FIRST_BOLD = re.compile(r"^\s*\*\*(.+?)\*\*\s*(.*)$", re.S)


def split_item_text(raw):
    """
    A day-card line -> (title, body).

    The convention is "**Title** [Load]. evidence trail", and the sweeps
    write the trail inline: 29 lines in the real file run past 400
    characters and the longest is 3,494. The bold span is the title. The
    rest is history and belongs in a note file, not in a title column.
    """
    text = KEY_TAG.sub("", raw)
    text = DONE_SUFFIX.sub("", text)

    match = FIRST_BOLD.match(text)
    if match:
        title, body = match.group(1), match.group(2)
    else:
        # No bold span: fall back to the first sentence, keeping decimals,
        # times and file extensions intact.
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z*(])", text.strip(), maxsplit=1)
        title = parts[0]
        body = parts[1] if len(parts) > 1 else ""

    # The load tag sits between the title and the prose and is captured
    # separately, so it belongs in neither.
    body = re.sub(r"^\s*\[(deep|medium|shallow)[^\]]*\]\s*[.:]?\s*", "",
                  body, flags=re.I)
    body = strip_generated_markers(body)
    title = re.sub(r"\[(deep|medium|shallow)[^\]]*\]", "", title, flags=re.I)
    return clean_title(title).strip(" .:"), body.strip()


def strip_item_text(raw):
    """A day-card line down to its title: no key tag, load tag, or done note."""
    return split_item_text(raw)[0]


def parse_priorities_file(repo):
    """
    The current week out of priorities.md: its start date, the day cards, the
    blocked list and the backlog.

    The file carries several "**Week of ...**" strings; the one that matters
    is the last before "## Weekly Goals". The others are references inside the
    capacity dashboard.
    """
    path = repo / "priorities.md"
    if not path.exists():
        return None
    lines = path.read_text().splitlines()

    try:
        goals_at = next(i for i, l in enumerate(lines) if WEEKLY_GOALS.match(l))
    except StopIteration:
        return None

    week_label, week_start, header_line = None, None, ""
    for line in reversed(lines[:goals_at]):
        m = WEEK_HEADER.match(line)
        if not m:
            continue
        label = m.group(1).strip()
        dm = MONTH_DAY_YEAR.search(label)
        if dm and dm.group(1).lower() in MONTHS:
            from datetime import date as _date
            week_label = label
            # The proposed/confirmed marker sits after the closing **, so
            # state has to be read from the whole line, not the label.
            header_line = line
            week_start = _date(int(dm.group(3)), MONTHS[dm.group(1).lower()],
                               int(dm.group(2))).isoformat()
            break

    days, blocked, backlog, deferred = [], [], [], []
    deferred_sections = []
    current_day, bucket = None, None
    for line in lines[goals_at + 1:]:
        day = DAY_CARD.match(line)
        if day:
            from datetime import date as _date
            year = int(week_start[:4]) if week_start else 2026
            try:
                iso = _date(year, int(day.group(2)), int(day.group(3))).isoformat()
            except ValueError:
                iso = None
            current_day = {"day": day.group(1).title(), "date": iso, "items": []}
            days.append(current_day)
            bucket = "day"
            continue
        if BLOCKED_HEADER.match(line):
            bucket, current_day = "blocked", None
            continue
        if BACKLOG_HEADER.match(line):
            bucket, current_day = "backlog", None
            continue
        if line.startswith("### "):
            # A sub-heading that is not a day card ends the current day.
            # Without this, a section like "Not this week, deliberately"
            # inherits the previous day and its items are scheduled as that
            # day's work, which is the opposite of what it says.
            deferred_sections.append(line[4:].strip())
            bucket, current_day = "deferred", None
            continue
        if line.startswith("## ") and not BACKLOG_HEADER.match(line):
            bucket, current_day = None, None
            continue
        box = CHECKBOX.match(line)
        if not box or bucket is None:
            continue
        tag = KEY_TAG.search(box.group(2))
        load = LOAD_TAG.search(box.group(2))
        parsed_title, parsed_body = split_item_text(box.group(2))
        item = {
            "raw": box.group(2).strip(),
            "title": parsed_title,
            "body": parsed_body,
            "done": box.group(1).lower() == "x",
            "key": tag.group(1) if tag else None,
            "ord": tag.group(2) if tag else None,
            "load": load.group(1).lower() if load else None,
        }
        if not item["title"]:
            continue
        if bucket == "day" and current_day:
            current_day["items"].append(item)
        elif bucket == "blocked":
            blocked.append(item)
        elif bucket == "backlog":
            backlog.append(item)
        elif bucket == "deferred":
            deferred.append(item)

    # "(proposed by Orca ...)" vs "(confirmed by MQ ...)" in the week header
    # is the difference between a proposal waiting on MQ and a locked week.
    label_l = (header_line or week_label or "").lower()
    state = "proposed" if "propos" in label_l else (
        "locked" if ("confirm" in label_l or "locked" in label_l) else None)

    return {"label": week_label, "week_start": week_start, "state": state,
            "days": days, "blocked": blocked, "backlog": backlog,
            "deferred": deferred, "deferred_sections": deferred_sections}


def migrate_priorities(repo, store, report, dry_run=False):
    """
    Put the current week into the store: planned_day on the tasks the day
    cards point at, and a one-off row for the lines that point nowhere.

    Tagged lines (`aba-academy#25`) resolve exactly. Untagged ones cannot be
    matched safely by text, so they become one-off tasks rather than guessing
    at which project row they meant.
    """
    week = parse_priorities_file(repo)
    if not week:
        return None

    by_tag = {}
    # One-offs have no key#N to match on, so they are matched by title. Without
    # this the priorities pass re-creates all of them on every run: 17 rows a
    # time, growing without bound.
    by_title = {}
    for task in store.tasks():
        if task["display_ord"]:
            by_tag[(task["project_key"], str(task["display_ord"]))] = task["id"]
        if task["project_key"] == mhstore.ONE_OFF:
            by_title[task["title"]] = task["id"]

    scheduled = {}   # task id -> the first day it was seen on

    def place(item, planned_day, status_hint):
        target = by_tag.get((item["key"], item["ord"])) if item["key"] else None
        if target is not None and target in scheduled:
            # Either the same task sits on two day cards ("Thu first pass, Fri
            # final"), or it is on a day card and also listed under blocked or
            # backlog. A task has one planned_day, so keep the first day it
            # was given and never let a later pass clear it.
            if planned_day and planned_day != scheduled[target]:
                report.priority_multi_day.append(
                    (f"{item['key']}#{item['ord']}", scheduled[target],
                     planned_day, item["title"][:44]))
            return
        if target is not None and planned_day:
            scheduled[target] = planned_day
        if target is None:
            report.priority_unmatched.append(
                (item["title"][:56], planned_day or status_hint,
                 "tagged, no such row" if item["key"] else "untagged"))
            if dry_run:
                if len(item.get("body") or "") > NOTE_THRESHOLD:
                    report.notes_written += 1
                    report.note_chars += len(item["body"])
                return
            body = (item.get("body") or "").strip()
            common = dict(
                status="done" if item["done"] else status_hint,
                planned_day=planned_day, load=item["load"],
                notes=body if 0 < len(body) <= NOTE_THRESHOLD else None)
            if item["title"] in by_title:
                row = store.update_task(by_title[item["title"]],
                                        actor="migration", **common)
            else:
                row = store.add_task(
                    mhstore.ONE_OFF, item["title"], actor="migration",
                    source="migration", done_at=None, **common)
                by_title[item["title"]] = row["id"]
            report.priority_one_offs += 1
            _stash_body(repo, store, report, row["id"], item, "one-off")
            return
        report.priority_matched += 1
        if dry_run:
            return
        body = (item.get("body") or "").strip()
        if 0 < len(body) <= NOTE_THRESHOLD and not store.task(target)["notes"]:
            store.update_task(target, actor="migration", notes=body)
        _stash_body(repo, store, report, target, item,
                    store.task(target)["project_key"])
        # Only ever set a day, never clear one. Blocked and backlog entries
        # carry no day and must not evict a task from the week.
        fields = {"planned_day": planned_day} if planned_day else {}
        if item["load"]:
            fields["load"] = item["load"]
        current = store.task(target)
        # The task list is the authority on whether work is finished; the day
        # card only says it was scheduled. Never un-finish a task from here.
        if item["done"] and current["status"] not in ("done", "canceled"):
            fields["status"] = "done"
        elif not item["done"] and current["status"] not in ("done", "canceled"):
            fields["status"] = status_hint
        store.update_task(target, actor="migration", **fields)

    for day in week["days"]:
        for item in day["items"]:
            place(item, day["date"], "planned")
    for item in week["blocked"]:
        place(item, None, "waiting")
    for item in week["backlog"]:
        place(item, None, "backlog")
    # Explicitly deferred: real work, but not this week's commitment.
    for item in week["deferred"]:
        place(item, None, "backlog")

    if not dry_run and week["week_start"]:
        # Stamp only the first time. Re-stamping on every run walks the lock
        # date forward, so a week locked last Friday would keep claiming it
        # was locked today.
        current = store.week(week["week_start"]) or {}
        stamp = {}
        if week["state"] == "proposed" and not current.get("proposed_at"):
            stamp["proposed_at"] = _now_iso()
        elif week["state"] == "locked" and not current.get("locked_at"):
            stamp["locked_at"] = _now_iso()
        store.upsert_week(week["week_start"], actor="migration",
                          notes=week["label"], **stamp)
    report.week = week["week_start"]
    report.week_days = len(week["days"])
    report.week_state = week["state"]
    report.deferred_sections = week["deferred_sections"]
    return week


def _stash_body(repo, store, report, task_id, item, project_key):
    """
    Put a day-card line's evidence trail in a note file.

    Sweeps write the whole history inline, so without this the prose either
    becomes a 3,000-character title or is dropped on the floor. Appends
    rather than overwrites, and only when this exact text is not already
    there, so a re-run does not grow the file.
    """
    body = (item.get("body") or "").strip()
    if len(body) <= NOTE_THRESHOLD:
        return
    task = store.task(task_id)
    project = store.project(project_key) or {}
    project_dir = project.get("dir") or "operations/one-off"
    rel = task["note_path"] or str(
        Path(project_dir) / "task-notes" / f"{task_id}-{slugify(task['title'])}.md")
    target = repo / rel
    existing = target.read_text() if target.exists() else ""
    if body[:120] in existing:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if not existing:
        existing = (f"# {task['title']}\n\n**Task:** `{task_id}`\n\n"
                    f"<!-- Created by migration from the weekly plan. "
                    f"Hand-written from here on. -->\n\n## History\n")
    with open(target, "w") as f:
        f.write(existing.rstrip("\n") + "\n\n### From the weekly plan\n\n"
                + body + "\n")
    if not task["note_path"]:
        store.update_task(task_id, actor="migration", note_path=rel)
    report.notes_written += 1
    report.note_chars += len(body)


def verify(repo, store, note_threshold=NOTE_THRESHOLD):
    """
    Cutover step 2: prove nothing was lost.

    Every substantial Notes cell in the markdown must be findable in a note
    file, and every task row must have a store row. Returns a list of
    problems; empty means clean.
    """
    repo = Path(repo)
    problems = []
    registry = read_registry(repo)
    by_title = {}
    for row in store.tasks():
        by_title.setdefault((row["project_key"], row["title"]), []).append(row)

    for key, meta in sorted(registry.items()):
        list_path = repo / meta["path"]
        if not list_path.exists():
            continue
        lines = list_path.read_text().splitlines()
        rows = parse_task_rows(lines, Report(), meta["path"])
        for row in rows:
            title = clean_title(row.get("task", ""))
            if not title:
                continue
            stored = by_title.get((key, title))
            if not stored:
                problems.append(f"{key}: no store row for {title[:48]!r}")
                continue
            notes = row.get("notes", "").strip()
            if len(notes) <= note_threshold:
                continue
            note_path = stored[0].get("note_path")
            if not note_path:
                problems.append(
                    f"{key}: {len(notes)} chars of notes on {title[:40]!r} "
                    f"but no note file")
                continue
            target = repo / note_path
            if not target.exists():
                problems.append(f"{key}: note file missing at {note_path}")
                continue
            body = target.read_text()
            # Compare on a distinctive slice; whitespace differs after the
            # markdown cell is unwrapped.
            probe = notes[:120].strip()
            if probe and probe not in body:
                problems.append(
                    f"{key}: note file for {title[:40]!r} does not contain "
                    f"the original Notes text")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="Migrate task-lists into the store")
    ap.add_argument("--from-repo", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, write nothing")
    ap.add_argument("--verify", action="store_true",
                    help="check an existing migration for lost prose, then exit")
    ap.add_argument("--note-threshold", type=int, default=NOTE_THRESHOLD)
    args = ap.parse_args(argv)

    repo = args.from_repo.expanduser()
    if not repo.exists():
        raise SystemExit(f"no repo at {repo}")

    store = mhstore.open_store(root=repo)
    try:
        if args.verify:
            problems = verify(repo, store, args.note_threshold)
            if problems:
                print(f"\n{len(problems)} PROBLEMS\n")
                for p in problems:
                    print(f"  {p}")
                return 1
            print("\nverified: every task row is in the store and every "
                  "substantial Notes cell is in a note file\n")
            return 0
        report = migrate(repo, store, dry_run=args.dry_run,
                         note_threshold=args.note_threshold)
        print_report(report, args.dry_run)
        if not args.dry_run:
            problems = verify(repo, store, args.note_threshold)
            if problems:
                print(f"VERIFY FOUND {len(problems)} PROBLEMS")
                for p in problems[:20]:
                    print(f"  {p}")
                return 1
            print("verified: no prose lost\n")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
