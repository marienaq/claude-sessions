#!/usr/bin/env python3
"""
Tests for the task-view additions to the store and CLI (plan §A):
events on every write, questions, sessions, the new verbs, and the
generators' new lines.
"""

import io
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_mh_usage
import mhcli
import mhgen
import mhsession
import mhstore
from mhstore import StoreError

from test_mhmigrate import make_list

FAKE_SESSION = {
    "session_id": "11111111-2222-3333-4444-555555555555",
    "iterm_id": "E7B75229-D25E-4F72-B02A-98783AEAB9A5",
    "cwd": "/tmp/repo", "kind": "interactive", "name": "test-c1",
    "actor": None, "pid": 4242,
}


class StoreCase(unittest.TestCase):
    """A store on a throwaway repo with one project and one task."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "operations").mkdir(parents=True)
        (self.repo / "proj").mkdir()
        self.store = mhstore.open_store(root=self.repo)
        # Tests run inside a Claude session themselves; pin what the store
        # thinks its conversation is so assertions do not depend on that.
        self.store._session = dict(FAKE_SESSION)
        self.store.add_project("proj", "A Project", dir="proj")
        self.task = self.store.add_task("proj", "Build the thing", display_ord="1")
        make_list(self.repo / "proj" / "task-list.md", "proj",
                  [{"n": 1, "task": "placeholder"}])
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def events(self, **kw):
        return self.store.events(limit=None, newest_first=False, **kw)

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mhcli.main(["--repo", str(self.repo), *argv])
        return code, buf.getvalue()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class TestEvents(StoreCase):

    def test_every_write_makes_an_event_and_an_audit_line(self):
        before = len(self.events())
        lines_before = len(self.store.audit_log.read_text().splitlines())
        self.store.complete_task(self.task["id"], actor="Orca")
        self.assertEqual(len(self.events()), before + 1)
        self.assertEqual(len(self.store.audit_log.read_text().splitlines()),
                         lines_before + 1)

    def test_actor_is_lower_cased_on_both(self):
        self.store.complete_task(self.task["id"], actor="Orca")
        ev = self.events(task_id=self.task["id"])[-1]
        self.assertEqual(ev["actor"], "orca")
        last = json.loads(self.store.audit_log.read_text().splitlines()[-1])
        self.assertEqual(last["actor"], "orca")

    def test_kind_is_read_off_the_diff(self):
        tid = self.task["id"]
        self.store.plan_task(tid, "2026-09-10", actor="orca")
        self.store.update_task(tid, actor="orca", load="deep")
        self.store.update_task(tid, actor="orca", note_path="proj/task-notes/1-x.md")
        self.store.update_task(tid, actor="orca", brief_path="proj/brief.md")
        self.store.complete_task(tid, actor="mq")
        self.store.reopen_task(tid, actor="mq")
        kinds = [e["kind"] for e in self.events(task_id=tid)]
        self.assertEqual(kinds, ["add", "plan", "update", "note", "brief",
                                 "done", "reopen"])

    def test_event_carries_the_session(self):
        self.store.complete_task(self.task["id"], actor="mq")
        ev = self.events(task_id=self.task["id"])[-1]
        self.assertEqual(ev["session_id"], FAKE_SESSION["session_id"])
        self.assertEqual(ev["iterm_id"], FAKE_SESSION["iterm_id"])
        self.assertEqual(ev["project_key"], "proj")

    def test_first_write_records_the_session_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = mhstore.open_store(root=tmp)
            store._session = dict(FAKE_SESSION)
            self.assertIsNone(store.session_row(FAKE_SESSION["session_id"]))
            store.add_project("p", "P")
            row = store.session_row(FAKE_SESSION["session_id"])
            store.close()
        self.assertEqual(row["kind"], "interactive")
        self.assertEqual(row["name"], "test-c1")

    def test_no_session_leaves_nulls(self):
        self.store._session = dict(mhsession.EMPTY)
        self.store.complete_task(self.task["id"], actor="mq")
        ev = self.events(task_id=self.task["id"])[-1]
        self.assertIsNone(ev["session_id"])
        self.assertIsNone(ev["iterm_id"])

    def test_failed_batch_writes_no_event(self):
        before = len(self.events())
        with self.assertRaises(StoreError):
            with self.store.write():
                self.store.complete_task(self.task["id"], actor="mq")
                self.store.update_task(self.task["id"], actor="mq", bogus=1)
        self.assertEqual(len(self.events()), before)

    def test_events_listing_filters(self):
        tid = self.task["id"]
        self.store.dispatch(tid, "iddy", actor="orca")
        self.store.deliver(tid, "proj/out.md", actor="iddy")
        self.assertEqual([e["kind"] for e in self.events(task_id=tid, kind="dispatch")],
                         ["dispatch"])
        by_session = self.store.events(session_id=FAKE_SESSION["session_id"], limit=None)
        self.assertTrue(all(e["session_id"] == FAKE_SESSION["session_id"]
                            for e in by_session))


class TestMigration(unittest.TestCase):

    def test_v1_store_is_backfilled_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = mhstore.open_store(root=root)
            store._session = dict(mhsession.EMPTY)
            store.add_project("proj", "P", dir="proj")
            t = store.add_task("proj", "Old row", display_ord="1")
            store.complete_task(t["id"], actor="Orca")
            store.update_task(t["id"], actor="orca", note_path="proj/n.md")
            # Make it look like a v1 file: no events table, version 0.
            store.conn.execute("DROP TABLE events")
            store.conn.execute("PRAGMA user_version = 0")
            store.close()

            store = mhstore.open_store(root=root)
            kinds = [e["kind"] for e in store.events(limit=None, newest_first=False)]
            self.assertEqual(kinds, ["project", "add", "done", "note"])
            self.assertEqual({e["actor"] for e in store.events(limit=None)}
                             - {"orca", "system"}, set())
            self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0],
                             mhstore.SCHEMA_VERSION)
            store.close()

            store = mhstore.open_store(root=root)
            self.assertEqual(len(store.events(limit=None)), 4, "backfill must not repeat")
            store.close()

    def test_lines_written_by_a_v1_writer_are_picked_up(self):
        """
        During the dual-live window the live manager keeps appending audit
        lines with no event rows. Reopening must fill the gap, once.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = mhstore.open_store(root=root)
            store._session = dict(mhsession.EMPTY)
            store.add_project("proj", "P", dir="proj")
            t = store.add_task("proj", "Row", display_ord="1")
            store.close()
            line = json.dumps({"ts": "2026-09-10T18:30:00", "actor": "Orca",
                               "action": "task.update", "task_id": t["id"],
                               "before": {"status": "backlog"},
                               "after": {"status": "review"}})
            with open(root / "operations" / "tasks-audit.log", "a") as f:
                f.write(line + "\n" + line + "\n")     # two identical lines
            store = mhstore.open_store(root=root)
            rows = store.events(task_id=t["id"], kind="status", limit=None)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["actor"], "orca")
            store.close()
            store = mhstore.open_store(root=root)
            self.assertEqual(len(store.events(task_id=t["id"], kind="status", limit=None)), 2)
            store.close()

    def test_backfill_survives_a_deleted_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = mhstore.open_store(root=root)
            store._session = dict(mhsession.EMPTY)
            store.add_project("proj", "P", dir="proj")
            t = store.add_task("proj", "Gone", display_ord="1")
            store.conn.execute("DELETE FROM tasks WHERE id = ?", (t["id"],))
            store.conn.execute("DROP TABLE events")
            store.conn.execute("PRAGMA user_version = 0")
            store.close()
            store = mhstore.open_store(root=root)
            adds = store.events(kind="add", limit=None)
            self.assertEqual(len(adds), 1)
            self.assertIsNone(adds[0]["task_id"])
            self.assertEqual(adds[0]["project_key"], "proj")
            store.close()


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------

class TestSessionDetection(unittest.TestCase):

    def fake_tree(self, table):
        """table: {pid: (ppid, comm)}"""
        return lambda pid: table.get(pid)

    def test_finds_claude_up_the_tree(self):
        tree = {100: (90, "/bin/zsh"), 90: (80, "claude"), 80: (1, "-zsh")}
        self.assertEqual(mhsession.find_claude_pid(100, parent_of=self.fake_tree(tree)), 90)

    def test_gives_up_past_the_depth_limit(self):
        tree = {i: (i - 1, "/bin/sh") for i in range(2, 30)}
        tree[1] = (0, "claude")
        self.assertIsNone(mhsession.find_claude_pid(29, parent_of=self.fake_tree(tree)))

    def test_reads_the_pid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "90.json").write_text(json.dumps({
                "pid": 90, "sessionId": "abc-123", "cwd": "/w",
                "kind": "interactive", "name": "mellonhead-c5"}))
            tree = {100: (90, "/bin/zsh"), 90: (80, "claude")}
            info = mhsession.current_session(
                env={"ITERM_SESSION_ID": "w0t1p0:UUID-1", "MH_ACTOR": "Orca"},
                start_pid=100, parent_of=self.fake_tree(tree), sessions_dir=tmp)
        self.assertEqual(info["session_id"], "abc-123")
        self.assertEqual(info["iterm_id"], "UUID-1")
        self.assertEqual(info["name"], "mellonhead-c5")
        self.assertEqual(info["kind"], "interactive")
        self.assertEqual(info["actor"], "orca")
        self.assertEqual(info["cwd"], "/w")

    def test_missing_everything_is_all_none(self):
        info = mhsession.current_session(env={}, start_pid=100,
                                         parent_of=lambda pid: None)
        self.assertEqual({k for k, v in info.items() if v is not None}, set())

    def test_pid_file_without_kind_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "90.json").write_text(json.dumps({"sessionId": "s"}))
            tree = {100: (90, "/bin/zsh"), 90: (80, "claude")}
            info = mhsession.current_session(env={}, start_pid=100,
                                             parent_of=self.fake_tree(tree),
                                             sessions_dir=tmp)
        self.assertEqual(info["kind"], "unknown")

    def test_real_detection_is_cheap(self):
        """The budget is 20 ms per process; measured here against real ps."""
        t0 = time.perf_counter()
        mhsession.current_session()
        elapsed = (time.perf_counter() - t0) * 1000
        # Generous: a loaded machine can double the ps cost, and a flaky
        # timing test is worse than a loose one. Typical is 5-6 ms.
        self.assertLess(elapsed, 100)


# ---------------------------------------------------------------------------
# Dispatch, deliver, review, link
# ---------------------------------------------------------------------------

class TestAgentRecord(StoreCase):

    def test_deliver_closes_the_open_dispatch_for_the_same_agent(self):
        tid = self.task["id"]
        d1 = self.store.dispatch(tid, "iddy", expect="proj/a.md", actor="orca")
        self.store.dispatch(tid, "scout", expect="proj/b.md", actor="orca")
        self.assertEqual(len(self.store.open_dispatches(tid)), 2)
        ev = self.store.deliver(tid, "proj/a.md", actor="iddy")
        self.assertEqual(json.loads(ev["payload"])["closes"], d1["id"])
        still = self.store.open_dispatches(tid)
        self.assertEqual([d["agent"] for d in still], ["scout"])

    def test_deliver_without_a_dispatch_is_fine(self):
        ev = self.store.deliver(self.task["id"], "proj/a.md", actor="mark")
        self.assertEqual(ev["kind"], "deliver")
        self.assertNotIn("closes", json.loads(ev["payload"]))

    def test_deliver_by_someone_else_leaves_the_dispatch_open(self):
        """Orca recording for a subagent must say which one (agent=)."""
        tid = self.task["id"]
        self.store.dispatch(tid, "iddy", actor="orca")
        ev = self.store.deliver(tid, "proj/x.md", actor="orca")
        self.assertEqual(ev["agent"], "orca")
        self.assertNotIn("closes", json.loads(ev["payload"]))
        self.assertEqual([d["agent"] for d in self.store.open_dispatches(tid)], ["iddy"])
        ev = self.store.deliver(tid, "proj/x.md", actor="orca", agent="Iddy")
        self.assertEqual(ev["agent"], "iddy")
        self.assertEqual(self.store.open_dispatches(tid), [])

    def test_review_rejects_a_made_up_verdict(self):
        with self.assertRaises(StoreError):
            self.store.review(self.task["id"], "meh", actor="revi")
        ev = self.store.review(self.task["id"], "back", findings="proj/f.md",
                               actor="Revi-Iddy")
        self.assertEqual(ev["verdict"], "back")
        self.assertEqual(ev["actor"], "revi-iddy")
        self.assertEqual(self.store.last_review(self.task["id"])["id"], ev["id"])

    def test_link_records_the_current_session(self):
        ev = self.store.link_session(self.task["id"], actor="mq")
        self.assertEqual(ev["kind"], "link")
        self.assertEqual(ev["session_id"], FAKE_SESSION["session_id"])
        convs = self.store.sessions_for_task(self.task["id"])
        self.assertEqual([c["session_id"] for c in convs], [FAKE_SESSION["session_id"]])
        self.assertEqual(convs[0]["last_event"]["kind"], "link")

    def test_unlink_supersedes_link_and_keeps_both(self):
        tid = self.task["id"]
        self.store.link_session(tid, actor="mq")
        self.assertEqual(self.store.linked_sessions(tid), [FAKE_SESSION["session_id"]])
        self.store.unlink_session(tid, actor="mq")
        self.assertEqual(self.store.linked_sessions(tid), [])
        kinds = [e["kind"] for e in self.events(task_id=tid, kind=("link", "unlink"))]
        self.assertEqual(kinds, ["link", "unlink"])
        self.assertFalse(self.store.sessions_for_task(tid)[0]["linked"])
        self.store.link_session(tid, actor="mq")
        self.assertEqual(self.store.linked_sessions(tid), [FAKE_SESSION["session_id"]])

    def test_writing_to_a_task_does_not_link_it(self):
        self.store.dispatch(self.task["id"], "iddy", actor="orca")
        self.assertEqual(self.store.linked_sessions(self.task["id"]), [])
        self.assertFalse(self.store.sessions_for_task(self.task["id"])[0]["linked"])

    def test_link_on_behalf_of_another_session(self):
        ev = self.store.link_session(self.task["id"], actor="mq",
                                     session_id="other-sid", iterm_id="OTHER")
        self.assertEqual(ev["session_id"], "other-sid")
        self.assertEqual(ev["iterm_id"], "OTHER")
        self.assertIsNotNone(self.store.session_row("other-sid"))
        sids = {c["session_id"] for c in self.store.sessions_for_task(self.task["id"])}
        self.assertIn("other-sid", sids)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

class TestQuestions(StoreCase):

    def test_add_answer_and_next_open(self):
        tid = self.task["id"]
        q = self.store.add_question("75 or 90 minutes?", actor="Orca", task_id=tid,
                                    proposed="90", blocks="Anushka")
        self.assertEqual(q["status"], "open")
        self.assertEqual(q["asked_by"], "orca")
        self.assertEqual(q["project_key"], "proj")
        ans = self.store.answer_question(q["id"], "run the 90", actor="mq",
                                         source="dashboard")
        self.assertEqual(ans["status"], "answered")
        self.assertEqual(ans["answered_by"], "mq")
        self.assertEqual(ans["source"], "dashboard")
        self.assertEqual(self.store.questions(task_id=tid), [])
        kinds = [e["kind"] for e in self.events(task_id=tid)]
        self.assertEqual(kinds[-2:], ["question", "answer"])

    def test_accept_uses_the_proposed_answer(self):
        q = self.store.add_question("Which?", task_id=self.task["id"], proposed="B")
        ans = self.store.answer_question(q["id"], actor="mq", accept=True)
        self.assertEqual(ans["answer"], "B")
        ev = self.events(task_id=self.task["id"])[-1]
        self.assertIn("accepted", ev["summary"])

    def test_accept_without_a_proposal_is_refused(self):
        q = self.store.add_question("Which?", task_id=self.task["id"])
        with self.assertRaises(StoreError):
            self.store.answer_question(q["id"], actor="mq", accept=True)

    def test_answering_twice_is_refused(self):
        q = self.store.add_question("Which?", task_id=self.task["id"])
        self.store.answer_question(q["id"], "A", actor="mq")
        with self.assertRaises(StoreError):
            self.store.answer_question(q["id"], "B", actor="mq")

    def test_near_duplicate_is_refused_with_the_existing_id(self):
        q = self.store.add_question("Hold the VILT at 75 minutes or run the 90?",
                                    task_id=self.task["id"])
        with self.assertRaises(StoreError) as ctx:
            self.store.add_question("Hold the VILT at 75 minutes, or run the 90",
                                    task_id=self.task["id"])
        self.assertIn(f"Q{q['id']}", str(ctx.exception))
        # A different question on the same task is fine.
        self.store.add_question("Who opens the session?", task_id=self.task["id"])
        # And the same words on another task are fine too.
        other = self.store.add_task("proj", "Another", display_ord="2")
        self.store.add_question("Hold the VILT at 75 minutes or run the 90?",
                                task_id=other["id"])

    def test_answered_question_does_not_block_a_duplicate(self):
        q = self.store.add_question("Which?", task_id=self.task["id"])
        self.store.answer_question(q["id"], "A", actor="mq")
        self.store.add_question("Which?", task_id=self.task["id"])

    def test_project_level_question(self):
        q = self.store.add_question("Do we still owe a readout?", project_key="proj",
                                    actor="capture")
        self.assertIsNone(q["task_id"])
        self.assertEqual([x["id"] for x in self.store.questions(project_key="proj")],
                         [q["id"]])
        ev = self.events(project_key="proj", kind="question")[-1]
        self.assertIsNone(ev["task_id"])
        self.assertEqual(ev["project_key"], "proj")

    def test_withdraw(self):
        q = self.store.add_question("Moot?", task_id=self.task["id"])
        self.store.withdraw_question(q["id"], actor="orca", reason="answered in the brief")
        self.assertEqual(self.store.question(q["id"])["status"], "withdrawn")
        self.assertEqual(self.store.questions(task_id=self.task["id"]), [])

    def test_order_is_planned_day_then_due_then_asked(self):
        later = self.store.add_task("proj", "Later", display_ord="2", due="2026-10-01")
        soon = self.store.add_task("proj", "Soon", display_ord="3",
                                   planned_day="2026-09-10")
        q_none = self.store.add_question("no date", task_id=self.task["id"])
        q_later = self.store.add_question("due later", task_id=later["id"])
        q_soon = self.store.add_question("planned soon", task_id=soon["id"])
        self.assertEqual([q["id"] for q in self.store.questions()],
                         [q_soon["id"], q_later["id"], q_none["id"]])

    def test_dispatch_ready_truth_table(self):
        tid = self.task["id"]
        self.assertFalse(self.store.dispatch_ready(tid), "no brief")
        self.store.update_task(tid, brief_path="proj/brief.md")
        self.assertTrue(self.store.dispatch_ready(tid), "brief, no questions")
        q = self.store.add_question("Anything?", task_id=tid)
        self.assertTrue(self.store.dispatch_ready(tid),
                        "an open question that blocks nobody does not gate it")
        qb = self.store.add_question("Which room?", task_id=tid, blocks="Anushka")
        self.assertFalse(self.store.dispatch_ready(tid), "a blocking question gates it")
        self.store.answer_question(qb["id"], "B12", actor="mq")
        self.assertTrue(self.store.dispatch_ready(tid), "answering clears it")
        self.store.withdraw_question(q["id"])
        self.assertTrue(self.store.dispatch_ready(tid))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestVerbs(StoreCase):

    def test_dispatch_deliver_review_round_trip(self):
        code, out = self.run_cli("task", "dispatch", "proj#1", "--to", "Iddy",
                                 "--expect", "./proj/draft.md", "--actor", "orca")
        self.assertEqual(code, 0, out)
        self.assertIn("dispatched to iddy, expecting proj/draft.md", out)
        code, out = self.run_cli("task", "deliver", "proj#1", "--artifact",
                                 "proj/draft.md", "--actor", "iddy")
        self.assertEqual(code, 0, out)
        self.assertIn("closes dispatch", out)
        code, out = self.run_cli("task", "review", "proj#1", "--verdict",
                                 "pass-with-notes", "--findings", "proj/f.md",
                                 "--actor", "revi")
        self.assertEqual(code, 0, out)
        self.assertIn("review: pass-with-notes", out)
        kinds = [e["kind"] for e in self.events(task_id=self.task["id"])]
        self.assertEqual(kinds[-3:], ["dispatch", "deliver", "review"])

    def test_review_verdict_is_validated_by_the_parser(self):
        with self.assertRaises(SystemExit):
            self.run_cli("task", "review", "proj#1", "--verdict", "fine")

    def test_artifact_outside_the_repo_is_refused(self):
        code, out = self.run_cli("task", "deliver", "proj#1", "--artifact",
                                 "../elsewhere.md")
        self.assertEqual(code, 1)

    def test_brief_must_exist_under_the_repo(self):
        code, _ = self.run_cli("task", "brief", "proj#1", "proj/missing.md")
        self.assertEqual(code, 1)
        (self.repo / "proj" / "brief.md").write_text("# brief\n")
        code, out = self.run_cli("task", "brief", "proj#1",
                                 str(self.repo / "proj" / "brief.md"))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.task(self.task["id"])["brief_path"], "proj/brief.md")
        self.assertIn("dispatch-ready", out)

    def test_question_add_answer_accept_list(self):
        code, out = self.run_cli("question", "add", "proj#1", "75 or 90?",
                                 "--proposed", "90", "--blocks", "Anushka",
                                 "--actor", "orca")
        self.assertEqual(code, 0, out)
        self.assertIn("Q1", out)
        self.assertIn("Proposed: 90", out)
        code, out = self.run_cli("question", "list", "proj#1")
        self.assertIn("blocks Anushka", out)
        code, out = self.run_cli("question", "accept", "Q1", "--actor", "mq")
        self.assertEqual(code, 0, out)
        self.assertIn("answered by mq", out)
        code, out = self.run_cli("question", "list")
        self.assertIn("no open questions", out)
        code, out = self.run_cli("question", "list", "--all", "--json")
        rows = json.loads(out)
        self.assertEqual(rows[0]["answer"], "90")
        self.assertEqual(rows[0]["task"], "proj#1")

    def test_question_add_without_a_proposal_says_so(self):
        code, out = self.run_cli("question", "add", "proj#1", "Which?")
        self.assertEqual(code, 0)
        self.assertIn("no proposed answer", out)

    def test_question_duplicate_prints_the_existing_id_and_exits_zero(self):
        self.run_cli("question", "add", "proj#1", "Hold at 75 or run the 90?")
        code, out = self.run_cli("question", "add", "proj#1", "Hold at 75, or run the 90")
        self.assertEqual(code, 0)
        self.assertIn("already open as Q1", out)
        self.assertEqual(len(self.store.questions(task_id=self.task["id"])), 1)

    def test_question_on_a_project(self):
        code, out = self.run_cli("question", "add", "proj", "Still owe a readout?",
                                 "--proposed", "no", "--actor", "capture")
        self.assertEqual(code, 0, out)
        self.assertIn("[proj]", out)
        code, out = self.run_cli("question", "list", "proj")
        self.assertIn("Still owe a readout?", out)

    def test_answer_prints_the_next_open_question(self):
        self.run_cli("question", "add", "proj#1", "First?", "--proposed", "a")
        self.run_cli("question", "add", "proj#1", "Second?", "--proposed", "b")
        code, out = self.run_cli("question", "answer", "1", "yes", "--source", "slack")
        self.assertIn("next open on this task", out)
        self.assertIn("Second?", out)

    def test_question_writes_regenerate_the_task_list(self):
        self.run_cli("question", "add", "proj#1", "Which room?", "--proposed", "B12",
                     "--blocks", "Anushka")
        text = (self.repo / "proj" / "task-list.md").read_text()
        self.assertIn("Q1 (blocks Anushka): Which room? Proposed: B12", text)
        self.run_cli("question", "accept", "1")
        text = (self.repo / "proj" / "task-list.md").read_text()
        self.assertNotIn("Which room?", text)

    def test_task_list_json_carries_readiness(self):
        (self.repo / "proj" / "brief.md").write_text("# b\n")
        self.run_cli("task", "brief", "proj#1", "proj/brief.md")
        self.run_cli("task", "dispatch", "proj#1", "--to", "iddy")
        code, out = self.run_cli("task", "list", "proj", "--json")
        rows = json.loads(out)
        self.assertTrue(rows[0]["dispatch_ready"])
        self.assertEqual(rows[0]["open_dispatches"][0]["agent"], "iddy")

    def test_owner_hands_a_task_over_and_back(self):
        code, out = self.run_cli("task", "owner", "proj#1", "Anushka", "--actor", "orca")
        self.assertEqual(code, 0, out)
        self.assertIn("owner=anushka", out)
        self.assertEqual(self.store.task(self.task["id"])["owner"], "anushka")
        self.assertIsNone(self.store.next_task("proj"), "not MQ's next action any more")
        ev = self.events(task_id=self.task["id"])[-1]
        self.assertEqual((ev["kind"], ev["summary"]), ("update", "owner mq → anushka"))
        text = (self.repo / "proj" / "task-list.md").read_text()
        self.assertIn("| Anushka |", text)
        code, out = self.run_cli("task", "owner", "proj#1", "Mariena")
        self.assertEqual(self.store.task(self.task["id"])["owner"], "mq")
        code, out = self.run_cli("task", "owner", "proj#1", "MQ")
        self.assertIn("already owned by mq", out)

    def test_link_and_session_show(self):
        code, out = self.run_cli("task", "link", "proj#1", "--actor", "mq")
        self.assertEqual(code, 0, out)
        self.assertIn("linked", out)
        code, out = self.run_cli("task", "unlink", "proj#1", "--actor", "mq")
        self.assertEqual(code, 0, out)
        self.assertIn("unlinked", out)
        code, out = self.run_cli("session", "show")
        self.assertEqual(code, 0, out)

    def test_verify_is_clean_after_question_writes(self):
        self.run_cli("question", "add", "proj#1", "Which?", "--blocks", "Anushka")
        code, out = self.run_cli("verify")
        self.assertEqual(code, 0, out)


class TestReferenceAndChecker(unittest.TestCase):

    def test_committed_reference_includes_the_new_verbs(self):
        text = (Path(__file__).resolve().parent.parent / "mh-reference.md").read_text()
        for verb in ("mh task dispatch", "mh task deliver", "mh task review",
                     "mh task link", "mh task unlink", "mh task owner", "mh question add", "mh question answer",
                     "mh question accept", "mh question list", "mh session show"):
            self.assertIn(verb, text)

    def test_checker_accepts_the_skill_shapes(self):
        parser = mhcli.build_parser()
        for cmd in (
            'mh question add <key#id> "<question>" --proposed "<Orca\'s default>" --blocks "<who waits>" --actor orca',
            "mh task dispatch <key#id> --to <agent> --expect <path> --actor orca",
            "mh task deliver <key#id> --artifact <path> --actor orca --agent <agent>",
            "mh task review <key#id> --verdict <pass|pass-with-notes|back> --findings <path> --actor revi",
            "mh question answer <id> \"<text>\" --source <brief path>",
            "mh question list --open --json",
            "mh question list <key#id>",
            "mh session show",
        ):
            self.assertIsNone(check_mh_usage.check(cmd, parser), cmd)

    def test_checker_still_catches_a_bad_verdict(self):
        parser = mhcli.build_parser()
        self.assertIsNotNone(check_mh_usage.check(
            "mh task review proj#1 --verdict fine", parser))


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

class TestGeneratedViews(StoreCase):

    def test_blocking_question_in_priorities(self):
        self.store.upsert_week("2026-09-07")
        self.store.plan_task(self.task["id"], "2026-09-10")
        self.store.add_question("Which room?", task_id=self.task["id"],
                                proposed="B12", blocks="Anushka")
        self.store.add_question("Nobody waits", task_id=self.task["id"])
        text = mhgen.generate_priorities(self.store, self.repo, week_start="2026-09-07",
                                         dry_run=True)
        blocked = text.split("### Blocked or waiting")[1].split("##")[0]
        self.assertIn("**Q1** Which room? Proposed: B12. Blocks Anushka. `proj#1`", blocked)
        self.assertNotIn("Nobody waits", blocked)

    def test_dashboard_json_carries_open_questions(self):
        self.store.add_question("Which room?", task_id=self.task["id"], blocks="Anushka")
        mhgen.generate_dashboard(self.store, self.repo)
        data = json.loads((self.repo / "operations" / "projects-dashboard.json").read_text())
        proj = next(p for p in data["projects"] if p["key"] == "proj")
        self.assertEqual(proj["openQuestions"], 1)
        self.assertEqual(proj["questions"][0]["blocks"], "Anushka")
        md = (self.repo / "operations" / "projects-dashboard.md").read_text()
        self.assertIn("| Questions |", md)


if __name__ == "__main__":
    unittest.main()
