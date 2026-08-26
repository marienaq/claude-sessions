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
DIVIDER = re.compile(r"^\|[\s\-:|]+\|$")
HEADING = re.compile(r"^(#{2,4})\s+(.*)$")
YAML_FENCE = "```yaml"
KEY_DECISIONS = re.compile(r"^##\s+Key Decisions", re.I)
# ken-yarmosh keeps each task's real content in "## N. <title>" sections.
NARRATIVE = re.compile(r"^##\s+(\d+)\.\s+(.*)$")


def split_cells(line):
    parts = line.split("|")
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip() for p in parts]


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
        self.no_seq = []         # (key, open_count)

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

    # Wipe prior migration rows so a re-run does not duplicate.
    if not dry_run:
        store.conn.execute("DELETE FROM tasks WHERE source = 'migration'")
        store.conn.execute(
            "DELETE FROM projects WHERE key != ? AND key IN "
            "(SELECT key FROM projects)", (mhstore.ONE_OFF,))

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
            store.add_project(
                key,
                header.get("name") or meta["name"],
                actor="migration",
                dir=project_dir,
                status=normalize_project_status(header.get("status")),
                owner=normalize_owner(header.get("owner")),
                due=clean(header.get("due")),
                milestones=header.get("milestones", []),
                notion_project_id=clean(header.get("notion_project_id")),
                goal=parse_goal(lines),
            )
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
                task = store.add_task(
                    key, title, actor="migration", source="migration",
                    status=status, status_raw=kept, done_at=done_at,
                    owner=normalize_owner(row.get("owner")),
                    seq=seq, is_next=1 if has_next_marker(raw_title) else 0,
                    section=row.get("_section"),
                    display_ord=clean(row.get("#")),
                    due=due,
                    notion_task_id=clean(row.get("notion task id")),
                )
                id_map.append({
                    "project": key, "old_row": row.get("#", ""),
                    "new_id": task["id"], "title": title,
                    "source_line": row["_line"],
                })

            # Prose that does not belong in a column: the Notes cell, plus
            # ken-yarmosh's narrative section for this row. Decided in both
            # modes so a dry run reports what it would write.
            notes = row.get("notes", "").strip()
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

    def section(title, items, render):
        if not items:
            return
        print(f"\n{title} ({len(items)})")
        for item in items:
            print(f"  {render(item)}")

    section("NEEDS A RULING — status cells no rule reads honestly",
            report.ambiguous,
            lambda a: f"[{a[3]:11}] {a[0]:18} {a[1][:34]:36} {a[2][:52]}")
    section("duplicate files skipped", report.duplicates,
            lambda d: f"{d[0]}  identical to  {d[1]}")
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
