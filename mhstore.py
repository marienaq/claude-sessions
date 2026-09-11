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

import mhsession

# 2 added events, questions and sessions (the task view). Stored in
# PRAGMA user_version so open_store() can tell a v1 file from a v2 one.
SCHEMA_VERSION = 2

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

# What an event records. The task page renders one line per kind, so the
# list is the vocabulary of "what the agents did". 'update' covers a field
# change that is none of the named ones (load, seq, notes), and 'setting'
# a settings write; both exist so every audit line has a kind.
EVENT_KINDS = (
    "add", "status", "note", "plan", "done", "reopen", "confirm",
    "dispatch", "deliver", "review", "question", "answer",
    "brief", "link", "project", "week", "update", "setting",
)
VERDICTS = ("pass", "pass-with-notes", "back")
QUESTION_STATUSES = ("open", "answered", "withdrawn")
SESSION_KINDS = ("interactive", "scheduled", "unknown")

# Phase 1 writes the literal string "none" for an absent date or id, and the
# pre-Phase-1 files used several other sentinels. All of them mean NULL.
_NULL_SENTINELS = {
    "", "-", "--", "---", "none", "(none)", "n/a", "na", "tbd", "—",
    "(no notion task)", "no notion task", "null",
}

# The two enums spell "cancel" differently: tasks took the plan's spelling,
# projects took Phase 1's, and build-dashboard.py archives on "cancelled".
# Neither can change without breaking one of them, so both spellings are
# accepted on input and mapped to whichever the target enum uses. A caller
# should never have to remember which noun takes which spelling.
_TASK_STATUS_ALIASES = {"cancelled": "canceled", "killed": "canceled",
                        "blocked": "waiting", "not started": "backlog",
                        "in progress": "in_progress", "todo": "backlog"}


def normalize_task_status(value):
    """Fold spelling and near-miss variants onto the task enum."""
    text = (clean(value) or "").lower().strip().replace("-", "_")
    text = _TASK_STATUS_ALIASES.get(text.replace("_", " "), text)
    text = _TASK_STATUS_ALIASES.get(text, text)
    return text if text in TASK_STATUSES else None


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
    -- One sentence of context, rendered inline in the generated views.
    -- Anything longer than this lives in note_path. Without both, a short
    -- Notes cell or a short trailing clause had nowhere to go and was lost.
    notes          TEXT,
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

-- Who did what. One row per audit line; the JSONL file stays the readable
-- copy and the rollback path, this is what gets queried. session_id is the
-- Claude conversation that made the write, found by mhsession without
-- anyone passing it.
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    session_id    TEXT,
    iterm_id      TEXT,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    project_key   TEXT,
    kind          TEXT NOT NULL CHECK (kind IN {EVENT_KINDS!r}),
    summary       TEXT,
    artifact_path TEXT,
    agent         TEXT,
    verdict       TEXT CHECK (verdict IS NULL OR verdict IN {VERDICTS!r}),
    payload       TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS idx_events_task    ON events(task_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);

-- What a `Question:` task row wanted to be: an id, a status, a proposed
-- answer so the cheapest reply is Accept, and who is waiting on it.
CREATE TABLE IF NOT EXISTS questions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    project_key  TEXT NOT NULL REFERENCES projects(key) ON UPDATE CASCADE,
    text         TEXT NOT NULL,
    proposed     TEXT,
    blocks       TEXT,
    asked_by     TEXT NOT NULL,
    asked_at     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN {QUESTION_STATUSES!r}),
    answer       TEXT,
    answered_by  TEXT,
    answered_at  TEXT,
    source       TEXT
);
CREATE INDEX IF NOT EXISTS idx_questions_open ON questions(status, project_key);

-- Every Claude conversation the store has seen write to it.
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    iterm_id    TEXT,
    cwd         TEXT,
    kind        TEXT,
    actor       TEXT,
    name        TEXT,
    started_at  TEXT,
    last_seen   TEXT NOT NULL
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
# Events: what an audit line means
#
# The audit log records actions (task.update) with a before/after diff. The
# task page wants to say what happened in one word, so the diff is read once
# here, both for new writes and for the backfill of the 745 existing lines.
# ---------------------------------------------------------------------------

def event_kind(action, before, after):
    """'task.update' + a diff -> one of EVENT_KINDS."""
    after = after or {}
    before = before or {}
    if action == "task.add":
        return "add"
    if action.startswith("project."):
        return "project"
    if action.startswith("week."):
        return "week"
    if action.startswith("setting."):
        return "setting"
    if action != "task.update":
        return "update"
    if "status" in after:
        if after["status"] == "done":
            return "done"
        if before.get("status") == "done":
            return "reopen"
        if "planned_day" in after and after["status"] in ("planned", "backlog"):
            return "plan"
        return "status"
    if "note_path" in after:
        return "note"
    if "brief_path" in after:
        return "brief"
    if "confirmed" in after:
        return "confirm"
    if "planned_day" in after:
        return "plan"
    return "update"


def event_summary(kind, action, before, after):
    """The one line the task page shows for an audit-derived event."""
    after = after or {}
    before = before or {}
    if kind == "add":
        return f"added: {after.get('title', '')}".strip()
    if kind == "done":
        return "done"
    if kind == "reopen":
        return f"reopened as {after.get('status')}"
    if kind == "status":
        line = f"status {before.get('status')} → {after.get('status')}"
        if after.get("waiting_on"):
            line += f", waiting on {after['waiting_on']}"
        return line
    if kind == "plan":
        day = after.get("planned_day")
        return f"planned for {day}" if day else "taken off the week"
    if kind == "note":
        return "note appended"
    if kind == "brief":
        return f"brief: {after.get('brief_path') or 'cleared'}"
    if kind == "confirm":
        return "confirmed" if after.get("confirmed") else "unconfirmed"
    if kind == "project":
        return action.split(".")[-1] + " " + (after.get("key") or "")
    if kind == "week":
        return "week " + ", ".join(k for k in after) if isinstance(after, dict) else "week"
    if kind == "setting":
        return "setting " + ", ".join(f"{k}={v}" for k, v in after.items())
    changed = ", ".join(f"{k}={after[k]}" for k in after) if isinstance(after, dict) else ""
    return changed or action


def word_overlap(a, b):
    """
    Share of the words in `a` (longer than two letters) that also occur in
    `b`. The same rule `mh task find` scores by; used to refuse a question
    that is already open in other words.
    """
    def words(text):
        return {w for w in re.findall(r"[a-z0-9']+", (text or "").lower())
                if len(w) > 2}
    wa, wb = words(a), words(b)
    if not wa:
        return 0.0
    return len(wa & wb) / len(wa)


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
        # Which conversation this process is. Found once, lazily, and
        # stamped on every event; None everywhere when there is none.
        self._session = None
        self._session_recorded = False

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

    # -- session identity ---------------------------------------------------

    @property
    def session(self):
        """The conversation this process runs in, detected once per process."""
        if self._session is None:
            try:
                self._session = mhsession.current_session()
            except Exception:                              # noqa: BLE001
                self._session = dict(mhsession.EMPTY)
        return self._session

    def _record_session(self):
        """Upsert the sessions row for this conversation, once per process."""
        if self._session_recorded:
            return
        self._session_recorded = True
        info = self.session
        if not info.get("session_id"):
            return
        now = _now()
        self.conn.execute(
            """INSERT INTO sessions (session_id, iterm_id, cwd, kind, actor,
                                     name, started_at, last_seen)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 iterm_id  = COALESCE(excluded.iterm_id, sessions.iterm_id),
                 cwd       = COALESCE(excluded.cwd, sessions.cwd),
                 kind      = COALESCE(excluded.kind, sessions.kind),
                 actor     = COALESCE(excluded.actor, sessions.actor),
                 name      = COALESCE(excluded.name, sessions.name),
                 last_seen = excluded.last_seen""",
            (info["session_id"], info.get("iterm_id"), info.get("cwd"),
             info.get("kind") or "unknown", info.get("actor"),
             info.get("name"), now, now))

    def touch_session(self, session_id, actor=None, **fields):
        """
        Upsert a sessions row the dashboard learned about some other way (a
        card it discovered, a resume it launched). Not audited: it records
        that a conversation exists, not that anyone did anything.
        """
        now = _now()
        self.conn.execute(
            """INSERT INTO sessions (session_id, iterm_id, cwd, kind, actor,
                                     name, started_at, last_seen)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 iterm_id  = COALESCE(excluded.iterm_id, sessions.iterm_id),
                 cwd       = COALESCE(excluded.cwd, sessions.cwd),
                 kind      = COALESCE(excluded.kind, sessions.kind),
                 actor     = COALESCE(excluded.actor, sessions.actor),
                 name      = COALESCE(excluded.name, sessions.name),
                 last_seen = excluded.last_seen""",
            (session_id, fields.get("iterm_id"), fields.get("cwd"),
             fields.get("kind"), actor, fields.get("name"),
             fields.get("started_at") or now, now))

    # -- audit ------------------------------------------------------------

    def _audit(self, actor, action, task_id, before, after, **event):
        """
        One audit line and one event row per write.

        The line is the readable, greppable copy; the row is what the task
        page and the sweeps query. Both carry the same actor, lower-cased:
        the log had `orca` 62 times and `Orca` 28 before this.
        """
        actor = (actor or "system").strip().lower() or "system"
        ts = _now()
        self._pending_audit.append(json.dumps({
            "ts": ts, "actor": actor, "action": action,
            "task_id": task_id, "before": before, "after": after,
        }, ensure_ascii=False))
        self._dirty = True

        kind = event.pop("kind", None) or event_kind(action, before, after)
        summary = event.pop("summary", None)
        if summary is None:
            summary = event_summary(kind, action, before, after)
        project_key = event.pop("project_key", None)
        if project_key is None and task_id is not None:
            row = self.conn.execute(
                "SELECT project_key FROM tasks WHERE id = ?", (task_id,)).fetchone()
            project_key = row["project_key"] if row else None
        info = self.session
        self._record_session()
        payload = {"action": action, "before": before, "after": after}
        payload.update(event.pop("payload", None) or {})
        cur = self.conn.execute(
            """INSERT INTO events (ts, actor, session_id, iterm_id, task_id,
                                   project_key, kind, summary, artifact_path,
                                   agent, verdict, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, actor, info.get("session_id"), info.get("iterm_id"), task_id,
             project_key, kind, summary, event.get("artifact_path"),
             (event.get("agent") or None) and event["agent"].lower(),
             event.get("verdict"),
             json.dumps(payload, ensure_ascii=False)))
        return cur.lastrowid

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

        # Give the row a per-project number if the caller did not. Without one
        # a task created in the UI or the CLI has no key#N, so it cannot be
        # referenced by the convention every skill and every day-card line
        # uses, and it renders in the markdown with a raw store id.
        display_ord = fields.get("display_ord")
        if display_ord is None and project_key != ONE_OFF:
            existing = [t["display_ord"] for t in self.tasks(project_key=project_key)]
            numbers = [int(o) for o in existing if o and str(o).isdigit()]
            display_ord = str(max(numbers) + 1) if numbers else "1"

        now = _now()
        cols = {
            "project_key": project_key, "title": title, "status": status,
            "status_raw": fields.get("status_raw"),
            "owner": fields.get("owner", "mq"),
            "seq": fields.get("seq"),
            "is_next": 1 if fields.get("is_next") else 0,
            "section": fields.get("section"),
            "display_ord": display_ord,
            "load": load, "due": fields.get("due"),
            "planned_day": fields.get("planned_day"),
            "depends_on": fields.get("depends_on"),
            "waiting_on": fields.get("waiting_on"),
            "notes": fields.get("notes"),
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
            "depends_on", "waiting_on", "notes", "note_path", "brief_path",
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

        # A row that is no longer waiting is not waiting on anything. Leaving
        # the reason behind renders a finished task as "done, waiting on
        # Sharla's review", which reads as unfinished.
        if ("status" in fields and fields["status"] != "waiting"
                and "waiting_on" not in fields and before["waiting_on"]):
            fields["waiting_on"] = None

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

    # -- events ------------------------------------------------------------

    def events(self, task_id=None, session_id=None, project_key=None,
               kind=None, limit=50, newest_first=True):
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if task_id is not None:
            sql += " AND task_id = ?"
            args.append(task_id)
        if session_id:
            sql += " AND session_id = ?"
            args.append(session_id)
        if project_key:
            sql += " AND project_key = ?"
            args.append(project_key)
        if kind:
            if isinstance(kind, str):
                kind = (kind,)
            sql += f" AND kind IN ({','.join('?' * len(kind))})"
            args.extend(kind)
        sql += " ORDER BY ts DESC, id DESC" if newest_first else " ORDER BY ts, id"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args)]

    def record_event(self, task_id, kind, actor="system", summary=None,
                     **fields):
        """
        An event that is not a field change: dispatch, deliver, review,
        link. Audited like everything else, so the JSONL log has it too.
        """
        if kind not in EVENT_KINDS:
            raise StoreError(f"bad event kind {kind!r}")
        task = self.task(task_id) if task_id is not None else None
        if task_id is not None and not task:
            raise StoreError(f"no task {task_id}")
        after = {"kind": kind}
        for key in ("agent", "artifact_path", "verdict", "summary"):
            if fields.get(key):
                after[key] = fields[key]
        if summary:
            after["summary"] = summary
        event_id = self._audit(
            actor, f"task.{kind}", task_id, None, after, kind=kind,
            summary=summary, agent=fields.get("agent"),
            artifact_path=fields.get("artifact_path"),
            verdict=fields.get("verdict"), payload=fields.get("payload"),
            project_key=fields.get("project_key"))
        self._commit_if_top()
        return self.event(event_id)

    def event(self, event_id):
        row = self.conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def dispatch(self, task_id, agent, expect=None, actor="system", summary=None):
        agent = (agent or "").strip().lower()
        if not agent:
            raise StoreError("dispatch needs an agent")
        return self.record_event(
            task_id, "dispatch", actor=actor, agent=agent, artifact_path=expect,
            summary=summary or (f"dispatched to {agent}"
                                + (f", expecting {expect}" if expect else "")))

    def open_dispatches(self, task_id):
        """
        Dispatches with no delivery after them from the same agent.

        A delivery closes the newest open dispatch for its agent; a delivery
        with no agent closes the newest open dispatch of any agent. Nothing
        is edited to mark a dispatch closed: the record is the order of
        events, so the answer is always recomputable.
        """
        rows = self.events(task_id=task_id, kind=("dispatch", "deliver"),
                           limit=None, newest_first=False)
        open_ = []
        for row in rows:
            if row["kind"] == "dispatch":
                open_.append(row)
                continue
            agent = row["agent"]
            for i in range(len(open_) - 1, -1, -1):
                if agent is None or open_[i]["agent"] == agent:
                    del open_[i]
                    break
        return open_

    def deliver(self, task_id, artifact_path, actor="system", agent=None,
                summary=None):
        """
        The agent is the actor unless told otherwise: a specialist running
        as the session delivers its own work, and Orca recording for a
        subagent passes agent=. Closes that agent's newest open dispatch;
        with none to close, the delivery still stands on its own.
        """
        agent = (agent or actor or "").strip().lower() or None
        closes = None
        for d in reversed(self.open_dispatches(task_id)):
            if d["agent"] == agent:
                closes = d
                break
        return self.record_event(
            task_id, "deliver", actor=actor, agent=agent,
            artifact_path=artifact_path,
            summary=summary or f"delivered {artifact_path}",
            payload={"closes": closes["id"]} if closes else None)

    def review(self, task_id, verdict, findings=None, actor="system",
               agent=None, summary=None):
        if verdict not in VERDICTS:
            raise StoreError(f"verdict must be one of {', '.join(VERDICTS)}")
        return self.record_event(
            task_id, "review", actor=actor, agent=agent, verdict=verdict,
            artifact_path=findings,
            summary=summary or f"review: {verdict}")

    def link_session(self, task_id, actor="system", session_id=None,
                     iterm_id=None, summary=None):
        """
        Say that a conversation is about this task. The current session is
        the default; the dashboard passes one explicitly for a card it is
        linking on MQ's behalf.
        """
        info = self.session
        sid = session_id or info.get("session_id")
        payload = {"session_id": sid, "iterm_id": iterm_id or info.get("iterm_id")}
        row = self.record_event(
            task_id, "link", actor=actor,
            summary=summary or ("linked this conversation" if sid
                                else "linked (no session detected)"),
            payload=payload)
        if session_id and session_id != info.get("session_id"):
            # The event was stamped with the writer's own session; the
            # linked one is what the task page should join on.
            self.conn.execute(
                "UPDATE events SET session_id = ?, iterm_id = ? WHERE id = ?",
                (session_id, iterm_id, row["id"]))
            self.touch_session(session_id, iterm_id=iterm_id)
            self._commit_if_top()
            row = self.event(row["id"])
        return row

    def last_review(self, task_id):
        rows = self.events(task_id=task_id, kind="review", limit=1)
        return rows[0] if rows else None

    # -- questions ---------------------------------------------------------

    def question(self, question_id):
        row = self.conn.execute(
            "SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
        return dict(row) if row else None

    def questions(self, task_id=None, project_key=None, status="open",
                  include_task_level=True):
        """
        Open by default. Ordered the way the dashboard lists them: by the
        task's planned day, then its due date, then when they were asked.
        """
        sql = """SELECT q.* FROM questions q
                 LEFT JOIN tasks t ON t.id = q.task_id
                 WHERE 1=1"""
        args = []
        if status:
            sql += " AND q.status = ?"
            args.append(status)
        if task_id is not None:
            sql += " AND q.task_id = ?"
            args.append(task_id)
        if project_key:
            sql += " AND q.project_key = ?"
            args.append(project_key)
            if not include_task_level:
                sql += " AND q.task_id IS NULL"
        sql += """ ORDER BY t.planned_day IS NULL, t.planned_day,
                            t.due IS NULL, t.due, q.asked_at, q.id"""
        return [dict(r) for r in self.conn.execute(sql, args)]

    def find_duplicate_question(self, text, task_id=None, project_key=None,
                                threshold=0.8):
        """An open question on the same task (or project) in nearly the same words."""
        scope = self.questions(task_id=task_id) if task_id is not None \
            else self.questions(project_key=project_key, include_task_level=False)
        for q in scope:
            if word_overlap(text, q["text"]) >= threshold \
                    or word_overlap(q["text"], text) >= threshold:
                return q
        return None

    def add_question(self, text, actor="system", task_id=None,
                     project_key=None, proposed=None, blocks=None):
        text = (text or "").strip()
        if not text:
            raise StoreError("a question needs text")
        task = None
        if task_id is not None:
            task = self.task(task_id)
            if not task:
                raise StoreError(f"no task {task_id}")
            project_key = task["project_key"]
        if not project_key or not self.project(project_key):
            raise StoreError(f"no project {project_key!r}")
        dup = self.find_duplicate_question(text, task_id=task_id,
                                           project_key=project_key)
        if dup:
            raise StoreError(f"already open as Q{dup['id']}: {dup['text']}")
        actor = (actor or "system").strip().lower()
        now = _now()
        cur = self.conn.execute(
            """INSERT INTO questions (task_id, project_key, text, proposed,
                                      blocks, asked_by, asked_at, status)
               VALUES (?,?,?,?,?,?,?,'open')""",
            (task_id, project_key, text, (proposed or "").strip() or None,
             (blocks or "").strip() or None, actor, now))
        qid = cur.lastrowid
        line = f"Q{qid} asked: {text}"
        if blocks:
            line += f" (blocks {blocks})"
        self._audit(actor, "question.add", task_id, None,
                    {"question_id": qid, "text": text, "proposed": proposed,
                     "blocks": blocks},
                    kind="question", summary=line, project_key=project_key,
                    payload={"question_id": qid})
        self._commit_if_top()
        return self.question(qid)

    def answer_question(self, question_id, answer=None, actor="system",
                        source=None, accept=False):
        q = self.question(question_id)
        if not q:
            raise StoreError(f"no question Q{question_id}")
        if q["status"] != "open":
            raise StoreError(f"Q{question_id} is already {q['status']}")
        if accept:
            if not q["proposed"]:
                raise StoreError(f"Q{question_id} has no proposed answer to accept")
            answer = q["proposed"]
        answer = (answer or "").strip()
        if not answer:
            raise StoreError("an answer needs text")
        actor = (actor or "system").strip().lower()
        now = _now()
        self.conn.execute(
            """UPDATE questions SET status = 'answered', answer = ?,
                   answered_by = ?, answered_at = ?, source = ?
               WHERE id = ?""",
            (answer, actor, now, source, question_id))
        self._audit(actor, "question.answer", q["task_id"],
                    {"status": "open"},
                    {"question_id": question_id, "answer": answer,
                     "source": source, "accepted": bool(accept)},
                    kind="answer", project_key=q["project_key"],
                    summary=f"Q{question_id} {'accepted' if accept else 'answered'}: {answer}",
                    payload={"question_id": question_id})
        self._commit_if_top()
        return self.question(question_id)

    def withdraw_question(self, question_id, actor="system", reason=None):
        q = self.question(question_id)
        if not q:
            raise StoreError(f"no question Q{question_id}")
        if q["status"] != "open":
            raise StoreError(f"Q{question_id} is already {q['status']}")
        actor = (actor or "system").strip().lower()
        self.conn.execute(
            """UPDATE questions SET status = 'withdrawn', answered_by = ?,
                   answered_at = ?, answer = ? WHERE id = ?""",
            (actor, _now(), reason, question_id))
        self._audit(actor, "question.withdraw", q["task_id"],
                    {"status": "open"}, {"question_id": question_id,
                                         "reason": reason},
                    kind="question", project_key=q["project_key"],
                    summary=f"Q{question_id} withdrawn"
                            + (f": {reason}" if reason else ""),
                    payload={"question_id": question_id})
        self._commit_if_top()
        return self.question(question_id)

    def blocking_questions(self, task_id):
        return [q for q in self.questions(task_id=task_id) if q["blocks"]]

    def dispatch_ready(self, task_id):
        """
        Derived, never stored: a brief exists and no open question on the
        task blocks anyone. The brief's own "Dispatch-ready" line used to
        be flipped by hand and lied; see task note #242.
        """
        task = self.task(task_id)
        if not task or not task["brief_path"]:
            return False
        return not self.blocking_questions(task_id)

    # -- sessions ----------------------------------------------------------

    def session_row(self, session_id):
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def sessions_for_task(self, task_id):
        """
        Every conversation that has written to this task, newest write
        first, with the last thing it wrote. Includes conversations the
        sessions table never saw (a link recorded by the dashboard for a
        transcript it discovered), so the join is left.
        """
        rows = self.conn.execute(
            """SELECT e.session_id, e.iterm_id, MAX(e.ts) AS last_ts,
                      COUNT(*) AS n
               FROM events e
               WHERE e.task_id = ? AND e.session_id IS NOT NULL
               GROUP BY e.session_id
               ORDER BY last_ts DESC""", (task_id,)).fetchall()
        result = []
        for r in rows:
            last = self.conn.execute(
                """SELECT * FROM events WHERE task_id = ? AND session_id = ?
                   ORDER BY ts DESC, id DESC LIMIT 1""",
                (task_id, r["session_id"])).fetchone()
            info = self.session_row(r["session_id"]) or {
                "session_id": r["session_id"], "iterm_id": r["iterm_id"],
                "cwd": None, "kind": None, "actor": None, "name": None,
                "started_at": None, "last_seen": r["last_ts"]}
            info = dict(info)
            info["iterm_id"] = info.get("iterm_id") or r["iterm_id"]
            info["event_count"] = r["n"]
            info["last_event"] = dict(last) if last else None
            result.append(info)
        return result

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
            "questions": [dict(r) for r in
                          self.conn.execute("SELECT * FROM questions ORDER BY id")],
            "sessions": [dict(r) for r in
                         self.conn.execute("SELECT * FROM sessions ORDER BY last_seen")],
        }

    def write_snapshot(self):
        """
        Atomic: temp file then replace, so a reader never sees half a dump.

        The temp file needs a unique name. With a fixed one, two writers race:
        both create it, the first renames it away, and the second's rename
        raises FileNotFoundError after its database commit has already
        succeeded. The caller then sees a failure for a write that landed.
        Four concurrent writers lost 10% of their calls that way.
        """
        import os
        import tempfile

        path = self.snapshot_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".tasks-", suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.snapshot(), f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
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
    have = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "notes" not in have:
        conn.execute("ALTER TABLE tasks ADD COLUMN notes TEXT")

    store = Store(conn, root)
    migrate_schema(store)
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


def audit_key(ts, actor, action, task_id, before, after):
    """What makes an audit line the same fact as an event row."""
    return (ts, (actor or "system").strip().lower() or "system", action,
            task_id, json.dumps(before, sort_keys=True, ensure_ascii=False),
            json.dumps(after, sort_keys=True, ensure_ascii=False))


def migrate_schema(store):
    """
    Bring the file up to date, and keep events in step with the audit log.

    The tables are CREATE IF NOT EXISTS, so the only real work is syncing
    `events` from the audit lines that have no row yet. That is keyed on
    content rather than on "the table is empty": a v1 writer that keeps
    appending audit lines after the tables exist (the live manager during
    the dual-live window) must not leave a hole at cutover. The check is a
    line count against a row count; both are cheap.
    """
    conn = store.conn
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    synced = 0
    if store.audit_log.exists():
        try:
            with open(store.audit_log, "rb") as f:
                lines = sum(1 for line in f if line.strip())
        except OSError:
            lines = 0
        rows = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if version < SCHEMA_VERSION or lines > rows:
            synced = sync_events_from_audit(store)
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return synced


def sync_events_from_audit(store):
    """
    One event row for every audit line that has none. Returns the count.

    Matching is on (ts, actor, action, task_id, before, after), as a
    multiset: two identical lines in the log get two rows. Nothing beyond
    the line is inferred, and rows are never removed.
    """
    from collections import Counter

    conn = store.conn
    have = Counter()
    for row in conn.execute("SELECT ts, actor, task_id, payload FROM events"):
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        have[audit_key(row["ts"], row["actor"], payload.get("action"),
                       row["task_id"], payload.get("before"),
                       payload.get("after"))] += 1

    n = 0
    conn.execute("BEGIN")
    try:
        with open(store.audit_log) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                action = rec.get("action") or "unknown"
                before, after = rec.get("before"), rec.get("after")
                task_id = rec.get("task_id")
                key = audit_key(rec.get("ts"), rec.get("actor"), action,
                                task_id, before, after)
                if have[key] > 0:
                    have[key] -= 1
                    continue
                kind = event_kind(action, before, after)
                project_key = None
                if task_id is not None:
                    row = conn.execute("SELECT project_key FROM tasks WHERE id = ?",
                                       (task_id,)).fetchone()
                    if row:
                        project_key = row["project_key"]
                    else:
                        if isinstance(after, dict):
                            project_key = after.get("project")
                        task_id = None      # the row is gone; keep the line, drop the FK
                elif action.startswith("project.") and isinstance(after, dict):
                    project_key = after.get("key")
                conn.execute(
                    """INSERT INTO events (ts, actor, session_id, iterm_id, task_id,
                                           project_key, kind, summary, artifact_path,
                                           agent, verdict, payload)
                       VALUES (?,?,NULL,NULL,?,?,?,?,NULL,NULL,NULL,?)""",
                    (rec.get("ts") or _now(),
                     (rec.get("actor") or "system").strip().lower() or "system",
                     task_id, project_key, kind,
                     event_summary(kind, action, before, after),
                     json.dumps({"action": action, "before": before,
                                 "after": after, "backfilled": True},
                                ensure_ascii=False)))
                n += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n


if __name__ == "__main__":
    import sys
    s = open_store()
    snap = s.snapshot()
    print(f"store:    {s.root / 'operations' / 'tasks.db'}")
    print(f"projects: {len(snap['projects'])}")
    print(f"tasks:    {len(snap['tasks'])}")
    print(f"weeks:    {len(snap['weeks'])}")
    sys.exit(0)
