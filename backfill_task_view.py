#!/usr/bin/env python3
"""
backfill_task_view — the one-off after schema 2 ships (plan §A5).

    python3 backfill_task_view.py --repo ~/Projects/mellonhead           # dry run
    python3 backfill_task_view.py --repo ~/Projects/mellonhead --apply

Two things, both reviewed as a printed diff before anything is written:

  brief_path   Scan the repo for *brief*.md files whose header block carries
               `**Task:** \\`key#N\\``, and set brief_path on that row where it
               is empty. One row in 291 had one before this.

  questions    Turn each open `Question:` task row into a question row on
               its project (task_id null), and close the task row with a
               note pointing at the question id.

Deliberately not done: importing the blank `**MQ:**` slots from briefs.
Task note #242 shows most of them were answered elsewhere, and importing
them would recreate the false blockers this whole design exists to remove.
Orca re-raises any that are still real through `mh question add` on its
next scoped pass.
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mhgen
import mhstore

TASK_HEADER = re.compile(r"^\*\*Task:\*\*\s*`?([a-z0-9][a-z0-9-]*)#(\d+)`?", re.M)
SKIP_DIRS = ("task-notes", "migration", "backups", ".git", "node_modules")
QUESTION_TITLE = re.compile(r"^\s*Question:\s*(.+)$", re.I | re.S)


def find_briefs(repo):
    """(relative path, key, number) for every brief that names its task."""
    found = []
    for path in sorted(repo.rglob("*brief*.md")):
        rel = path.relative_to(repo)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            head = path.read_text(errors="ignore")[:4000]
        except OSError:
            continue
        # Only the header block, above the first rule: a note file that
        # quotes a brief's header further down is not a brief.
        head = head.split("\n---", 1)[0]
        m = TASK_HEADER.search(head)
        if m:
            found.append((str(rel), m.group(1), m.group(2)))
    return found


def task_by_tag(store, key, number):
    rows = [t for t in store.tasks(project_key=key)
            if str(t["display_ord"]) == str(number)]
    return rows[0] if len(rows) == 1 else None


def plan_briefs(store, repo):
    actions = []
    for rel, key, number in find_briefs(repo):
        task = task_by_tag(store, key, number)
        if task is None:
            actions.append(("skip", rel, f"{key}#{number}", "no such row"))
        elif task["brief_path"] == rel:
            actions.append(("skip", rel, f"{key}#{number}", "already set"))
        elif task["brief_path"]:
            actions.append(("skip", rel, f"{key}#{number}",
                            f"row already points at {task['brief_path']}"))
        else:
            actions.append(("set", rel, f"{key}#{number}", task["id"]))
    return actions


def plan_questions(store):
    actions = []
    for task in store.tasks(open_only=True):
        m = QUESTION_TITLE.match(task["title"] or "")
        if not m:
            continue
        text = " ".join(m.group(1).split())
        actions.append(("convert", task, text))
    return actions


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", type=Path,
                    default=os.environ.get("MELLONHEAD_ROOT",
                                           Path.home() / "Projects" / "mellonhead"))
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--actor", default="migration")
    args = ap.parse_args(argv)

    repo = Path(args.repo).expanduser()
    if not (repo / "operations" / "tasks.db").exists():
        print(f"no task store at {repo}/operations/tasks.db", file=sys.stderr)
        return 2
    store = mhstore.open_store(root=repo, seed_settings=False)
    try:
        briefs = plan_briefs(store, repo)
        questions = plan_questions(store)

        print("brief_path:")
        for action, rel, tag, detail in briefs:
            if action == "set":
                print(f"  SET   {tag:<22} {rel}")
            else:
                print(f"  skip  {tag:<22} {rel}  ({detail})")
        if not briefs:
            print("  (no briefs with a **Task:** header found)")

        print("\nquestions:")
        for _, task, text in questions:
            where = f"{task['project_key']}#{task['display_ord']}" \
                if task["display_ord"] else f"#{task['id']}"
            blocks = f"  blocks {task['waiting_on']}" if task["waiting_on"] else ""
            print(f"  CONVERT {where:<20} -> Q?: {text[:90]}{blocks}")
        if not questions:
            print("  (no open Question: rows)")

        if not args.apply:
            print("\ndry run; pass --apply to write.")
            return 0

        touched = set()
        with store.write():
            for action, rel, tag, task_id in briefs:
                if action != "set":
                    continue
                store.update_task(task_id, actor=args.actor, brief_path=rel)
                touched.add(store.task(task_id)["project_key"])
            for _, task, text in questions:
                q = store.add_question(
                    text, actor=task["source"] if task["source"] in ("capture",) else args.actor,
                    project_key=task["project_key"],
                    blocks=task["waiting_on"] or None)
                note = f"→ Q{q['id']}"
                notes = f"{task['notes']} {note}".strip() if task["notes"] else note
                store.update_task(task["id"], actor=args.actor, notes=notes)
                store.complete_task(task["id"], actor=args.actor)
                touched.add(task["project_key"])
                print(f"  wrote Q{q['id']} for #{task['id']}")
        for key in sorted(touched):
            if key != mhstore.ONE_OFF:
                mhgen.generate_task_list(store, repo, key)
        mhgen.generate_priorities(store, repo)
        mhgen.generate_dashboard(store, repo)
        print(f"\napplied; regenerated views for {', '.join(sorted(touched)) or 'nothing'}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
