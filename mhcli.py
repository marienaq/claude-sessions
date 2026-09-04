#!/usr/bin/env python3
"""
mh — the write path for agents.

Agents read the generated markdown for context and write through this. They
must not edit task-list.md or priorities.md directly: those are generated, so
a hand edit is silently overwritten the next time anything regenerates.

    mh task next --all
    mh task done aba-academy#25
    mh task add aba-academy "Draft the outline" --load deep --day 2026-09-03
    mh task status aba-champions#23 waiting --waiting-on "Sharla's review"
    mh task note aba-academy#25 "Sharla returned the copy 9/2"
    mh plan lock 2026-08-31
    mh verify

Every write regenerates the views it affects, so the markdown an agent reads
next is never behind the store. Pass --no-regen in a loop and run
`mh regen` once at the end.

Workstream 2.5. Stdlib only.
"""

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import mhgen
import mhstore
from mhstore import ONE_OFF, OPEN_STATUSES, StoreError

# "aba-academy#25", "#230" and "230" all address a task. The bare-hash form
# matters because that is what this tool prints for a row with no per-project
# number, and output you cannot paste back in is a trap.
IDENT = re.compile(r"^(?:([a-z0-9][a-z0-9-]*))?#?(\d+)$")


class CliError(Exception):
    pass


def _task_status(value):
    """
    Accept either spelling of cancel, and the markdown's wording.

    The task enum says "canceled" and the project enum says "cancelled";
    making a caller remember which noun takes which spelling is a trap.
    """
    status = mhstore.normalize_task_status(value)
    if status is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a task status "
            f"({', '.join(mhstore.TASK_STATUSES)})")
    return status


def resolve(store, ident):
    """
    'aba-academy#25' -> that project's row 25. A bare number is a store id.

    Agents write the key form because it is what appears in the markdown they
    read; the bare id exists for scripts that already hold one.
    """
    match = IDENT.match(str(ident).strip().strip("`"))
    if not match:
        raise CliError(f"{ident!r} is not a task id or key#N")
    key, number = match.group(1), match.group(2)
    if key:
        rows = [t for t in store.tasks(project_key=key)
                if str(t["display_ord"]) == number]
        if not rows:
            raise CliError(f"no task {key}#{number}")
        if len(rows) > 1:
            raise CliError(f"{key}#{number} matches {len(rows)} rows; "
                           f"use a store id: {[r['id'] for r in rows]}")
        return rows[0]
    task = store.task(int(number))
    if not task:
        raise CliError(f"no task with id {number}")
    return task


def tag(task):
    if task["project_key"] != ONE_OFF and task["display_ord"]:
        return f"{task['project_key']}#{task['display_ord']}"
    return f"#{task['id']}"


def show(task, prefix=""):
    bits = [f"{prefix}{tag(task):<22} {task['status']:<11} {task['title'][:56]}"]
    extra = []
    if task["owner"] != "mq":
        extra.append(f"owner={task['owner']}")
    if task["due"]:
        extra.append(f"due={task['due']}")
    if task["planned_day"]:
        extra.append(f"day={task['planned_day']}")
    if task["waiting_on"]:
        extra.append(f"waiting on {task['waiting_on']}")
    if extra:
        bits.append("    " + "  ".join(extra))
    return "\n".join(bits)


def regenerate(store, repo, touched_projects, quiet=False):
    """
    Invariant 4: a write regenerates the views it affects, in the same call.
    Otherwise the markdown an agent reads next is behind the store it just
    wrote to.
    """
    written = []
    for key in sorted(set(touched_projects)):
        if key == ONE_OFF:
            continue
        if mhgen.generate_task_list(store, repo, key) is not None:
            written.append(f"{key}/task-list.md")
    if mhgen.generate_priorities(store, repo) is not None:
        written.append("priorities.md")
    mhgen.generate_dashboard(store, repo)
    written.append("operations/projects-dashboard.md")
    if written and not quiet:
        print("regenerated: " + ", ".join(written))
    return written


# ---------------------------------------------------------------------------
# task
# ---------------------------------------------------------------------------

def cmd_task_next(store, repo, args):
    if args.all:
        rows = []
        for project in store.projects():
            if project["status"] not in ("active", "waiting"):
                continue
            if project["key"] == ONE_OFF:
                continue
            nxt = store.next_task(project["key"])
            if nxt:
                rows.append((project, nxt))
        if not rows:
            print("nothing open")
            return 0
        for project, task in rows:
            flag = "" if project["status"] == "active" else f"  ({project['status']})"
            print(f"{project['key']:<18}{flag}")
            print(show(task, prefix="  "))
        return 0

    if not args.project:
        raise CliError("give a project key, or --all")
    task = store.next_task(args.project)
    if not task:
        print(f"{args.project}: nothing open")
        return 0
    print(show(task))
    return 0


def cmd_task_find(store, repo, args):
    """
    Used by the SessionStart hook: given words from what MQ just asked for,
    surface candidate rows so the session starts attached to real work.
    """
    words = [w.lower() for w in args.words if len(w) > 2]
    if not words:
        raise CliError("give at least one word longer than two characters")
    scored = []
    for task in store.tasks(open_only=not args.include_done):
        hay = f"{task['title']} {task['notes'] or ''} {task['project_key']}".lower()
        hits = sum(1 for w in words if w in hay)
        if hits:
            scored.append((hits, -(task["seq"] or 9999), task))
    scored.sort(key=lambda r: (-r[0], -r[1]))
    if not scored:
        print("no matching task")
        return 1
    for _, _, task in scored[:args.limit]:
        print(show(task))
    return 0


def cmd_task_add(store, repo, args):
    project = args.project or ONE_OFF
    if not store.project(project):
        raise CliError(f"no project {project!r}; see `mh project list`")
    task = store.add_task(
        project, args.title, actor=args.actor,
        source="capture" if args.unconfirmed else args.source,
        confirmed=0 if args.unconfirmed else 1,
        status="planned" if args.day else "backlog",
        planned_day=args.day, load=args.load, due=args.due,
        owner=args.owner or "mq", notes=args.note)
    print(show(task))
    if not args.no_regen:
        regenerate(store, repo, [project])
    return 0


def cmd_task_done(store, repo, args):
    task = resolve(store, args.task)
    if task["status"] == "done":
        print(f"already done: {show(task)}")
        return 0
    store.complete_task(task["id"], actor=args.actor)
    print(show(store.task(task["id"])))
    nxt = store.next_task(task["project_key"])
    if nxt:
        print(f"\nnext in {task['project_key']}:")
        print(show(nxt, prefix="  "))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_task_status(store, repo, args):
    task = resolve(store, args.task)
    if args.status not in mhstore.TASK_STATUSES:
        raise CliError(f"status must be one of "
                       f"{', '.join(mhstore.TASK_STATUSES)}")
    fields = {"status": args.status}
    if args.waiting_on is not None:
        fields["waiting_on"] = args.waiting_on
    if args.status != "waiting" and task["waiting_on"] and args.waiting_on is None:
        fields["waiting_on"] = None
    store.update_task(task["id"], actor=args.actor, **fields)
    print(show(store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_task_note(store, repo, args):
    """
    Append a dated entry to the task's note file, creating it if needed.

    The note file is hand-written territory; this only ever appends, and
    never rewrites what is already there.
    """
    task = resolve(store, args.task)
    project = store.project(task["project_key"]) or {}
    rel = task["note_path"]
    if not rel:
        base = project.get("dir") or "operations/one-off"
        rel = str(Path(base) / "task-notes"
                  / f"{task['id']}-{mhstore.slugify(task['title'])}.md")
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(
            f"# {task['title']}\n\n**Task:** `{tag(task)}`\n\n"
            f"<!-- Hand-written. Agents append dated entries below. -->\n\n"
            f"## History\n")
    stamp = date.today().isoformat()
    with open(target, "a") as f:
        f.write(f"\n### {stamp} ({args.actor})\n\n{args.text}\n")
    if not task["note_path"]:
        store.update_task(task["id"], actor=args.actor, note_path=rel)
    print(f"appended to {rel}")
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_task_plan(store, repo, args):
    """
    Put an existing row on a day, or take it off one.

    Planning a week is mostly moving already-open rows onto days, so without
    this the CLI could create work but never schedule it: Mode 5 could not
    finalize and the Friday job could not write a proposed week at all.
    """
    task = resolve(store, args.task)
    if args.day:
        store.plan_task(task["id"], args.day, actor=args.actor)
    else:
        store.unplan_task(task["id"], actor=args.actor)
    print(show(store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_task_load(store, repo, args):
    task = resolve(store, args.task)
    store.update_task(task["id"], actor=args.actor,
                      load=None if args.load == "none" else args.load)
    print(show(store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_plan_propose(store, repo, args):
    """
    Mark a week proposed: assembled, waiting on MQ, not committed.

    Nothing set proposed_at except migration, so the Friday job had no way to
    say "here is next week, react to it".
    """
    week = store.week(args.week) or {}
    if week.get("locked_at") and not args.force:
        raise CliError(f"{args.week} is already locked; pass --force to "
                       f"reopen it as a proposal")
    fields = {"proposed_at": datetime.now().isoformat(timespec="seconds")}
    if args.force:
        fields["locked_at"] = None
    if args.notes:
        fields["notes"] = args.notes
    store.upsert_week(args.week, actor=args.actor, **fields)
    rows = [t for t in store.tasks() if t["planned_day"]
            and args.week <= t["planned_day"] <= _week_end(args.week)]
    print(f"proposed {args.week}  ({len(rows)} rows on day cards)")
    print("MQ confirms in the session manager, or with `mh plan lock "
          f"{args.week}`.")
    if not args.no_regen:
        regenerate(store, repo, {t["project_key"] for t in rows})
    return 0


def cmd_task_seq(store, repo, args):
    task = resolve(store, args.task)
    store.update_task(task["id"], actor=args.actor, seq=args.seq, is_next=0)
    print(show(store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_task_confirm(store, repo, args):
    task = resolve(store, args.task)
    store.confirm_task(task["id"], actor=args.actor)
    print(show(store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_task_brief(store, repo, args):
    task = resolve(store, args.task)
    store.update_task(task["id"], actor=args.actor, brief_path=args.path)
    print(show(store.task(task["id"])))
    return 0


def cmd_task_list(store, repo, args):
    rows = store.tasks(project_key=args.project,
                       open_only=not args.include_done)
    if args.day:
        rows = [t for t in rows if t["planned_day"] == args.day]
    if args.owner:
        rows = [t for t in rows if t["owner"] == args.owner]
    if not rows:
        print("no matching tasks")
        return 0
    for task in rows:
        print(show(task))
    return 0


# ---------------------------------------------------------------------------
# plan, project, report
# ---------------------------------------------------------------------------

def cmd_plan_lock(store, repo, args):
    store.lock_week(args.week, actor=args.actor)
    week = store.week(args.week)
    planned = [t for t in store.tasks() if t["planned_day"]
               and args.week <= t["planned_day"] <= _week_end(args.week)]
    print(f"locked {args.week} at {week['locked_at']}  ({len(planned)} rows)")
    if not args.no_regen:
        regenerate(store, repo, {t["project_key"] for t in planned})
    return 0


def _week_end(week_start):
    from datetime import timedelta
    return (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()


def cmd_plan_show(store, repo, args):
    week = args.week or _current_week(store)
    if not week:
        print("no week in the store")
        return 1
    row = store.week(week) or {}
    state = ("locked" if row.get("locked_at") else
             "proposed" if row.get("proposed_at") else "unconfirmed")
    print(f"week of {week}  ({state})")
    rows = [t for t in store.tasks()
            if t["planned_day"] and week <= t["planned_day"] <= _week_end(week)]
    by_day = {}
    for task in rows:
        by_day.setdefault(task["planned_day"], []).append(task)
    for day in sorted(by_day):
        done = sum(1 for t in by_day[day] if t["status"] == "done")
        print(f"\n{day}  ({done}/{len(by_day[day])} done)")
        for task in by_day[day]:
            mark = "x" if task["status"] == "done" else " "
            print(f"  [{mark}] {tag(task):<20} {task['title'][:52]}")
    return 0


def _current_week(store):
    row = store.conn.execute(
        "SELECT week_start FROM weeks ORDER BY week_start DESC LIMIT 1").fetchone()
    return row["week_start"] if row else None


def cmd_project_list(store, repo, args):
    for project in store.projects():
        if project["key"] == ONE_OFF and not args.all:
            continue
        if not args.all and project["status"] in mhstore.ARCHIVED_PROJECT_STATUSES:
            continue
        open_count = len(store.tasks(project_key=project["key"], open_only=True))
        nxt = store.next_task(project["key"])
        print(f"{project['key']:<18} {project['status']:<9} {open_count:>3} open  "
              f"{(nxt['title'][:44] if nxt else '—')}")
    return 0


def cmd_project_add(store, repo, args):
    if store.project(args.key):
        raise CliError(f"project {args.key!r} already exists")
    project = store.add_project(
        args.key, args.name, actor=args.actor, dir=args.dir,
        status=mhstore.normalize_project_status(args.status),
        owner=args.owner or "mq", due=args.due,
        notion_project_id=args.notion, goal=args.goal)
    print(f"{project['key']:<18} {project['status']:<9} {project['name']}")
    if project["dir"]:
        print(f"  dir: {project['dir']}")
        print("  add a row to operations/project-registry.md so the migration "
              "and the dashboard see it too.")
    return 0


def cmd_project_status(store, repo, args):
    status = mhstore.normalize_project_status(args.status)
    store.update_project(args.project, actor=args.actor, status=status)
    print(f"{args.project}: {status}")
    if not args.no_regen:
        regenerate(store, repo, [args.project])
    return 0


def cmd_report(store, repo, args):
    week = args.week or _current_week(store)
    rows = [t for t in store.tasks()
            if t["planned_day"] and week <= t["planned_day"] <= _week_end(week)]
    done = [t for t in rows if t["status"] == "done"]
    open_rows = [t for t in rows if t["status"] in OPEN_STATUSES]
    blocked = [t for t in store.tasks() if t["status"] == "waiting"]
    print(f"Week of {week}: {len(done)} of {len(rows)} done.")
    if open_rows:
        print("\nStill open:")
        for task in open_rows:
            print(f"  {task['planned_day']}  {task['title'][:60]}")
    if blocked:
        print(f"\nBlocked or waiting ({len(blocked)}):")
        for task in blocked[:10]:
            note = f" — {task['waiting_on']}" if task["waiting_on"] else ""
            print(f"  {task['title'][:56]}{note}")
    return 0


def cmd_verify(store, repo, args):
    """
    Cutover step 4: nothing should hand-edit a generated file.

    Regenerates into memory and compares. A difference means either someone
    edited the markdown directly or the store moved without regenerating.
    """
    stale = []
    for project in store.projects():
        if project["key"] == ONE_OFF or not project["dir"]:
            continue
        path = repo / project["dir"] / "task-list.md"
        if not path.exists():
            continue
        expected = mhgen.generate_task_list(store, repo, project["key"],
                                            dry_run=True)
        if expected is None:
            continue
        if _strip_stamp(expected) != _strip_stamp(path.read_text()):
            stale.append(f"{project['dir']}/task-list.md")
    expected = mhgen.generate_priorities(store, repo, dry_run=True)
    priorities = repo / "priorities.md"
    if expected and priorities.exists():
        if _strip_stamp(expected) != _strip_stamp(priorities.read_text()):
            stale.append("priorities.md")

    if not stale:
        print("verified: every generated file matches the store")
        return 0
    print(f"{len(stale)} generated file(s) differ from the store:")
    for name in stale:
        print(f"  {name}")
    print("\nEither someone edited a generated file by hand, or a write "
          "skipped regeneration. `mh regen` rewrites them from the store.")
    return 1


def _strip_stamp(text):
    return "\n".join(l for l in text.splitlines()
                     if not l.startswith("<!-- GENERATED")).strip()


def cmd_regen(store, repo, args):
    written = mhgen.generate_all(store, repo)
    print(f"regenerated {len(written)} files")
    return 0


def cmd_export(store, repo, args):
    """
    Cutover step 6: a readable copy that does not need SQLite to open.

    The rollback path, and the weekly safety net.
    """
    out = repo / "operations" / "tasks-export.md"
    lines = [f"# Task export, {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    for project in store.projects():
        rows = store.tasks(project_key=project["key"])
        if not rows:
            continue
        lines += [f"## {project['name']} (`{project['key']}`) — "
                  f"{project['status']}", ""]
        for task in rows:
            mark = "x" if task["status"] == "done" else " "
            bits = [f"- [{mark}] {task['title']}"]
            if task["owner"] != "mq":
                bits.append(f"({task['owner']})")
            if task["due"]:
                bits.append(f"due {task['due']}")
            if task["notes"]:
                bits.append(f"— {task['notes']}")
            lines.append(" ".join(bits))
        lines.append("")
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out.relative_to(repo)}  ({len(store.tasks())} tasks)")
    return 0


def cmd_check_skills(store, repo, args):
    """
    Validate every mh command quoted in the skill and agent files.

    Reachable through the CLI so nobody needs to know where the
    implementation checkout lives.
    """
    import check_mh_usage
    target = args.path or (repo / ".claude")
    return check_mh_usage.main([str(target)])


def cmd_docs(store, repo, args):
    print(render_reference())
    return 0


def render_reference():
    """
    The command table, built from the parser itself.

    Hand-written docs drift from the code and nobody notices until an agent
    runs a command that no longer exists. This is generated, and a test
    asserts the committed file still matches.
    """
    parser = build_parser()
    lines = [HEADER.strip(), "", "## Commands", ""]

    groups = next(a for a in parser._actions
                  if isinstance(a, argparse._SubParsersAction))
    top_blurbs = {a.dest: (a.help or "") for a in groups._choices_actions}
    flat = []
    for name, sub in groups.choices.items():
        nested = next((a for a in sub._actions
                       if isinstance(a, argparse._SubParsersAction)), None)
        if nested is None:
            flat.append((name, groups._choices_actions))
            continue

        # argparse keeps a subcommand's one-line help on the parent's
        # _choices_actions, not on the subparser itself.
        blurbs = {a.dest: (a.help or "") for a in nested._choices_actions}
        lines += [f"### `mh {name}`", "",
                  "| Command | Options | What it does |", "|---|---|---|"]
        for action_name, leaf in nested.choices.items():
            positional = [a.dest for a in leaf._actions
                          if not a.option_strings and a.dest != "help"]
            flags = sorted({o for a in leaf._actions for o in a.option_strings
                            if o.startswith("--")
                            and o not in ("--help", "--repo", "--actor",
                                          "--no-regen", "--json")})
            signature = " ".join([f"mh {name} {action_name}"]
                                 + [f"<{d}>" for d in positional])
            lines.append(f"| `{signature}` | "
                         f"{' '.join(f'`{f}`' for f in flags) or '—'} | "
                         f"{blurbs.get(action_name, '') or '—'} |")
        lines.append("")

    if flat:
        lines += ["### Single commands", "",
                  "| Command | What it does |", "|---|---|"]
        for name, _ in flat:
            lines.append(f"| `mh {name}` | {top_blurbs.get(name, '') or '—'} |")
        lines.append("")

    lines += ["### Global flags", "",
              "Accepted before or after the subcommand.", "",
              "| Flag | Meaning |", "|---|---|",
              "| `--repo PATH` | Content repo. Defaults to `$MELLONHEAD_ROOT`, "
              "or the repo the `operations/mh` shim lives in. |",
              "| `--actor NAME` | Who is writing. Recorded on every audit line. "
              "Pass your agent name. |",
              "| `--no-regen` | Skip regenerating the views. Use in a loop, "
              "then run `mh regen` once. |",
              ""]
    lines.append(FOOTER.strip())
    return "\n".join(lines) + "\n"


HEADER = """
<!-- GENERATED by `mh docs`. Do not hand-edit; a test asserts it matches the
     parser. Regenerate with: mh docs > operations/mh-reference.md -->

# `mh` command reference

The write path for agents. Read the generated markdown for context, write
through this.

## The rules

1. **Never edit `task-list.md` or `priorities.md` by hand.** They are
   generated from the store. A hand edit is silently overwritten the next
   time anything regenerates, and `mh verify` will report the file as
   diverged in the meantime.
2. **Note files, briefs and `decisions.md` are yours.** The generator never
   touches them. Use `mh task note` to append, or write them directly.
3. **Every write regenerates the views it affects**, so the markdown you read
   next is never behind the store you just wrote to.

## Identifying a task

Three forms work everywhere a task is expected:

| Form | Use |
|---|---|
| `aba-academy#25` | The tag on a day-card line and in a task list. Prefer it. |
| `#231` | What this tool prints for a row with no project number. |
| `231` | A bare store id, for scripts that already hold one. |

Backticks are tolerated, so `` `aba-academy#25` `` copied out of markdown
works as-is.
"""

FOOTER = """
## Recipes

**Close a task and see what is next**

```
mh task done aba-academy#25 --actor orca
```

Prints the row, then the project's next open action.

**Start a session on the right work**

```
mh task find summarizing rise course
```

Ranks open tasks by word overlap. This is what the SessionStart hook calls.

**Record a proposal that MQ has not agreed to**

```
mh task add aba-academy "Draft the Q4 outline" --unconfirmed --actor capture
```

Lands in the backlog with `confirmed=0` and appears in the dashboard's
Proposed panel. It is not a commitment until MQ keeps it.

**Note what happened without touching the task list**

```
mh task note aba-academy#25 "Sharla returned the copy 9/2, two edits."
```

Appends a dated entry under `## History` in the task's note file, creating
the file if it does not exist. Never rewrites what is there.

**Say what a task is waiting on**

```
mh task status aba-champions#23 waiting --waiting-on "Sharla's review"
```

Moving the row off `waiting` later clears the reason automatically.

## Safety

| Command | When |
|---|---|
| `mh verify` | Checks every generated file still matches the store. Non-zero if one was hand-edited or a write skipped regeneration. Run it nightly. |
| `mh regen` | Rewrites every generated view from the store. The fix when `verify` fails. |
| `mh export` | Writes `operations/tasks-export.md`, readable without SQLite. The rollback path. |

`mh` refuses to run against a repo that has no `operations/tasks.db` rather
than creating an empty one. That is deliberate: pointed at the wrong repo it
would otherwise write an empty store and regenerate the views from nothing.
"""


# ---------------------------------------------------------------------------

def build_parser():
    # The global flags are accepted on either side of the subcommand.
    # "mh task done X --no-regen" is what anyone writes by reflex, and having
    # only the "mh --no-regen task done X" form work is a trap. SUPPRESS keeps
    # the subparser from overriding a value given before the subcommand.
    def add_globals(parser, suppressed):
        blank = argparse.SUPPRESS if suppressed else None
        parser.add_argument("--repo", type=Path,
                            default=blank if suppressed else None,
                            help="content repo (default $MELLONHEAD_ROOT)")
        parser.add_argument("--actor",
                            default=blank if suppressed else "agent",
                            help="who is writing; recorded in the audit log")
        parser.add_argument("--no-regen", action="store_true",
                            default=argparse.SUPPRESS if suppressed else False,
                            help="skip regenerating views; run `mh regen` after")
        parser.add_argument("--json", action="store_true",
                            default=argparse.SUPPRESS if suppressed else False)

    # The subcommand copies are suppressed so that a value given before the
    # subcommand survives. A subparser writes its defaults over the namespace
    # the top level already filled in, so sharing one definition silently
    # reset --repo to None whenever it was passed first.
    common = argparse.ArgumentParser(add_help=False)
    add_globals(common, suppressed=True)

    ap = argparse.ArgumentParser(
        prog="mh",
        description="Write path for agents. Never edit generated "
                    "markdown by hand; it is overwritten.")
    add_globals(ap, suppressed=False)
    sub = ap.add_subparsers(dest="group", required=True)

    task = sub.add_parser("task", help="task operations",
                          parents=[common]).add_subparsers(
        dest="action", required=True)

    p = task.add_parser("next", help="the next action for a project", parents=[common])
    p.add_argument("project", nargs="?")
    p.add_argument("--all", action="store_true", help="every active project")
    p.set_defaults(fn=cmd_task_next)

    p = task.add_parser("find", help="search open tasks by words", parents=[common])
    p.add_argument("words", nargs="+")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--include-done", action="store_true")
    p.set_defaults(fn=cmd_task_find)

    p = task.add_parser("list", help="list tasks", parents=[common])
    p.add_argument("project", nargs="?")
    p.add_argument("--day")
    p.add_argument("--owner")
    p.add_argument("--include-done", action="store_true")
    p.set_defaults(fn=cmd_task_list)

    p = task.add_parser("add", help="create a task", parents=[common])
    p.add_argument("project", nargs="?", help="project key; omit for a one-off")
    p.add_argument("title")
    p.add_argument("--day", help="planned day, YYYY-MM-DD")
    p.add_argument("--due")
    p.add_argument("--load", choices=mhstore.LOADS)
    p.add_argument("--owner")
    p.add_argument("--note", help="one sentence, rendered inline")
    p.add_argument("--source", default="agent", choices=mhstore.SOURCES)
    p.add_argument("--unconfirmed", action="store_true",
                   help="a proposal for MQ, not a commitment")
    p.set_defaults(fn=cmd_task_add)

    p = task.add_parser("done", help="close a task", parents=[common])
    p.add_argument("task", help="key#N or a store id")
    p.set_defaults(fn=cmd_task_done)

    p = task.add_parser("status", help="set a task's status", parents=[common])
    p.add_argument("task")
    p.add_argument("status", type=_task_status,
                   help="one of: " + ", ".join(mhstore.TASK_STATUSES))
    p.add_argument("--waiting-on")
    p.set_defaults(fn=cmd_task_status)

    p = task.add_parser("note", help="append a dated entry to the note file", parents=[common])
    p.add_argument("task")
    p.add_argument("text")
    p.set_defaults(fn=cmd_task_note)

    p = task.add_parser("plan", help="put a task on a day, or take it off", parents=[common])
    p.add_argument("task")
    p.add_argument("day", nargs="?", help="YYYY-MM-DD; omit to unschedule")
    p.set_defaults(fn=cmd_task_plan)

    p = task.add_parser("load", help="set a task's load tag", parents=[common])
    p.add_argument("task")
    p.add_argument("load", choices=(*mhstore.LOADS, "none"))
    p.set_defaults(fn=cmd_task_load)

    p = task.add_parser("seq", help="set ordering within a project", parents=[common])
    p.add_argument("task")
    p.add_argument("seq", type=int)
    p.set_defaults(fn=cmd_task_seq)

    p = task.add_parser("confirm", help="accept a capture proposal", parents=[common])
    p.add_argument("task")
    p.set_defaults(fn=cmd_task_confirm)

    p = task.add_parser("brief", help="record a brief file path", parents=[common])
    p.add_argument("task")
    p.add_argument("path")
    p.set_defaults(fn=cmd_task_brief)

    plan = sub.add_parser("plan", help="the week",
                          parents=[common]).add_subparsers(
        dest="action", required=True)
    p = plan.add_parser("lock", help="commit a proposed week", parents=[common])
    p.add_argument("week", help="Monday, YYYY-MM-DD")
    p.set_defaults(fn=cmd_plan_lock)
    p = plan.add_parser("propose", help="mark a week proposed, awaiting MQ", parents=[common])
    p.add_argument("week", help="Monday, YYYY-MM-DD")
    p.add_argument("--notes", help="the reasoning behind the proposal")
    p.add_argument("--force", action="store_true",
                   help="reopen a week that is already locked")
    p.set_defaults(fn=cmd_plan_propose)

    p = plan.add_parser("show", help="the week's day cards", parents=[common])
    p.add_argument("week", nargs="?")
    p.set_defaults(fn=cmd_plan_show)

    project = sub.add_parser("project", help="projects",
                             parents=[common]).add_subparsers(
        dest="action", required=True)
    p = project.add_parser("add", help="register a new project", parents=[common])
    p.add_argument("key", help="short slug, e.g. aba-academy")
    p.add_argument("name")
    p.add_argument("--dir", help="repo-relative directory holding its task-list.md")
    p.add_argument("--status", default="active")
    p.add_argument("--owner")
    p.add_argument("--due")
    p.add_argument("--notion", help="Notion project id, contractor rows only")
    p.add_argument("--goal")
    p.set_defaults(fn=cmd_project_add)

    p = project.add_parser("list", help="every live project, with its next action", parents=[common])
    p.add_argument("--all", action="store_true", help="include archived")
    p.set_defaults(fn=cmd_project_list)
    p = project.add_parser("status", help="set a project's status", parents=[common])
    p.add_argument("project")
    p.add_argument("status")
    p.set_defaults(fn=cmd_project_status)

    p = sub.add_parser("report", help="a week summary for Slack", parents=[common])
    p.add_argument("week", nargs="?")
    p.set_defaults(fn=cmd_report, group="report", action=None)

    p = sub.add_parser("verify", help="check generated files match the store", parents=[common])
    p.set_defaults(fn=cmd_verify, group="verify", action=None)

    p = sub.add_parser("regen", help="rewrite every generated view", parents=[common])
    p.set_defaults(fn=cmd_regen, group="regen", action=None)

    p = sub.add_parser("export", help="a readable copy that needs no SQLite", parents=[common])
    p.set_defaults(fn=cmd_export, group="export", action=None)

    p = sub.add_parser("docs", help="print this command reference as markdown", parents=[common])
    p.set_defaults(fn=cmd_docs, group="docs", action=None)

    p = sub.add_parser("check-skills", parents=[common],
                       help="verify mh commands quoted in .claude are real")
    p.add_argument("path", nargs="?", type=Path,
                   help="defaults to <repo>/.claude")
    p.set_defaults(fn=cmd_check_skills, group="check-skills", action=None)
    return ap


def main(argv=None):
    import os

    ap = build_parser()
    args = ap.parse_args(argv)

    # The reference must be readable before anything is set up, so it runs
    # before any check on the repo.
    if args.group == "docs":
        print(render_reference())
        return 0

    if args.group == "check-skills":
        import check_mh_usage
        repo = Path(args.repo or os.environ.get(
            "MELLONHEAD_ROOT", Path.home() / "Projects" / "mellonhead")).expanduser()
        return check_mh_usage.main([str(args.path or (repo / ".claude"))])

    repo = args.repo or os.environ.get(
        "MELLONHEAD_ROOT", Path.home() / "Projects" / "mellonhead")
    repo = Path(repo).expanduser()
    if not (repo / "operations").exists():
        print(f"no content repo at {repo}", file=sys.stderr)
        return 2

    # Never create a store as a side effect. open_store() would happily make
    # one wherever it is pointed, so a missing MELLONHEAD_ROOT silently wrote
    # an empty tasks.db into the live repo and regenerated its dashboard from
    # nothing. Refuse instead, and say where it looked.
    if not (repo / "operations" / "tasks.db").exists():
        print(f"no task store at {repo}/operations/tasks.db\n"
              f"Set MELLONHEAD_ROOT or pass --repo. To create one, run the "
              f"migration.", file=sys.stderr)
        return 2

    store = mhstore.open_store(root=repo, seed_settings=False)
    try:
        return args.fn(store, repo, args) or 0
    except (CliError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
