#!/usr/bin/env python3
"""
mhstore — the task record behind the session manager.

SQLite, stdlib only, to match server.py's no-dependency rule. One file at
<repo>/operations/tasks.db, so it sits in the same Drive backup path as the
markdown it replaces.

Three things guard against this being a one-way door:
  - every write appends a line to operations/tasks-audit.log
  - every write refreshes operations/tasks.json, a full dump
  - status_raw keeps the original markdown string forever, so a bad
    normalization can be redone from the store instead of from a backup

See operations/prioritization-implementation-plan.md sections 2.1 to 2.4.
"""

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

SCHEMA_VERSION = 1

# Task lifecycle. The markdown these came from used 48 distinct strings for
# these seven states; see normalize_status.
TASK_STATUSES = (
    "backlog", "planned", "in_progress", "waiting", "review", "done", "canceled",
)
OPEN_STATUSES = ("backlog", "planned", "in_progress", "waiting", "review")

# What Phase 1 actually writes, which is not what the plan specified: it uses
# done/cancelled where the plan said closed. Phase 1 is the running system and
# build-dashboard.py treats ("done", "cancelled") as archived, so the store
# follows it. 'closed' is accepted on the way in and folded into 'done'.
PROJECT_STATUSES = ("active", "waiting", "parked", "done", "cancelled")
ARCHIVED_PROJECT_STATUSES = ("done", "cancelled")
_PROJECT_STATUS_ALIASES = {"closed": "done", "canceled": "cancelled",
                           "complete": "done", "completed": "done"}

LOADS = ("deep", "medium", "shallow")
SOURCES = ("manual", "capture", "planning", "agent", "migration")

# Phase 1 writes the literal string "none" for an absent date or id, and the
# pre-Phase-1 files used several other sentinels. All of them mean NULL.
_NULL_SENTINELS = {
    "", "-", "--", "---", "none", "(none)", "n/a", "na", "tbd", "—",
    "(no notion task)", "no notion task", "null",
}

# Owner is written as "MQ" in the markdown; the plan's enum is lower case.
_OWNER_ALIASES = {"mq": "mq", "mariena": "mq", "": "mq"}

# Reserved project for work that belongs to no project.
ONE_OFF = "one-off"

DEFAULT_SETTINGS = {
    "stale_days": "5",
    "max_deep_per_week": "3",
    "business_days": '["Tue","Thu"]',
    "max_aba_days": "3",
    "slack_channel": "C0BJ6FFH3QQ",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS projects (
    key                TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    dir                TEXT,
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN {PROJECT_STATUSES!r}),
    owner              TEXT NOT NULL DEFAULT 'mq',
    due                TEXT,
    milestones         TEXT NOT NULL DEFAULT '[]',
    -- Not unique: the two TWG lists are byte-identical duplicates that share
    -- one Notion id. Deduplicate deliberately, do not let a constraint decide.
    notion_project_id  TEXT,
    goal               TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key    TEXT NOT NULL REFERENCES projects(key) ON UPDATE CASCADE,
    title          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'backlog'
                   CHECK (status IN {TASK_STATUSES!r}),
    -- The verbatim Status cell before normalization. Never read by logic;
    -- kept so a mapping mistake is recoverable without the markdown.
    status_raw     TEXT,
    owner          TEXT NOT NULL DEFAULT 'mq',
    seq            INTEGER,
    -- The markdown carried no ordering, only a single arrow per file marking
    -- the next action. Preserved as a flag; seq is populated going forward.
    is_next        INTEGER NOT NULL DEFAULT 0 CHECK (is_next IN (0, 1)),
    -- Sub-heading the row sat under. Three lists group rows into phases and
    -- would silently flatten without this.
    section        TEXT,
    -- The original '#' cell. Not a key: it holds ranges (3-17), suffixes
    -- (18b) and prefixes (W1). Display and traceability only.
    display_ord    TEXT,
    load           TEXT CHECK (load IS NULL OR load IN {LOADS!r}),
    due            TEXT,
    planned_day    TEXT,
    depends_on     INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    waiting_on     TEXT,
    note_path      TEXT,
    brief_path     TEXT,
    notion_task_id TEXT,
    source         TEXT NOT NULL DEFAULT 'manual'
                   CHECK (source IN {SOURCES!r}),
    confirmed      INTEGER NOT NULL DEFAULT 1 CHECK (confirmed IN (0, 1)),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    done_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_key, status);
CREATE INDEX IF NOT EXISTS idx_tasks_planned ON tasks(planned_day);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);

CREATE TABLE IF NOT EXISTS weeks (
    week_start   TEXT PRIMARY KEY,
    proposed_at  TEXT,
    locked_at    TEXT,
    rules_report TEXT NOT NULL DEFAULT '{{}}',
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Status normalization
#
# The markdown fused three facts into one cell: lifecycle state, a completion
# date, and a reason. These rules split them. First match wins, so the review
# rules sit above the draft rules ("Draft ready for MQ review" is review, not
# in_progress).
# ---------------------------------------------------------------------------

_STATUS_RULES = (
    (r"\breview\b",                                   "review"),
    (r"(~canceled|\b(killed|cancell?ed|superseded|dropped|abandoned)\b)", "canceled"),
    (r"\b(done|shipped|delivered|shared|complete|completed|closed|decided"
     r"|sent|submitted)\b",                           "done"),
    # "blocking" as well as "blocked": a row that is blocking someone else is
    # not finished, and pairing it with a done word is exactly the
    # contradiction status_is_ambiguous looks for.
    (r"\b(block(ed|ing|s)?|waiting|on hold|held)\b", "waiting"),
    (r"\b(in progress|underway|ongoing|drafted|draft|scoping|started"
     r"|unblocked|rulings in)\b",                     "in_progress"),
    (r"(~backlog|\b(backlog|todo|to do)\b)",          "backlog"),
    (r"\bplanned\b",                                  "planned"),
)

_DATE_PATTERNS = (
    (r"\b(\d{4})-(\d{2})-(\d{2})\b", "ymd"),
    (r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", "mdy"),
)


def _strip_markdown(text):
    """Bold, backticks and stray whitespace out; the words that remain."""
    text = re.sub(r"\*\*|__|`", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _prepare_status(text):
    """
    Lower-case the cell and deal with negation before any rule sees it.

    Several cells are sentences that mention a state in order to deny it
    ("In progress (Jennifer). NOT blocked.", "Anushka handoff NOT done").
    Matching on bare keywords reads those exactly backwards.

    Two negated phrases are idioms that name a state rather than denying
    one, so they are converted to tokens first and survive the sweep.
    """
    flat = _strip_markdown(text).lower()
    flat = re.sub(r"\bnot\s+(yet\s+)?started\b", " ~backlog ", flat)
    flat = re.sub(r"\bnot\s+(needed|required|happening)\b", " ~canceled ", flat)
    return re.sub(r"\bnot\s+\w+\b", " ", flat)


def extract_date(text, default_year=None):
    """First date in a status cell, ISO. Handles 2026-08-12 and 8/12."""
    if not text:
        return None
    for pattern, kind in _DATE_PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
        try:
            if kind == "ymd":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                mo, d = int(m.group(1)), int(m.group(2))
                raw_year = m.group(3)
                if raw_year:
                    y = int(raw_year)
                    if y < 100:
                        y += 2000
                else:
                    y = default_year or date.today().year
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def normalize_status(raw, default_year=None):
    """
    A markdown Status cell -> (status, done_at, raw).

    Unrecognized cells become 'backlog' rather than guessing, and keep their
    raw text so the migration report can list them for a human.
    """
    raw = (raw or "").strip()
    flat = _prepare_status(raw)
    if not flat.strip():
        return "backlog", None, raw

    status = None
    for pattern, mapped in _STATUS_RULES:
        if re.search(pattern, flat):
            status = mapped
            break
    if status is None:
        status = "backlog"

    done_at = extract_date(raw, default_year) if status == "done" else None
    return status, done_at, raw


def status_is_ambiguous(raw):
    """
    True when a Status cell is prose describing several states at once, or
    names no state this code recognizes.

    Some cells cannot be classified by rule and should not be guessed at:

        "Scope and objectives DONE 8/4. Anushka handoff NOT done.
         Blocking her."

    Read literally that is done; read honestly it is blocked. Migration
    lists these for a human rather than picking one silently. status_raw
    keeps the original either way, so a ruling can be revised later.
    """
    flat = _prepare_status(raw)
    if not flat.strip():
        return False
    matched = {s for pattern, s in _STATUS_RULES if re.search(pattern, flat)}
    if not matched:
        return True                      # fell through to the backlog default
    # Two open states ("Draft ready for MQ review" is both review and draft)
    # are resolved correctly by rule precedence and need no human. A terminal
    # state next to an open one is a real contradiction.
    terminal = matched & {"done", "canceled"}
    still_open = matched & set(OPEN_STATUSES)
    if terminal and still_open:
        return True
    return len(_strip_markdown(raw)) > 60


def clean(value):
    """A markdown cell -> its value, or None if it is one of the null sentinels."""
    if value is None:
        return None
    text = _strip_markdown(str(value))
    return None if text.lower() in _NULL_SENTINELS else text or None


def normalize_owner(value):
    """'MQ' / '' / 'Mariena' -> 'mq'. Anything else lower-cased."""
    text = (clean(value) or "").lower()
    return _OWNER_ALIASES.get(text, text or "mq")


def normalize_project_status(value):
    """Fold the plan's vocabulary into what Phase 1 writes."""
    text = (clean(value) or "active").lower()
    text = _PROJECT_STATUS_ALIASES.get(text, text)
    return text if text in PROJECT_STATUSES else "active"


def is_struck_through(title):
    """Phase 1 marks a task done by wrapping the title in ~~ as well."""
    return bool(re.match(r"^\s*~~.+~~\s*$", (title or "").strip()))


def clean_title(title):
    """Strip the next-action arrow and strikethrough markers from a task cell."""
    text = (title or "").strip()
    text = re.sub(r"^\s*\*\*\s*", "", text)
    text = re.sub(r"^\s*(→|->)\s*", "", text)
    text = re.sub(r"^\s*~~\s*|\s*~~\s*$", "", text)
    return _strip_markdown(text)


def has_next_marker(title):
    """The arrow lives at the start of the Task cell, sometimes behind bold."""
    return bool(re.match(r"^\s*(\*\*\s*)?(→|->)", (title or "")))


def slugify(text, max_len=48):
    """For note filenames: '<id>-<slug>.md'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0] or text[:max_len]
    return text or "task"


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class StoreError(Exception):
    pass


class Store:
    """
    Every mutating method takes actor= and writes one audit line. Nothing
    mutates without saying who did it.
    """

    def __init__(self, conn, root):
        self.conn = conn
        self.root = Path(root)
        self._depth = 0
        self._dirty = False
        self._pending_audit = []

    # -- infrastructure ----------------------------------------------------

    @property
    def audit_log(self):
        return self.root / "operations" / "tasks-audit.log"

    @property
    def snapshot_path(self):
        return self.root / "operations" / "tasks.json"

    @contextmanager
    def write(self):
        """
        Group writes into one transaction, one audit flush and one snapshot.
        Nests, so a high-level operation calling several low-level ones still
        commits once at the end.

        The connection is in autocommit mode (PRAGMA and executescript need
        it), so the transaction is opened by hand here. Without the explicit
        BEGIN a rollback would have nothing to undo.
        """
        top = self._depth == 0
        if top:
            self.conn.execute("BEGIN")
        self._depth += 1
        try:
            yield self
        except Exception:
            self._depth -= 1
            if top:
                self.conn.execute("ROLLBACK")
                # Audit is a file and cannot roll back, so buffered lines for
                # the abandoned writes are dropped rather than written.
                self._pending_audit.clear()
                self._dirty = False
            raise
        self._depth -= 1
        if top:
            self.conn.execute("COMMIT")
            self._flush()

    def _audit(self, actor, action, task_id, before, after):
        self._pending_audit.append(json.dumps({
            "ts": _now(), "actor": actor, "action": action,
            "task_id": task_id, "before": before, "after": after,
        }, ensure_ascii=False))
        self._dirty = True

    def _flush(self):
        """Append buffered audit lines and refresh the snapshot."""
        if self._pending_audit:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_log, "a") as f:
                f.write("\n".join(self._pending_audit) + "\n")
            self._pending_audit.clear()
        if self._dirty:
            self.write_snapshot()
            self._dirty = False

    def _commit_if_top(self):
        """A write made outside write() already autocommitted; just flush."""
        if self._depth == 0:
            self._flush()

    # -- reads -------------------------------------------------------------

    def project(self, key):
        row = self.conn.execute(
            "SELECT * FROM projects WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def projects(self, status=None):
        sql = "SELECT * FROM projects"
        args = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY key"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def task(self, task_id):
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def tasks(self, project_key=None, status=None, planned_day=None,
              open_only=False, confirmed=None):
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if project_key:
            sql += " AND project_key = ?"
            args.append(project_key)
        if status:
            sql += " AND status = ?"
            args.append(status)
        if planned_day:
            sql += " AND planned_day = ?"
            args.append(planned_day)
        if open_only:
            sql += f" AND status IN ({','.join('?' * len(OPEN_STATUSES))})"
            args.extend(OPEN_STATUSES)
        if confirmed is not None:
            sql += " AND confirmed = ?"
            args.append(1 if confirmed else 0)
        sql += " ORDER BY seq IS NULL, seq, id"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def next_task(self, project_key):
        """
        The project's next action: lowest open seq owned by mq, skipping
        anything still blocked by depends_on. Falls back to the migrated
        is_next flag while seq is unpopulated.
        """
        rows = self.conn.execute(
            f"""SELECT * FROM tasks
                WHERE project_key = ? AND owner = 'mq'
                  AND status IN ({','.join('?' * len(OPEN_STATUSES))})
                ORDER BY is_next DESC, seq IS NULL, seq, id""",
            (project_key, *OPEN_STATUSES)).fetchall()
        for row in rows:
            dep = row["depends_on"]
            if dep:
                blocker = self.task(dep)
                if blocker and blocker["status"] not in ("done", "canceled"):
                    continue
            return dict(row)
        return None

    def setting(self, key, default=None):
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # -- project writes ----------------------------------------------------

    def add_project(self, key, name, actor="system", **fields):
        if self.project(key):
            raise StoreError(f"project {key!r} already exists")
        if fields.get("status", "active") not in PROJECT_STATUSES:
            raise StoreError(f"bad project status {fields.get('status')!r}")
        now = _now()
        milestones = fields.get("milestones", [])
        if not isinstance(milestones, str):
            milestones = json.dumps(milestones, ensure_ascii=False)
        self.conn.execute(
            """INSERT INTO projects
               (key, name, dir, status, owner, due, milestones,
                notion_project_id, goal, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (key, name, fields.get("dir"), fields.get("status", "active"),
             fields.get("owner", "mq"), fields.get("due"), milestones,
             fields.get("notion_project_id"), fields.get("goal"), now, now))
        self._audit(actor, "project.add", None, None, {"key": key, "name": name})
        self._commit_if_top()
        return self.project(key)

    def update_project(self, key, actor="system", **fields):
        before = self.project(key)
        if not before:
            raise StoreError(f"no project {key!r}")
        allowed = {"name", "dir", "status", "owner", "due", "milestones",
                   "notion_project_id", "goal"}
        bad = set(fields) - allowed
        if bad:
            raise StoreError(f"cannot set {sorted(bad)} on a project")
        if "status" in fields and fields["status"] not in PROJECT_STATUSES:
            raise StoreError(f"bad project status {fields['status']!r}")
        if "milestones" in fields and not isinstance(fields["milestones"], str):
            fields["milestones"] = json.dumps(fields["milestones"], ensure_ascii=False)
        if not fields:
            return before
        sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
        self.conn.execute(f"UPDATE projects SET {sets} WHERE key = ?",
                          (*fields.values(), _now(), key))
        after = self.project(key)
        changed = {k: after[k] for k in fields}
        self._audit(actor, "project.update", None,
                    {k: before[k] for k in fields}, changed)
        self._commit_if_top()
        return after

    # -- task writes -------------------------------------------------------

    def add_task(self, project_key, title, actor="system", **fields):
        if not self.project(project_key):
            raise StoreError(f"no project {project_key!r}")
        title = (title or "").strip()
        if not title:
            raise StoreError("task needs a title")
        status = fields.get("status", "backlog")
        if status not in TASK_STATUSES:
            raise StoreError(f"bad task status {status!r}")
        source = fields.get("source", "manual")
        if source not in SOURCES:
            raise StoreError(f"bad source {source!r}")
        load = fields.get("load")
        if load is not None and load not in LOADS:
            raise StoreError(f"bad load {load!r}")

        now = _now()
        cols = {
            "project_key": project_key, "title": title, "status": status,
            "status_raw": fields.get("status_raw"),
            "owner": fields.get("owner", "mq"),
            "seq": fields.get("seq"),
            "is_next": 1 if fields.get("is_next") else 0,
            "section": fields.get("section"),
            "display_ord": fields.get("display_ord"),
            "load": load, "due": fields.get("due"),
            "planned_day": fields.get("planned_day"),
            "depends_on": fields.get("depends_on"),
            "waiting_on": fields.get("waiting_on"),
            "note_path": fields.get("note_path"),
            "brief_path": fields.get("brief_path"),
            "notion_task_id": fields.get("notion_task_id"),
            "source": source,
            "confirmed": 0 if fields.get("confirmed") == 0 else 1,
            "created_at": now, "updated_at": now,
            "done_at": fields.get("done_at"),
        }
        placeholders = ",".join("?" * len(cols))
        cur = self.conn.execute(
            f"INSERT INTO tasks ({','.join(cols)}) VALUES ({placeholders})",
            tuple(cols.values()))
        task_id = cur.lastrowid
        self._audit(actor, "task.add", task_id, None,
                    {"project": project_key, "title": title, "status": status})
        self._commit_if_top()
        return self.task(task_id)

    def update_task(self, task_id, actor="system", **fields):
        before = self.task(task_id)
        if not before:
            raise StoreError(f"no task {task_id}")
        allowed = {
            "title", "status", "status_raw", "owner", "seq", "is_next",
            "section", "display_ord", "load", "due", "planned_day",
            "depends_on", "waiting_on", "note_path", "brief_path",
            "notion_task_id", "source", "confirmed", "done_at",
        }
        bad = set(fields) - allowed
        if bad:
            raise StoreError(f"cannot set {sorted(bad)} on a task")
        if "status" in fields and fields["status"] not in TASK_STATUSES:
            raise StoreError(f"bad task status {fields['status']!r}")
        if fields.get("load") is not None and "load" in fields \
                and fields["load"] not in LOADS:
            raise StoreError(f"bad load {fields['load']!r}")
        if fields.get("depends_on") == task_id:
            raise StoreError("a task cannot depend on itself")
        if not fields:
            return before

        # Keep done_at honest unless the caller set it explicitly.
        if "status" in fields and "done_at" not in fields:
            if fields["status"] == "done" and not before["done_at"]:
                fields["done_at"] = date.today().isoformat()
            elif fields["status"] != "done" and before["done_at"]:
                fields["done_at"] = None

        sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
        self.conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?",
                          (*fields.values(), _now(), task_id))
        after = self.task(task_id)
        self._audit(actor, "task.update", task_id,
                    {k: before[k] for k in fields},
                    {k: after[k] for k in fields})
        self._commit_if_top()
        return after

    def complete_task(self, task_id, actor="system", done_at=None):
        return self.update_task(task_id, actor=actor, status="done",
                                done_at=done_at or date.today().isoformat())

    def reopen_task(self, task_id, actor="system", status="planned"):
        return self.update_task(task_id, actor=actor, status=status, done_at=None)

    def plan_task(self, task_id, day, actor="system"):
        """Put a task in the week. Only the UI and a plan lock may call this."""
        return self.update_task(task_id, actor=actor, planned_day=day,
                                status="planned")

    def unplan_task(self, task_id, actor="system"):
        return self.update_task(task_id, actor=actor, planned_day=None,
                                status="backlog")

    def confirm_task(self, task_id, actor="system"):
        return self.update_task(task_id, actor=actor, confirmed=1)

    def reorder(self, project_key, ordered_ids, actor="system"):
        """Rewrite seq for a project. Clears is_next once seq is real."""
        with self.write():
            for i, tid in enumerate(ordered_ids, start=1):
                row = self.task(tid)
                if not row or row["project_key"] != project_key:
                    raise StoreError(f"task {tid} is not in {project_key!r}")
                self.update_task(tid, actor=actor, seq=i, is_next=0)
        return self.tasks(project_key=project_key)

    # -- weeks -------------------------------------------------------------

    def week(self, week_start):
        row = self.conn.execute(
            "SELECT * FROM weeks WHERE week_start = ?", (week_start,)).fetchone()
        return dict(row) if row else None

    def upsert_week(self, week_start, actor="system", **fields):
        rules = fields.get("rules_report")
        if rules is not None and not isinstance(rules, str):
            fields["rules_report"] = json.dumps(rules, ensure_ascii=False)
        existing = self.week(week_start)
        if existing:
            allowed = {"proposed_at", "locked_at", "rules_report", "notes"}
            bad = set(fields) - allowed
            if bad:
                raise StoreError(f"cannot set {sorted(bad)} on a week")
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(f"UPDATE weeks SET {sets} WHERE week_start = ?",
                                  (*fields.values(), week_start))
        else:
            self.conn.execute(
                """INSERT INTO weeks (week_start, proposed_at, locked_at,
                                      rules_report, notes)
                   VALUES (?,?,?,?,?)""",
                (week_start, fields.get("proposed_at"), fields.get("locked_at"),
                 fields.get("rules_report", "{}"), fields.get("notes")))
        self._audit(actor, "week.upsert", None, existing, fields)
        self._commit_if_top()
        return self.week(week_start)

    def lock_week(self, week_start, actor="system", rules_report=None):
        with self.write():
            self.upsert_week(week_start, actor=actor, locked_at=_now(),
                             **({"rules_report": rules_report}
                                if rules_report is not None else {}))
            for row in self.conn.execute(
                    """SELECT id FROM tasks
                       WHERE planned_day >= ? AND planned_day <= date(?, '+6 day')
                         AND status = 'backlog'""",
                    (week_start, week_start)).fetchall():
                self.update_task(row["id"], actor=actor, status="planned")
        return self.week(week_start)

    # -- settings ----------------------------------------------------------

    def set_setting(self, key, value, actor="system"):
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        self._audit(actor, "setting.set", None, None, {key: str(value)})
        self._commit_if_top()

    # -- snapshot ----------------------------------------------------------

    def snapshot(self):
        return {
            "generated_at": _now(),
            "schema_version": SCHEMA_VERSION,
            "projects": [dict(r) for r in
                         self.conn.execute("SELECT * FROM projects ORDER BY key")],
            "tasks": [dict(r) for r in
                      self.conn.execute("SELECT * FROM tasks ORDER BY id")],
            "weeks": [dict(r) for r in
                      self.conn.execute("SELECT * FROM weeks ORDER BY week_start")],
            "settings": {r["key"]: r["value"] for r in
                         self.conn.execute("SELECT * FROM settings")},
        }

    def write_snapshot(self):
        """Atomic: temp file then replace, so a reader never sees half a dump."""
        path = self.snapshot_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(self.snapshot(), f, indent=2, ensure_ascii=False)
            f.write("\n")
        tmp.replace(path)
        return path

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------

def open_store(root=None, db_path=None, seed_settings=True):
    """
    Open (creating if needed) the store for a content repo.

    root      the repo, default $MELLONHEAD_ROOT or ~/Projects/mellonhead
    db_path   override the db location; ':memory:' for tests
    """
    import os

    if root is None:
        root = os.environ.get("MELLONHEAD_ROOT",
                              Path.home() / "Projects" / "mellonhead")
    root = Path(root).expanduser()

    if db_path is None:
        db_path = root / "operations" / "tasks.db"
    if db_path != ":memory:":
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    if db_path != ":memory:":
        # WAL so the server, the CLI and the Friday job can write concurrently.
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)

    store = Store(conn, root)
    if seed_settings:
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO NOTHING", (key, value))
        if not store.project(ONE_OFF):
            now = _now()
            conn.execute(
                """INSERT INTO projects (key, name, status, owner, milestones,
                                         created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (ONE_OFF, "One-off tasks", "active", "mq", "[]", now, now))
        conn.commit()
    return store


if __name__ == "__main__":
    import sys
    s = open_store()
    snap = s.snapshot()
    print(f"store:    {s.root / 'operations' / 'tasks.db'}")
    print(f"projects: {len(snap['projects'])}")
    print(f"tasks:    {len(snap['tasks'])}")
    print(f"weeks:    {len(snap['weeks'])}")
    sys.exit(0)
