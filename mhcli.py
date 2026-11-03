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
    mh task dispatch aba-champions#37 --to iddy --expect path/to/draft.md
    mh question add aba-champions#37 "75 or 90 minutes?" --proposed 90 --blocks Anushka
    mh question answer 41 "run the 90"
    mh plan lock 2026-08-31
    mh verify

Every write regenerates the views it affects, so the markdown an agent reads
next is never behind the store. Pass --no-regen in a loop and run
`mh regen` once at the end.

Workstream 2.5. Stdlib only.
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import mhgen
import mhschedule
import mhsession
import mhstore
from mhstore import ONE_OFF, OPEN_STATUSES, StoreError

# "aba-academy#25", "#230" and "230" all address a task. The bare-hash form
# matters because that is what this tool prints for a row with no per-project
# number, and output you cannot paste back in is a trap.
# The # is required whenever a key is given. With it optional, a bare store
# id like 574 parsed as key "57", row 4, and failed as "no task 57#4".
IDENT = re.compile(r"^(?:([a-z0-9][a-z0-9-]*)?#)?(\d+)$")


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


def _question_id(value):
    """'41', 'Q41' and '`Q41`' all name question 41."""
    text = str(value).strip().strip("`")
    if text[:1].lower() == "q":
        text = text[1:]
    if not text.isdigit():
        raise argparse.ArgumentTypeError(f"{value!r} is not a question id (41 or Q41)")
    return int(text)


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


tag = mhstore.tag


def _iso_day(value):
    """YYYY-MM-DD, checked, so a typo cannot land as a date the scheduler reads."""
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a date (YYYY-MM-DD)")


def _day_or_none(value):
    return "none" if mhstore.clean(value) is None else _iso_day(value)


def _yes_no(value):
    text = str(value).strip().lower()
    if text in ("yes", "y", "true", "1", "on"):
        return True
    if text in ("no", "n", "false", "0", "off"):
        return False
    raise argparse.ArgumentTypeError(f"{value!r}: say yes or no")


def check_args(args):
    """
    Rules argparse cannot state, checked after parsing. main() runs this and
    so does check_mh_usage, so a skill line that breaks one fails the skill
    check rather than failing mid-task.
    """
    # Nothing yet. `mh task add` will require --load and --done-when here,
    # once the skills that call it pass both (see ai-workflows#18).
    return None


def show(store, task, prefix=""):
    bits = [f"{prefix}{tag(task):<22} {task['status']:<11} {task['title'][:56]}"]
    extra = []
    if task["owner"] != store.primary_user:
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


def show_readiness(store, task):
    """
    One more line under a row: whether it can be handed to an agent.

    Dispatch-ready is derived (a brief, and no open question that blocks
    anyone), so this is the only place it is ever printed.
    """
    open_qs = store.questions(task_id=task["id"])
    if not task["brief_path"] and not open_qs:
        return ""
    bits = []
    bits.append(f"brief {task['brief_path']}" if task["brief_path"] else "no brief")
    blocking = [q for q in open_qs if q["blocks"]]
    if blocking:
        bits.append("blocked by " + ", ".join(
            f"Q{q['id']} ({q['blocks']})" for q in blocking))
    elif open_qs:
        bits.append("open: " + ", ".join(f"Q{q['id']}" for q in open_qs))
    if store.dispatch_ready(task["id"]):
        bits.append("dispatch-ready")
    return "    " + "  ".join(bits)


def show_question(q, task=None):
    head = f"Q{q['id']:<5}"
    where = ""
    if task:
        where = f" [{tag(task)}]"
    elif q.get("project_key"):
        where = f" [{q['project_key']}]"
    line = f"{head}{where} {q['text']}"
    tail = []
    if q.get("proposed"):
        tail.append(f"Proposed: {q['proposed']}")
    if q.get("blocks"):
        tail.append(f"blocks {q['blocks']}")
    if q.get("status") == "answered":
        tail.append(f"answered by {q['answered_by']} {q['answered_at'][:10]}: {q['answer']}")
    elif q.get("status") == "withdrawn":
        tail.append("withdrawn")
    else:
        tail.append(f"asked by {q['asked_by']} {q['asked_at'][:10]}")
    if tail:
        line += "\n        " + "  ".join(tail)
    return line


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
            print(show(store, task, prefix="  "))
        return 0

    if not args.project:
        raise CliError("give a project key, or --all")
    task = store.next_task(args.project)
    if not task:
        print(f"{args.project}: nothing open")
        return 0
    print(show(store, task))
    ready = show_readiness(store, task)
    if ready:
        print(ready)
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
        print(show(store, task))
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
        owner=args.owner, notes=args.note, done_when=args.done_when,
        fixed=args.fixed, external_ready_date=args.ready)
    print(show(store, task))
    if not args.no_regen:
        regenerate(store, repo, [project])
    return 0


def cmd_task_done(store, repo, args):
    task = resolve(store, args.task)
    if task["status"] == "done":
        print(f"already done: {show(store, task)}")
        return 0
    store.complete_task(task["id"], actor=args.actor)
    print(show(store, store.task(task["id"])))
    nxt = store.next_task(task["project_key"])
    if nxt:
        print(f"\nnext in {task['project_key']}:")
        print(show(store, nxt, prefix="  "))
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
    print(show(store, store.task(task["id"])))
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
    # 'none' clears here as it does for every other mh field. Before, it was
    # stored as the day itself, and the dashboard could not parse it.
    if args.day and args.day.lower() != "none":
        store.plan_task(task["id"], args.day, actor=args.actor)
    else:
        store.unplan_task(task["id"], actor=args.actor)
    print(show(store, store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_task_load(store, repo, args):
    task = resolve(store, args.task)
    store.update_task(task["id"], actor=args.actor,
                      load=None if args.load == "none" else args.load)
    print(show(store, store.task(task["id"])))
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


def cmd_task_set(store, repo, args):
    """
    Change the fields that have no verb of their own.

    Retitling, fixing a note, setting a due date or a section were being
    done with sqlite3 by hand, outside the audit log. Status, day, load,
    owner and seq keep their verbs because each carries a rule; these are
    plain fields.
    """
    task = resolve(store, args.task)
    fields = {}
    if args.title is not None:
        title = args.title.strip()
        if not title:
            raise CliError("a title cannot be empty")
        fields["title"] = title
    if args.notes is not None:
        fields["notes"] = args.notes.strip() or None
    if args.due is not None:
        fields["due"] = _blank_is_none(args.due)
    if args.section is not None:
        fields["section"] = _blank_is_none(args.section)
    if args.notion is not None:
        fields["notion_task_id"] = _blank_is_none(args.notion)
    if args.depends_on is not None:
        # The single-predecessor, zero-gap case of `mh dep add`, kept for the
        # skills that already say it. It replaces every other predecessor.
        if _blank_is_none(args.depends_on) is None:
            fields["depends_on"] = None
        else:
            fields["depends_on"] = resolve(store, args.depends_on)["id"]
    if args.done_when is not None:
        fields["done_when"] = _blank_is_none(args.done_when)
    if args.fixed is not None:
        fields["fixed"] = args.fixed
    if args.ready is not None:
        fields["external_ready_date"] = None if args.ready == "none" else args.ready
    if not fields:
        raise CliError("nothing to set; see `mh task set --help`")
    store.update_task(task["id"], actor=args.actor, **fields)
    print(show(store, store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def _blank_is_none(value):
    """'none', '-' or '' clears a field; anything else is the value."""
    return mhstore.clean(value)


def cmd_task_owner(store, repo, args):
    """
    Hand a task to someone, or take it back.

    Owner is who does the work, not who is waiting: a row owned by Anushka
    stays off the primary user's next-action list (next_task only returns
    their rows) and renders under her name in the task list. The primary
    user's aliases fold to their id.
    """
    task = resolve(store, args.task)
    owner = store.normalize_owner(args.owner)
    if not owner:
        raise CliError("owner needs a name")
    if task["owner"] == owner:
        print(f"already owned by {owner}: {show(store, task)}")
        return 0
    store.update_task(task["id"], actor=args.actor, owner=owner)
    print(show(store, store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_task_seq(store, repo, args):
    task = resolve(store, args.task)
    store.update_task(task["id"], actor=args.actor, seq=args.seq, is_next=0)
    print(show(store, store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_task_confirm(store, repo, args):
    task = resolve(store, args.task)
    store.confirm_task(task["id"], actor=args.actor)
    print(show(store, store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_task_brief(store, repo, args):
    """
    Record where the brief is. Validated, because one row in 291 carried a
    brief_path a month after the verb shipped and a path that does not
    resolve would make the task page's "Open brief" a dead button.
    """
    task = resolve(store, args.task)
    rel = _repo_relative(repo, args.path)
    if not (repo / rel).is_file():
        raise CliError(f"no such file under the repo: {rel}")
    store.update_task(task["id"], actor=args.actor, brief_path=rel)
    print(show(store, store.task(task["id"])))
    print(show_readiness(store, store.task(task["id"])))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def _repo_relative(repo, path):
    """
    A path as the store keeps it: relative to the repo, no leading ./.

    Absolute paths inside the repo are accepted and trimmed; anything that
    escapes the repo is refused, since artifact links open in Obsidian by
    joining to the repo root.
    """
    raw = Path(str(path).strip().strip("`"))
    if raw.is_absolute():
        try:
            return str(raw.resolve().relative_to(repo.resolve()))
        except ValueError:
            raise CliError(f"{raw} is outside the repo {repo}")
    text = str(raw)
    if text.startswith("./"):
        text = text[2:]
    if text.startswith("../") or "/../" in text:
        raise CliError(f"{text} escapes the repo")
    return text


def _artifact(repo, path):
    """An artifact path for an event: relative, but not required to exist yet."""
    return _repo_relative(repo, path) if path else None


# ---------------------------------------------------------------------------
# task: the agent record (dispatch, deliver, review, link)
# ---------------------------------------------------------------------------

def cmd_task_dispatch(store, repo, args):
    task = resolve(store, args.task)
    event = store.dispatch(task["id"], args.to, expect=_artifact(repo, args.expect),
                           actor=args.actor, summary=args.summary)
    print(f"{tag(task)}  {event['summary']}")
    open_qs = store.blocking_questions(task["id"])
    if open_qs:
        print("  note: still blocked by " + ", ".join(
            f"Q{q['id']} ({q['blocks']})" for q in open_qs))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]], quiet=True)
    return 0


def cmd_task_deliver(store, repo, args):
    task = resolve(store, args.task)
    event = store.deliver(task["id"], _artifact(repo, args.artifact),
                          actor=args.actor, agent=args.agent or None,
                          summary=args.summary)
    closed = json.loads(event["payload"]).get("closes")
    print(f"{tag(task)}  {event['summary']}"
          + (f"  (closes dispatch #{closed})" if closed else ""))
    still = store.open_dispatches(task["id"])
    if still:
        print("  still out: " + ", ".join(
            f"{d['agent']}" + (f" → {d['artifact_path']}" if d['artifact_path'] else "")
            for d in still))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]], quiet=True)
    return 0


def cmd_task_review(store, repo, args):
    task = resolve(store, args.task)
    event = store.review(task["id"], args.verdict,
                         findings=_artifact(repo, args.findings),
                         actor=args.actor, agent=args.agent or None,
                         summary=args.summary)
    print(f"{tag(task)}  {event['summary']}"
          + (f"  findings {event['artifact_path']}" if event["artifact_path"] else ""))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]], quiet=True)
    return 0


def cmd_task_link(store, repo, args):
    task = resolve(store, args.task)
    info = store.session
    event = store.link_session(task["id"], actor=args.actor)
    print(f"{tag(task)}  {event['summary']}")
    print("  " + mhsession.describe(info))
    return 0


def cmd_task_unlink(store, repo, args):
    task = resolve(store, args.task)
    event = store.unlink_session(task["id"], actor=args.actor)
    print(f"{tag(task)}  {event['summary']}")
    return 0


# ---------------------------------------------------------------------------
# question
# ---------------------------------------------------------------------------

def resolve_scope(store, ident):
    """
    'aba-champions#37' -> (task, project_key); 'aba-champions' -> (None, key).

    A project key is checked first: a key that happens to end in digits
    would otherwise be read as key#N.
    """
    text = str(ident).strip().strip("`")
    if store.project(text):
        return None, text
    task = resolve(store, text)
    return task, task["project_key"]


def cmd_question_add(store, repo, args):
    task, project_key = resolve_scope(store, args.scope)
    try:
        q = store.add_question(args.text, actor=args.actor,
                               task_id=task["id"] if task else None,
                               project_key=project_key,
                               proposed=args.proposed, blocks=args.blocks)
    except StoreError as exc:
        if "already open as" in str(exc):
            # Not an error for the caller: the question exists, use its id.
            print(str(exc))
            return 0
        raise
    print(show_question(q, task))
    if not q["proposed"]:
        print("  (no proposed answer: a question without one is a research "
              "task, not a question)")
    if not args.no_regen:
        regenerate(store, repo, [project_key], quiet=True)
    return 0


def _print_next_open(store, q):
    if q["task_id"] is None:
        return
    remaining = store.questions(task_id=q["task_id"])
    if remaining:
        print(f"\nnext open on this task:")
        print(show_question(remaining[0]))


def cmd_question_answer(store, repo, args):
    q = store.answer_question(args.id, args.answer, actor=args.actor,
                              source=args.source)
    task = store.task(q["task_id"]) if q["task_id"] else None
    print(show_question(q, task))
    _print_next_open(store, q)
    if not args.no_regen:
        regenerate(store, repo, [q["project_key"]], quiet=True)
    return 0


def cmd_question_accept(store, repo, args):
    q = store.answer_question(args.id, actor=args.actor, accept=True,
                              source=args.source)
    task = store.task(q["task_id"]) if q["task_id"] else None
    print(show_question(q, task))
    _print_next_open(store, q)
    if not args.no_regen:
        regenerate(store, repo, [q["project_key"]], quiet=True)
    return 0


def cmd_question_withdraw(store, repo, args):
    q = store.withdraw_question(args.id, actor=args.actor, reason=args.reason)
    task = store.task(q["task_id"]) if q["task_id"] else None
    print(show_question(q, task))
    if not args.no_regen:
        regenerate(store, repo, [q["project_key"]], quiet=True)
    return 0


def cmd_question_list(store, repo, args):
    task = project_key = None
    if args.scope:
        task, project_key = resolve_scope(store, args.scope)
    status = None if args.all else "open"
    if task:
        rows = store.questions(task_id=task["id"], status=status)
    elif project_key:
        rows = store.questions(project_key=project_key, status=status)
    else:
        rows = store.questions(status=status)
    if args.json:
        out = []
        for q in rows:
            item = dict(q)
            t = store.task(q["task_id"]) if q["task_id"] else None
            item["task"] = tag(t) if t else None
            item["task_title"] = t["title"] if t else None
            item["planned_day"] = t["planned_day"] if t else None
            out.append(item)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("no open questions" if status == "open" else "no questions")
        return 0
    for q in rows:
        t = store.task(q["task_id"]) if q["task_id"] else None
        print(show_question(q, t))
    return 0


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------

def cmd_session_show(store, repo, args):
    info = store.session
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    print(mhsession.describe(info))
    if info.get("session_id"):
        row = store.session_row(info["session_id"])
        print("known to the store" if row else "not yet in the store "
              "(the first write records it)")
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
    if args.json:
        out = []
        for task in rows:
            item = dict(task)
            item["tag"] = tag(task)
            item["dispatch_ready"] = store.dispatch_ready(task["id"])
            item["open_questions"] = store.questions(task_id=task["id"])
            item["open_dispatches"] = store.open_dispatches(task["id"])
            item["last_review"] = store.last_review(task["id"])
            out.append(item)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    for task in rows:
        print(show(store, task))
        ready = show_readiness(store, task)
        if ready:
            print(ready)
    return 0


# ---------------------------------------------------------------------------
# dep
# ---------------------------------------------------------------------------

def _show_dep(store, dep, direction):
    """One line: 'waits on X' from the dependent's side, 'blocks Y' from the other."""
    other = store.task(dep["depends_on_id"] if direction == "waits on" else dep["task_id"])
    how = "same day as" if dep["kind"] == "same_day" else direction
    gap = f"  (+{dep['min_gap_days']}d)" if dep["min_gap_days"] else ""
    return (f"    {how:<12} {tag(other):<22} {other['status']:<11} "
            f"{other['planned_day'] or '—':<10} {other['title'][:40]}{gap}")


def cmd_dep_add(store, repo, args):
    task, pred = resolve(store, args.task), resolve(store, args.on)
    kind = "same_day" if args.same_day else "finish_to_start"
    if args.same_day and args.gap:
        raise CliError("--same-day and --gap contradict each other")
    dep = store.add_dep(task["id"], pred["id"], actor=args.actor,
                        gap=args.gap or 0, kind=kind)
    print(show(store, task))
    print(_show_dep(store, dep, "waits on"))
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_dep_rm(store, repo, args):
    task, pred = resolve(store, args.task), resolve(store, args.on)
    if not store.remove_dep(task["id"], pred["id"], actor=args.actor):
        raise CliError(f"{tag(task)} does not depend on {tag(pred)}")
    print(f"{tag(task)} no longer waits on {tag(pred)}")
    if not args.no_regen:
        regenerate(store, repo, [task["project_key"]])
    return 0


def cmd_dep_list(store, repo, args):
    """Both directions: what each row waits on, and what it blocks."""
    target = args.target.strip().strip("`")
    if store.project(target):
        rows = [t for t in store.tasks(project_key=target)
                if store.deps(task_id=t["id"]) or store.deps(depends_on_id=t["id"])]
    else:
        rows = [resolve(store, target)]
    if args.json:
        print(json.dumps([{"tag": tag(t), "id": t["id"],
                           "waits_on": [{**d, "tag": tag(store.task(d["depends_on_id"]))}
                                        for d in store.deps(task_id=t["id"])],
                           "blocks": [{**d, "tag": tag(store.task(d["task_id"]))}
                                      for d in store.deps(depends_on_id=t["id"])]}
                          for t in rows], indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("no dependencies")
        return 0
    for t in rows:
        print(show(store, t))
        for d in store.deps(task_id=t["id"]):
            print(_show_dep(store, d, "waits on"))
        for d in store.deps(depends_on_id=t["id"]):
            print(_show_dep(store, d, "blocks"))
    return 0


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------

def cmd_schedule_validate(store, repo, args):
    """
    Hold the committed schedule to the rules. Read-only: writes nothing, not
    even weeks.rules_report, so it is safe against any store. Exits 1 when
    something is broken, like `mh verify`.
    """
    try:
        found = mhschedule.validate(store, start=args.start)
    except mhschedule.RulesError as exc:
        raise CliError(str(exc))
    if args.project:
        ids = {t["id"] for t in store.tasks(project_key=args.project)}
        found = [f for f in found if ids & set(f["tasks"])]
    if args.json:
        print(json.dumps(found, indent=2, ensure_ascii=False))
        return 1 if mhschedule.violations(found) else 0

    titles = dict(mhschedule.RULES)
    for rule in titles:
        mine = [f for f in found if f["rule"] == rule]
        if not mine:
            continue
        bad = sum(1 for f in mine if f["level"] == "violation")
        head = f"{bad} violation(s)" if bad else "notice"
        print(f"\n{rule}: {titles[rule]}  [{head}]")
        for f in mine:
            mark = "!" if f["level"] == "violation" else "·"
            print(f"  {mark} {f['message']}")
            if rule in ("day_cap", "week") and f["level"] == "violation":
                print(f"      {', '.join(f['tags'])}")
            if rule == "backlog":
                for key, v in f["by_project"].items():
                    print(f"      {key:<20} {v['rows']:>3} rows  {v['days']:>5.2f} days")
    n = len(mhschedule.violations(found))
    print(f"\n{n} violation(s)" if n else "\nno violations")
    return 1 if n else 0


def cmd_schedule_rules(store, repo, args):
    try:
        current = mhschedule.rules(store)
    except mhschedule.RulesError as exc:
        raise CliError(str(exc))
    if args.json:
        print(json.dumps(current, indent=2))
        return 0
    for name, rule in current.items():
        print(f"{name:<16} {json.dumps(rule['value']):<60} {rule['source']}")
    print("\nChange one with `mh schedule set <name> <value>`. Project lanes: "
          "`mh project set <project> --lane aba|business|any|auto`.")
    return 0


def cmd_schedule_set(store, repo, args):
    try:
        value = mhschedule.set_rule(store, args.name, args.value, actor=args.actor)
    except mhschedule.RulesError as exc:
        raise CliError(str(exc))
    print(f"{args.name} = {json.dumps(value)}")
    return 0


# ---------------------------------------------------------------------------
# template
# ---------------------------------------------------------------------------

def cmd_template_list(store, repo, args):
    for name, t in mhschedule.TEMPLATES.items():
        print(f"{name:<12} {t['about']}")
    return 0


def cmd_template_show(store, repo, args):
    """The stages, their loads, and what each waits on."""
    try:
        stages = mhschedule.template(args.name, args.self_paced)
    except mhschedule.RulesError as exc:
        raise CliError(str(exc))
    for st in stages:
        waits = ", ".join(f"{n}" + (f" +{g}d" if g else "") for n, g in st["after"])
        extra = []
        if waits:
            extra.append(f"after {waits}")
        if st["same_day"]:
            extra.append("same day as " + ", ".join(map(str, st["same_day"])))
        if st["fixed"]:
            extra.append("fixed")
        print(f"{st['n']:>3}  {st['name']:<44} {st['load']:<8} {'; '.join(extra)}")
    return 0


def _stage_overrides(store, values):
    """--stage 6=aba-academy#44,aba-academy#45 -> {6: [row, row]}."""
    out = {}
    for value in values or ():
        n, sep, idents = value.partition("=")
        if not sep or not n.strip().isdigit():
            raise CliError(f"--stage takes N=key#id[,key#id], not {value!r}")
        out[int(n)] = [resolve(store, i) for i in idents.split(",") if i.strip()]
    return out


def cmd_template_apply(store, repo, args):
    if not store.project(args.project):
        raise CliError(f"no project {args.project!r}; see `mh project list`")
    try:
        rows, n_deps = mhschedule.apply_template(
            store, args.name, args.project, args.prefix, actor=args.actor,
            self_paced=args.self_paced, owner=args.owner)
    except mhschedule.RulesError as exc:
        raise CliError(str(exc))
    for n, made in rows.items():
        print(show(store, made[0]))
    print(f"\n{len(rows)} rows, {n_deps} dependencies; none planned. "
          f"Place them, then `mh schedule validate --project {args.project}`.")
    if not args.no_regen:
        regenerate(store, repo, [args.project])
    return 0


def cmd_template_link(store, repo, args):
    """Wire an in-flight course's existing rows to the template. Creates nothing."""
    overrides = _stage_overrides(store, args.stage)
    try:
        rows, missing, n_deps = mhschedule.link_template(
            store, args.name, args.project, args.prefix, overrides=overrides,
            actor=args.actor, self_paced=args.self_paced)
    except mhschedule.RulesError as exc:
        raise CliError(str(exc))
    for n in sorted(rows):
        print(f"stage {n:>2}: " + ", ".join(
            f"{tag(t)} ({t['status']}{', ' + t['planned_day'] if t['planned_day'] else ''})"
            for t in rows[n]))
    if missing:
        print(f"no row for stage {', '.join(map(str, missing))}: bridged, so "
              "what follows waits on what came before. Pass --stage N=key#id "
              "for a row whose title does not say its stage.")
    print(f"{n_deps} dependencies wired")
    if not args.no_regen:
        regenerate(store, repo, [args.project])
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
        owner=args.owner, due=args.due,
        notion_project_id=args.notion, goal=args.goal)
    print(f"{project['key']:<18} {project['status']:<9} {project['name']}")
    if project["dir"]:
        print(f"  dir: {project['dir']}")
        # The generator only rewrites tables it finds, so a project with no
        # list would never get one. Seed an empty table here, the one place
        # that knows the project is new, never on a regen.
        task_list = repo / project["dir"] / "task-list.md"
        if not task_list.exists():
            mhgen.atomic_write(task_list, "\n".join((
                f"# {project['name']}", "", "## Tasks", "",
                mhgen.TASK_TABLE_HEADER, mhgen.TASK_TABLE_DIVIDER, "")))
            print(f"  created {project['dir']}/task-list.md")
        if (repo / "operations" / "project-registry.md").exists():
            print("  add a row to operations/project-registry.md so the "
                  "migration sees it too.")
    return 0


def cmd_project_set(store, repo, args):
    """
    Rename a project, move its directory, or fix its due, goal or owner.

    A moved directory needs the task list to move with it: the generator
    writes to <dir>/task-list.md and does nothing if the file is not there.
    project-registry.md is hand-written and is reminded about, not edited.
    """
    if not store.project(args.project):
        raise CliError(f"no project {args.project!r}; see `mh project list`")
    fields = {}
    if args.name is not None:
        name = args.name.strip()
        if not name:
            raise CliError("a name cannot be empty")
        fields["name"] = name
    if args.dir is not None:
        rel = _repo_relative(repo, args.dir)
        if not (repo / rel).is_dir():
            raise CliError(f"no such directory under the repo: {rel}")
        fields["dir"] = rel
    if args.due is not None:
        fields["due"] = _blank_is_none(args.due)
    if args.goal is not None:
        fields["goal"] = _blank_is_none(args.goal)
    if args.owner is not None:
        fields["owner"] = store.normalize_owner(args.owner)
    if args.notion is not None:
        fields["notion_project_id"] = _blank_is_none(args.notion)
    if args.lane is not None:
        fields["lane"] = None if args.lane == "auto" else args.lane
    if not fields:
        raise CliError("nothing to set; see `mh project set --help`")
    project = store.update_project(args.project, actor=args.actor, **fields)
    print(f"{project['key']:<18} {project['status']:<9} {project['name']}")
    if project["dir"]:
        print(f"  dir: {project['dir']}")
    if "dir" in fields and not (repo / project["dir"] / "task-list.md").exists():
        print(f"  note: no task-list.md at {project['dir']}; move the old one "
              "there or the list will not regenerate.")
    if "dir" in fields or "name" in fields:
        print("  update the row in operations/project-registry.md too; it is "
              "hand-written.")
    if not args.no_regen:
        regenerate(store, repo, [args.project])
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
            if task["owner"] != store.primary_user:
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


def cmd_init(repo, args):
    """
    Create an empty store, or rename the primary user of an existing one.

    The only verb that may create tasks.db. Everything else refuses without
    one, so a mistyped MELLONHEAD_ROOT cannot conjure an empty store.
    """
    db = repo / "operations" / "tasks.db"
    fresh = not db.exists()
    if not fresh and not args.user:
        raise CliError(f"a store already exists at {db}; pass --user to "
                       f"rename its primary user")
    user = mhstore.check_user_id(args.user or os.environ.get("USER", ""))
    store = mhstore.open_store(root=repo, primary_user=user)
    try:
        store.set_primary_user(user, name=args.name, aliases=args.alias or (),
                               actor=args.actor if args.actor != "agent"
                               else user)
        if not args.no_regen:
            mhgen.generate_all(store, repo)
        verb = "created" if fresh else "updated"
        print(f"{verb} {db}")
        print(f"  primary user: {store.primary_user} "
              f"(shown as {store.primary_name})")
        if store.owner_aliases():
            print(f"  also folds:   {', '.join(store.owner_aliases())}")
        if fresh:
            print("next: mh project add <key> \"<name>\" --repo "
                  f"{repo}")
    finally:
        store.close()
    return 0


def render_reference(user=mhstore.LEGACY_PRIMARY_USER,
                     name=mhstore.LEGACY_PRIMARY_NAME):
    """
    The command table, built from the parser itself.

    Hand-written docs drift from the code and nobody notices until an agent
    runs a command that no longer exists. This is generated, and a test
    asserts the committed file still matches.

    The prose is written about MQ, whose store this started as. For any
    other primary user the names are swapped on the way out, so an agent in
    someone else's repo is told whose questions it is asking.
    """
    text = _render_reference()
    if (user, name) == (mhstore.LEGACY_PRIMARY_USER,
                        mhstore.LEGACY_PRIMARY_NAME):
        return text
    text = re.sub(r"\bMQ\b", name, text)
    return re.sub(r"\bmq\b", user, text)


def _render_reference():
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
4. **Every write records which conversation made it.** `mh` finds the
   Claude session it runs inside on its own; nothing to pass. `--actor` is
   still yours to give.

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

Moving the row off `waiting` later clears the reason automatically. For
something that waits on **MQ**, ask a question instead (below): a task
status cannot be answered, a question can.

**Fix a task's title, note, due date or section**

```
mh task set aba-champions#37 --title "Build the first agent: session plan"
mh task set aba-champions#37 --due 2026-09-30 --notes "Anushka co-leads."
mh task set aba-champions#37 --depends-on aba-champions#36
```

`none` clears a field (`--due none`). Status, day, load, owner and seq
keep their own verbs. **Never edit `tasks.db` with sqlite3**: a change made
that way has no audit line, no event, and no regeneration.

**Rename a project or move its directory**

```
mh project set gamma-event --name "LA Tech Week CHRO roundtable"
mh project set gamma-event --dir marketing/2026-10-chro-roundtable
```

The directory must exist and hold the project's `task-list.md`; the
registry row in `operations/project-registry.md` is hand-written and is
yours to update.

**Hand a task to someone else**

```
mh task owner aba-champions#37 anushka --actor orca
mh task owner aba-champions#37 mq                    # take it back
```

Owner is who does the work. A row owned by someone else leaves MQ's
next-action list and renders under their name; `waiting --waiting-on` is
for a row MQ still owns that is blocked on them.

**Ask MQ something, and propose the answer**

```
mh question add aba-champions#37 "Hold the VILT at 75 minutes, or run the 90?" \
    --proposed "90" --blocks "Anushka" --actor orca
```

Prints `Q41`. Always give `--proposed`: a question without a default answer
is a research task, not a question, and the cheapest reply MQ can give is
Accept. `--blocks` names who is waiting; a question from a person creates
an obligation, a question from an agent does not. A near-duplicate of an
open question on the same task is refused and the existing id printed.

MQ answers in the session manager, or by id from anywhere:

```
mh question accept 41                              # answer = proposed
mh question answer 41 "run the 90" --source slack  # her own words
mh question list aba-champions#37                  # what is still open
mh question list --open --json                     # for the Friday job
```

**Record what an agent did**

```
mh task dispatch aba-champions#37 --to iddy --expect path/to/draft.md --actor orca
mh task deliver  aba-champions#37 --artifact path/to/draft.md --actor iddy
mh task review   aba-champions#37 --verdict back --findings path/to/findings.md --actor revi
```

Each is one event on the task page. `deliver` closes the newest open
dispatch for the same agent; when Orca records on a subagent's behalf, pass
`--agent <name>`. A task is **dispatch-ready** when it has a brief and no
open question that blocks anyone; nothing sets that by hand.

**Say this conversation is about a task**

```
mh task link aba-champions#37 --actor orca
```

Run it as the first thing when you start work on a task. The task page
lists a conversation only when it has been linked: every `mh` write records
which conversation made it (see `mh session show`), but writing to a task
is not the same as being about it, and a capture sweep writes to many. The
linked conversation is also what "Work on this with Orca" resumes.
`mh task unlink` takes it back.

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
    p.add_argument("--done-when", help="what finished means, one sentence")
    p.add_argument("--fixed", action="store_true",
                   help="cannot move: a session or a delivery")
    p.add_argument("--ready", type=_iso_day,
                   help="YYYY-MM-DD the other party delivers what this reviews")
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
    p.add_argument("day", nargs="?", help="YYYY-MM-DD; omit or 'none' to unschedule")
    p.set_defaults(fn=cmd_task_plan)

    p = task.add_parser("load", help="set a task's load tag", parents=[common])
    p.add_argument("task")
    p.add_argument("load", choices=(*mhstore.LOADS, "none"))
    p.set_defaults(fn=cmd_task_load)

    p = task.add_parser("set", help="change a task's title, notes, due, section, dependency or Notion id", parents=[common])
    p.add_argument("task")
    p.add_argument("--title", help="the new title")
    p.add_argument("--notes", help="one sentence, rendered inline; 'none' clears")
    p.add_argument("--due", help="YYYY-MM-DD; 'none' clears")
    p.add_argument("--section", help="the sub-heading its table sits under; 'none' clears")
    p.add_argument("--depends-on", help="key#N it waits for; 'none' clears")
    p.add_argument("--notion", help="Notion task id; 'none' clears")
    p.add_argument("--done-when", help="what finished means; 'none' clears")
    p.add_argument("--fixed", type=_yes_no, help="yes or no: cannot move")
    p.add_argument("--ready", type=_day_or_none,
                   help="YYYY-MM-DD the other party delivers; 'none' clears")
    p.set_defaults(fn=cmd_task_set)

    p = task.add_parser("owner", help="hand a task to someone (mq, or a person's name)", parents=[common])
    p.add_argument("task")
    p.add_argument("owner", help="who does the work; MQ and Mariena fold to mq")
    p.set_defaults(fn=cmd_task_owner)

    p = task.add_parser("seq", help="set ordering within a project", parents=[common])
    p.add_argument("task")
    p.add_argument("seq", type=int)
    p.set_defaults(fn=cmd_task_seq)

    p = task.add_parser("confirm", help="accept a capture proposal", parents=[common])
    p.add_argument("task")
    p.set_defaults(fn=cmd_task_confirm)

    p = task.add_parser("brief", help="record a brief file path (must exist, under the repo)", parents=[common])
    p.add_argument("task")
    p.add_argument("path")
    p.set_defaults(fn=cmd_task_brief)

    p = task.add_parser("dispatch", help="record that work went to an agent", parents=[common])
    p.add_argument("task")
    p.add_argument("--to", required=True, help="the agent it went to")
    p.add_argument("--expect", help="repo-relative path the deliverable should land at")
    p.add_argument("--summary", help="one line for the task page")
    p.set_defaults(fn=cmd_task_dispatch)

    p = task.add_parser("deliver", help="record a deliverable; closes the open dispatch", parents=[common])
    p.add_argument("task")
    p.add_argument("--artifact", required=True, help="repo-relative path of what was delivered")
    p.add_argument("--agent", help="who did the work, when recording for a subagent")
    p.add_argument("--summary", help="one line for the task page")
    p.set_defaults(fn=cmd_task_deliver)

    p = task.add_parser("review", help="record a reviewer's verdict", parents=[common])
    p.add_argument("task")
    p.add_argument("--verdict", required=True, choices=mhstore.VERDICTS)
    p.add_argument("--findings", help="repo-relative path of the findings")
    p.add_argument("--agent", help="which reviewer, when recording for a subagent")
    p.add_argument("--summary", help="one line for the task page")
    p.set_defaults(fn=cmd_task_review)

    p = task.add_parser("link", help="this conversation is about the task; run it when you start work", parents=[common])
    p.add_argument("task")
    p.set_defaults(fn=cmd_task_link)

    p = task.add_parser("unlink", help="this conversation is no longer about the task", parents=[common])
    p.add_argument("task")
    p.set_defaults(fn=cmd_task_unlink)

    question = sub.add_parser("question", help="questions for MQ",
                              parents=[common]).add_subparsers(
        dest="action", required=True)

    p = question.add_parser("add", help="ask MQ something; always propose an answer", parents=[common])
    p.add_argument("scope", help="key#N for a task, or a project key")
    p.add_argument("text")
    p.add_argument("--proposed", help="your default answer, so Accept is one click")
    p.add_argument("--blocks", help="who or what waits on this (a person, a session)")
    p.set_defaults(fn=cmd_question_add)

    p = question.add_parser("answer", help="record MQ's answer", parents=[common])
    p.add_argument("id", type=_question_id, help="the question id, 41 or Q41")
    p.add_argument("answer")
    p.add_argument("--source", help="where the answer came from: dashboard, slack, context.md, a path")
    p.set_defaults(fn=cmd_question_answer)

    p = question.add_parser("accept", help="answer = the proposed answer", parents=[common])
    p.add_argument("id", type=_question_id)
    p.add_argument("--source")
    p.set_defaults(fn=cmd_question_accept)

    p = question.add_parser("withdraw", help="the question no longer applies", parents=[common])
    p.add_argument("id", type=_question_id)
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_question_withdraw)

    p = question.add_parser("list", help="open questions, in the order the dashboard shows them", parents=[common])
    p.add_argument("scope", nargs="?", help="key#N or a project key; omit for all")
    p.add_argument("--open", action="store_true", help="open only (the default)")
    p.add_argument("--all", action="store_true", help="include answered and withdrawn")
    p.set_defaults(fn=cmd_question_list)

    session = sub.add_parser("session", help="the conversation mh is running in",
                             parents=[common]).add_subparsers(
        dest="action", required=True)
    p = session.add_parser("show", help="what the store thinks this conversation is", parents=[common])
    p.set_defaults(fn=cmd_session_show)

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
    p = project.add_parser("set", help="rename a project, move its directory, or fix due, goal, owner", parents=[common])
    p.add_argument("project")
    p.add_argument("--name")
    p.add_argument("--dir", help="repo-relative directory holding its task-list.md")
    p.add_argument("--due", help="YYYY-MM-DD; 'none' clears")
    p.add_argument("--goal", help="'none' clears")
    p.add_argument("--owner")
    p.add_argument("--notion", help="Notion project id; 'none' clears")
    p.add_argument("--lane", choices=(*mhstore.LANES, "auto"),
                   help="which days its work belongs on; auto infers from the key")
    p.set_defaults(fn=cmd_project_set)

    p = project.add_parser("status", help="set a project's status", parents=[common])
    p.add_argument("project")
    p.add_argument("status")
    p.set_defaults(fn=cmd_project_status)

    dep = sub.add_parser("dep", help="dependencies between tasks",
                         parents=[common]).add_subparsers(dest="action", required=True)

    p = dep.add_parser("add", help="a task waits on another, by at least --gap days", parents=[common])
    p.add_argument("task")
    p.add_argument("--on", required=True, help="the key#N it waits on")
    p.add_argument("--gap", type=int, default=0, help="minimum calendar days between them")
    p.add_argument("--same-day", action="store_true",
                   help="the two land on one day instead (the Sharla gate)")
    p.set_defaults(fn=cmd_dep_add)

    p = dep.add_parser("rm", help="remove a dependency", parents=[common])
    p.add_argument("task")
    p.add_argument("--on", required=True)
    p.set_defaults(fn=cmd_dep_rm)

    p = dep.add_parser("list", help="what a task or project waits on, and blocks", parents=[common])
    p.add_argument("target", help="key#N or a project key")
    p.set_defaults(fn=cmd_dep_list)

    schedule = sub.add_parser("schedule", help="scheduling rules and the check against them",
                              parents=[common]).add_subparsers(dest="action", required=True)

    p = schedule.add_parser("validate", help="every rule the committed schedule breaks; read-only", parents=[common])
    p.add_argument("--project", help="only findings touching this project")
    p.add_argument("--from", dest="start", type=_iso_day,
                   help="check capacity from this day (default today)")
    p.set_defaults(fn=cmd_schedule_validate)

    p = schedule.add_parser("rules", help="the rules in force, and where each comes from", parents=[common])
    p.set_defaults(fn=cmd_schedule_rules)

    p = schedule.add_parser("set", help="change a rule: day_cap, week_target, days...", parents=[common])
    p.add_argument("name", choices=tuple(mhschedule.DEFAULTS))
    p.add_argument("value", help="a number, a load, or JSON")
    p.set_defaults(fn=cmd_schedule_set)

    tmpl = sub.add_parser("template", help="pipeline templates: course, champions",
                          parents=[common]).add_subparsers(dest="action", required=True)

    p = tmpl.add_parser("list", help="the templates there are", parents=[common])
    p.set_defaults(fn=cmd_template_list)

    p = tmpl.add_parser("show", help="a template's stages and what each waits on", parents=[common])
    p.add_argument("name", choices=tuple(mhschedule.TEMPLATES))
    p.add_argument("--self-paced", action="store_true", help="the variant with no VILT")
    p.set_defaults(fn=cmd_template_show)

    p = tmpl.add_parser("apply", help="create every stage row and its dependencies", parents=[common])
    p.add_argument("name", choices=tuple(mhschedule.TEMPLATES))
    p.add_argument("project")
    p.add_argument("--prefix", required=True, help="the course or session name, e.g. \"Creating Documents\"")
    p.add_argument("--self-paced", action="store_true", help="drop the guide, slides, QRG reviews and the VILT")
    p.add_argument("--owner")
    p.set_defaults(fn=cmd_template_apply)

    p = tmpl.add_parser("link", help="wire an in-flight course's existing rows to the template", parents=[common])
    p.add_argument("name", choices=tuple(mhschedule.TEMPLATES))
    p.add_argument("project")
    p.add_argument("--prefix", required=True, help="the title prefix its rows share")
    p.add_argument("--stage", action="append", metavar="N=KEY#ID",
                   help="a row whose title does not say its stage; repeatable")
    p.add_argument("--self-paced", action="store_true")
    p.set_defaults(fn=cmd_template_link)

    p = sub.add_parser("report", help="a week summary for Slack", parents=[common])
    p.add_argument("week", nargs="?")
    p.set_defaults(fn=cmd_report, group="report", action=None)

    p = sub.add_parser("verify", help="check generated files match the store", parents=[common])
    p.set_defaults(fn=cmd_verify, group="verify", action=None)

    p = sub.add_parser("regen", help="rewrite every generated view", parents=[common])
    p.set_defaults(fn=cmd_regen, group="regen", action=None)

    p = sub.add_parser("export", help="a readable copy that needs no SQLite", parents=[common])
    p.set_defaults(fn=cmd_export, group="export", action=None)

    p = sub.add_parser("init", help="create an empty store, or rename its primary user", parents=[common])
    p.add_argument("--user", help="the primary user's id: lower case, no spaces (default $USER)")
    p.add_argument("--name", help="how the user is shown in markdown and on the dashboard")
    p.add_argument("--alias", action="append",
                   help="another spelling that folds to --user as an owner; repeatable")
    p.set_defaults(fn=cmd_init, group="init", action=None)

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
        print(render_reference(*_reference_user(args)))
        return 0

    if args.group == "check-skills":
        import check_mh_usage
        repo = Path(args.repo or os.environ.get(
            "MELLONHEAD_ROOT", Path.home() / "Projects" / "mellonhead")).expanduser()
        return check_mh_usage.main([str(args.path or (repo / ".claude"))])

    repo = args.repo or os.environ.get(
        "MELLONHEAD_ROOT", Path.home() / "Projects" / "mellonhead")
    repo = Path(repo).expanduser()
    if args.group == "init":
        try:
            return cmd_init(repo, args)
        except (CliError, StoreError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if not (repo / "operations").exists():
        print(f"no content repo at {repo}", file=sys.stderr)
        return 2

    # Never create a store as a side effect. open_store() would happily make
    # one wherever it is pointed, so a missing MELLONHEAD_ROOT silently wrote
    # an empty tasks.db into the live repo and regenerated its dashboard from
    # nothing. Refuse instead, and say where it looked.
    if not (repo / "operations" / "tasks.db").exists():
        print(f"no task store at {repo}/operations/tasks.db\n"
              f"Set MELLONHEAD_ROOT or pass --repo. To create one, run "
              f"`mh init --repo {repo} --user <you>`.", file=sys.stderr)
        return 2

    try:
        check_args(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    store = mhstore.open_store(root=repo, seed_settings=False)
    try:
        return args.fn(store, repo, args) or 0
    except (CliError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


def _reference_user(args):
    """The repo's primary user, when there is a store to ask; else MQ's."""
    import os
    repo = args.repo or os.environ.get("MELLONHEAD_ROOT")
    if repo and (Path(repo).expanduser() / "operations" / "tasks.db").exists():
        store = mhstore.open_store(root=Path(repo).expanduser(),
                                   seed_settings=False)
        try:
            return store.primary_user, store.primary_name
        finally:
            store.close()
    return mhstore.LEGACY_PRIMARY_USER, mhstore.LEGACY_PRIMARY_NAME


if __name__ == "__main__":
    sys.exit(main())
