#!/usr/bin/env python3
"""
Tests for the dashboard's task view (plan §B): the read model, the write
endpoints, the Orca launcher's resume-or-fresh choice, and the front end's
guards. Endpoints are exercised over HTTP against a server on a free port,
so the routing is tested too, not just the functions behind it.
"""

import http.server
import importlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mhsession
import mhstore

SERVER_PY = Path(__file__).resolve().parent.parent / "server.py"

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"


class TaskViewCase(unittest.TestCase):
    """
    A fixture store with a brief, two questions (one blocking), one
    dispatch, and events across two sessions. What plan §B4 asks for.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "operations").mkdir(parents=True)
        (self.repo / "proj").mkdir()
        self._prev = {k: os.environ.get(k) for k in ("MELLONHEAD_ROOT", "CSM_STATE_DIR")}
        os.environ["MELLONHEAD_ROOT"] = str(self.repo)
        os.environ["CSM_STATE_DIR"] = str(self.repo / ".state")

        (self.repo / "proj" / "brief.md").write_text(
            "# The brief\n\n**Task:** `proj#1`\n**Owner:** Mariena\n"
            "**Dispatch-ready:** no\n**First artifact:** `proj/out.md` **(soon)**\n\n---\n\n"
            "## Problem\n\nNot shown.\n\n## Goal\n\nOne ruling on the scope.\n\n"
            "## Deliverables\n\n1. The ruling\n2. An outline\n\n## Findings\n\nNot shown.\n")
        store = mhstore.open_store(root=self.repo)
        store._session = dict(mhsession.EMPTY)
        store.add_project("proj", "A Project", dir="proj")
        self.task = store.add_task("proj", "Decide the scope", display_ord="1",
                                   planned_day="2026-09-10", status="planned",
                                   brief_path="proj/brief.md")
        self.other = store.add_task("proj", "Another row", display_ord="2")
        store._session = {**mhsession.EMPTY, "session_id": SID_A, "iterm_id": "ITERM-A",
                          "kind": "interactive", "name": "conv-a"}
        self.q_block = store.add_question("75 or 90?", actor="orca", task_id=self.task["id"],
                                          proposed="90", blocks="Anushka")
        self.q_free = store.add_question("Who opens?", actor="orca", task_id=self.task["id"],
                                         proposed="Mariena")
        self.q_proj = store.add_question("Owe a readout?", actor="capture",
                                         project_key="proj", proposed="no")
        store.dispatch(self.task["id"], "iddy", expect="proj/out.md", actor="orca")
        store.link_session(self.task["id"], actor="orca")      # SID_A is about it
        store._session = {**mhsession.EMPTY, "session_id": SID_B, "kind": "scheduled",
                          "name": "sweep"}
        store._session_recorded = False     # a second process, in effect
        store.update_task(self.task["id"], actor="orca", load="deep")
        store.close()

        import server
        importlib.reload(server)
        server._STORE = None
        server._SESSIONS_CACHE["sessions"] = [
            {"itermId": "ITERM-A", "claudeSessionId": SID_A, "sessionId": SID_A,
             "name": "conv-a", "cwd": str(self.repo), "cardState": "ready",
             "isInactive": False, "isAutomation": False, "uptime": "2h"},
        ]
        server._SESSIONS_CACHE["at"] = time.time() + 3600   # never recompute
        server._LIVE_KEY_MAP["ITERM-A"] = SID_A
        self.server = server
        self.launched = []
        server.launch_claude_session = lambda cwd, prompt, resume_id=None, agent=None, \
            prompt_name=None: self.launched.append(
                {"cwd": cwd, "prompt": prompt, "resume": resume_id, "agent": agent}) or True

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if getattr(self.server, "_STORE", None):
            self.server._STORE.close()
            self.server._STORE = None
        for key, prev in self._prev.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        self._tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, json.loads(r.read())

    def post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


class TestReadModel(TaskViewCase):

    def test_payload_shape(self):
        status, d = self.get(f"/api/task/{self.task['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(d["task"]["tag"], "proj#1")
        self.assertEqual(d["task"]["plannedDay"], "2026-09-10")
        self.assertEqual(d["project"]["key"], "proj")
        self.assertFalse(d["dispatchReady"], "a blocking question gates it")
        self.assertEqual([q["id"] for q in d["questions"]["task"]],
                         [self.q_block["id"], self.q_free["id"]])
        self.assertEqual([q["id"] for q in d["questions"]["project"]], [self.q_proj["id"]])
        self.assertEqual(d["next"]["waitingOn"], [self.q_block["id"]])
        self.assertEqual([x["agent"] for x in d["next"]["dispatches"]], ["iddy"])
        self.assertEqual(d["events"][0]["kind"], "update", "newest first")
        self.assertTrue(len(d["events"]) >= 5)

    def test_brief_is_header_goal_and_deliverables_only(self):
        _, d = self.get(f"/api/task/{self.task['id']}")
        b = d["brief"]
        self.assertTrue(b["exists"])
        self.assertEqual(b["title"], "The brief")
        self.assertEqual(b["header"]["Owner"], "Mariena")
        self.assertEqual(b["header"]["First artifact"], "proj/out.md (soon)",
                         "bold and backticks come off")
        self.assertEqual(b["goal"], "One ruling on the scope.")
        self.assertEqual(b["deliverables"], "1. The ruling\n2. An outline")
        self.assertNotIn("Not shown", json.dumps(b))
        self.assertTrue(b["obsidianUrl"].startswith("obsidian://open?path="))

    def test_missing_brief_file(self):
        store = self.server.get_store()
        store.update_task(self.other["id"], brief_path="proj/nope.md")
        _, d = self.get(f"/api/task/{self.other['id']}")
        self.assertFalse(d["brief"]["exists"])
        _, d = self.get(f"/api/task/{self.task['id']}")

    def test_conversations_are_the_linked_ones_joined_to_live_cards(self):
        """SID_B wrote to the task but was never linked: timeline, not a conversation."""
        _, d = self.get(f"/api/task/{self.task['id']}")
        convs = {c["sessionId"]: c for c in d["conversations"]}
        self.assertEqual(set(convs), {SID_A})
        a = convs[SID_A]
        self.assertTrue(a["live"])
        self.assertEqual(a["itermId"], "ITERM-A")
        self.assertEqual(a["state"], "ready")
        self.assertEqual(a["lastEvent"]["kind"], "link")
        self.assertIn(SID_B, [e["sessionId"] for e in d["events"]], "still in the timeline")

    def test_a_scheduled_session_shows_once_linked(self):
        store = self.server.get_store()
        store.link_session(self.task["id"], actor="mq", session_id=SID_B)
        _, d = self.get(f"/api/task/{self.task['id']}")
        convs = {c["sessionId"]: c for c in d["conversations"]}
        self.assertIn(SID_B, convs)
        self.assertEqual(convs[SID_B]["kind"], "scheduled")
        self.assertEqual(convs[SID_B]["actor"], "sweep")
        self.assertFalse(convs[SID_B]["live"])
        self.assertEqual(d["conversations"][0]["sessionId"], SID_A, "live first")

    def test_unlink_hides_the_conversation(self):
        store = self.server.get_store()
        store.unlink_session(self.task["id"], actor="mq", session_id=SID_A)
        _, d = self.get(f"/api/task/{self.task['id']}")
        self.assertEqual(d["conversations"], [])
        store.link_session(self.task["id"], actor="mq", session_id=SID_A)
        _, d = self.get(f"/api/task/{self.task['id']}")
        self.assertEqual([c["sessionId"] for c in d["conversations"]], [SID_A])

    def test_unknown_task_is_404(self):
        try:
            self.get("/api/task/999")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)
        else:
            self.fail("expected 404")

    def test_inflight_list(self):
        store = self.server.get_store()
        store.add_task("proj", "Nothing going on", display_ord="3")
        _, d = self.get("/api/tasks")
        tags = [t["tag"] for t in d["tasks"]]
        self.assertIn("proj#1", tags)
        self.assertNotIn("proj#3", tags, "no brief, question, dispatch or conversation")
        row = next(t for t in d["tasks"] if t["tag"] == "proj#1")
        self.assertEqual(row["openQuestions"], 2)
        self.assertTrue(row["openDispatches"])
        self.assertEqual(row["liveConversations"], 1)


class TestWrites(TaskViewCase):

    def test_answer_writes_as_mq_and_returns_the_trimmed_list(self):
        status, d = self.post("/api/question/answer",
                              {"id": self.q_block["id"], "answer": "run the 90"})
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])
        self.assertEqual([q["id"] for q in d["questions"]["task"]], [self.q_free["id"]])
        self.assertEqual([q["id"] for q in d["questions"]["project"]], [self.q_proj["id"]])
        store = self.server.get_store()
        q = store.question(self.q_block["id"])
        self.assertEqual((q["status"], q["answered_by"], q["source"], q["answer"]),
                         ("answered", "mq", "dashboard", "run the 90"))
        self.assertTrue(store.dispatch_ready(self.task["id"]))
        self.assertEqual(store.events(task_id=self.task["id"], limit=1)[0]["actor"], "mq")

    def test_accept_uses_the_proposal(self):
        status, d = self.post("/api/question/accept", {"id": self.q_free["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(d["question"]["answer"], "Mariena")

    def test_accept_without_a_proposal_is_400(self):
        store = self.server.get_store()
        q = store.add_question("Open ended?", task_id=self.other["id"])
        status, d = self.post("/api/question/accept", {"id": q["id"]})
        self.assertEqual(status, 400)
        self.assertFalse(d["ok"])

    def test_answer_regenerates_the_task_list(self):
        from test_mhmigrate import make_list
        make_list(self.repo / "proj" / "task-list.md", "proj", [{"n": 1, "task": "x"}])
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")
        self.post("/api/question/answer", {"id": self.q_block["id"], "answer": "90"})
        text = (self.repo / "proj" / "task-list.md").read_text()
        self.assertNotIn("75 or 90?", text)
        self.assertIn("Who opens?", text)

    def test_link_writes_ids_and_an_event(self):
        status, d = self.post("/api/task/link", {"itermId": SID_A, "taskId": self.task["id"]})
        self.assertEqual(status, 200, d)
        state = json.loads((self.repo / ".state" / "sessions.json").read_text())
        link = state["taskAssignments"][SID_A][0]
        self.assertEqual(link["taskId"], self.task["id"])
        self.assertEqual(link["projectKey"], "proj")
        self.assertTrue(link["taskFile"].endswith("proj/task-list.md"))
        store = self.server.get_store()
        ev = store.events(task_id=self.task["id"], kind="link", limit=1)[0]
        self.assertEqual(ev["session_id"], SID_A)
        self.assertEqual(ev["actor"], "mq")

    def test_a_card_can_link_several_tasks(self):
        self.post("/api/task/link", {"itermId": SID_A, "taskId": self.task["id"]})
        self.post("/api/task/link", {"itermId": SID_A, "taskId": self.other["id"]})
        self.post("/api/task/link", {"itermId": SID_A, "taskId": self.task["id"]})   # again
        state = json.loads((self.repo / ".state" / "sessions.json").read_text())
        ids = [a["taskId"] for a in state["taskAssignments"][SID_A]]
        self.assertEqual(sorted(ids), sorted([self.task["id"], self.other["id"]]), "no duplicates")
        _, d = self.get(f"/api/task/{self.other['id']}")
        self.assertIn(SID_A, [c["sessionId"] for c in d["conversations"]])
        _, d = self.get(f"/api/task/{self.task['id']}")
        self.assertIn(SID_A, [c["sessionId"] for c in d["conversations"]])

    def test_unlink_takes_one_task_off_and_records_it(self):
        self.post("/api/task/link", {"itermId": SID_A, "taskId": self.task["id"]})
        self.post("/api/task/link", {"itermId": SID_A, "taskId": self.other["id"]})
        status, d = self.post("/api/task/unlink", {"itermId": SID_A, "taskId": self.task["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(d["remaining"], 1)
        state = json.loads((self.repo / ".state" / "sessions.json").read_text())
        self.assertEqual([a["taskId"] for a in state["taskAssignments"][SID_A]], [self.other["id"]])
        store = self.server.get_store()
        self.assertTrue(store.events(task_id=self.task["id"], kind="link", limit=1), "history kept")
        self.assertEqual(store.events(task_id=self.task["id"], limit=1)[0]["kind"], "unlink")
        self.assertNotIn(SID_A, store.linked_sessions(self.task["id"]))
        _, page = self.get(f"/api/task/{self.task['id']}")
        self.assertEqual(page["conversations"], [])
        self.post("/api/task/unlink", {"itermId": SID_A, "taskId": self.other["id"]})
        state = json.loads((self.repo / ".state" / "sessions.json").read_text())
        self.assertNotIn(SID_A, state["taskAssignments"])

    def test_old_single_entry_reads_as_a_list(self):
        self.assertEqual(self.server.assignment_list({"taskId": 3}), [{"taskId": 3}])
        self.assertEqual(self.server.assignment_list([{"taskId": 3}, "junk"]), [{"taskId": 3}])
        self.assertEqual(self.server.assignment_list(None), [])
        state = {"taskAssignments": {"X": {"taskFile": "/x", "notionTaskId": "", "taskTitle": "t", "taskId": 1}}}
        self.server.resolve_task_assignments(state)
        self.assertIsInstance(state["taskAssignments"]["X"], list)

    def test_link_without_a_real_task_is_400(self):
        status, _ = self.post("/api/task/link", {"itermId": SID_A, "taskId": 999})
        self.assertEqual(status, 400)

    def test_old_link_task_carries_the_store_id(self):
        path = self.repo / "proj" / "task-list.md"
        path.write_text("# proj\n")
        status, d = self.post("/api/link-task", {
            "itermId": "ITERM-A", "taskFile": str(path), "notionTaskId": "",
            "taskTitle": "Decide the scope", "taskId": self.task["id"]})
        self.assertEqual(status, 200, d)
        state = json.loads((self.repo / ".state" / "sessions.json").read_text())
        link = state["taskAssignments"][SID_A][0]   # canonical key is the Claude id
        self.assertEqual(link["taskId"], self.task["id"])
        store = self.server.get_store()
        self.assertTrue(store.events(task_id=self.task["id"], kind="link", limit=1))

    def test_legacy_assignments_are_resolved_once(self):
        store = self.server.get_store()
        store.update_task(self.other["id"], notion_task_id="3456d218-9ed6-8154-a81d-e75dfb0867a8")
        state = {"taskAssignments": {
            "X": {"taskFile": "/x/task-list.md", "notionTaskId": "3456d2189ed68154a81de75dfb0867a8",
                  "taskTitle": "Another row"},
            "Y": {"taskFile": "/y/task-list.md", "notionTaskId": "PENDING-NOTION-SYNC",
                  "taskTitle": "No such row"},
        }}
        self.assertTrue(self.server.resolve_task_assignments(state))
        self.assertEqual(state["taskAssignments"]["X"][0]["taskId"], self.other["id"])
        self.assertIsNone(state["taskAssignments"]["Y"][0]["taskId"])
        self.assertFalse(self.server.resolve_task_assignments(state), "not retried")


class TestOrcaLauncher(TaskViewCase):

    def _transcript(self, sid, age_days=1):
        d = self.repo / "projects" / "-fake-project"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{sid}.jsonl"
        f.write_text(json.dumps({"cwd": str(self.repo)}) + "\n")
        stamp = time.time() - age_days * 86400
        os.utime(f, (stamp, stamp))
        self.server.CLAUDE_PROJECTS_DIR = d.parent

    def test_resumes_the_conversation_that_touched_the_task(self):
        self._transcript(SID_A)
        status, d = self.post("/api/task/orca", {"taskId": self.task["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(d["resumed"], SID_A)
        self.assertEqual(len(self.launched), 1)
        launch = self.launched[0]
        self.assertEqual(launch["resume"], SID_A, "SID_B has no transcript on disk")
        self.assertEqual(launch["agent"], "orca")
        self.assertIn("proj#1", launch["prompt"])
        self.assertIn("worked on this task in this conversation before", launch["prompt"])
        self.assertIn("proj/brief.md", launch["prompt"])
        self.assertNotIn("mh task link", launch["prompt"], "a resumed conversation is already linked")

    def test_fresh_when_no_transcript_is_recent(self):
        self._transcript(SID_A, age_days=30)
        status, d = self.post("/api/task/orca", {"taskId": self.task["id"]})
        self.assertIsNone(d["resumed"])
        launch = self.launched[0]
        self.assertIsNone(launch["resume"])
        self.assertIn("Read these first", launch["prompt"])

    def test_only_a_linked_conversation_is_resumed(self):
        """A sweep writes to many tasks; its transcript is about none of them."""
        sid_c = "cccccccc-1111-2222-3333-444444444444"
        store = self.server.get_store()
        store._session = {**mhsession.EMPTY, "session_id": sid_c, "kind": "interactive"}
        store._session_recorded = False
        store.dispatch(self.task["id"], "iddy", actor="orca")     # wrote, never linked
        self._transcript(SID_A, age_days=2)
        self._transcript(SID_B, age_days=1)      # scheduled, wrote, never linked
        self._transcript(sid_c, age_days=0)      # newest write, never linked
        status, d = self.post("/api/task/orca", {"taskId": self.task["id"]})
        self.assertEqual(d["resumed"], SID_A)
        store.unlink_session(self.task["id"], actor="mq", session_id=SID_A)
        _, d = self.post("/api/task/orca", {"taskId": self.task["id"]})
        self.assertIsNone(d["resumed"], "nothing linked means a fresh conversation")
        self.assertIn("mh task link proj#1 --actor orca", self.launched[-1]["prompt"])

    def test_linked_conversation_wins_over_a_newer_write(self):
        sid_l = "dddddddd-1111-2222-3333-444444444444"
        store = self.server.get_store()
        store.link_session(self.task["id"], actor="mq", session_id=sid_l)
        store._session = {**mhsession.EMPTY, "session_id": SID_A, "kind": "interactive"}
        store.update_task(self.task["id"], actor="orca", notes="later")
        self._transcript(SID_A)
        self._transcript(sid_l)
        _, d = self.post("/api/task/orca", {"taskId": self.task["id"]})
        self.assertEqual(d["resumed"], sid_l)

    def test_no_brief_means_scope_and_start(self):
        status, d = self.post("/api/task/orca", {"taskId": self.other["id"]})
        self.assertEqual(status, 200)
        self.assertIn("/scope-and-start", self.launched[0]["prompt"])


class TestFrontEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.source = SERVER_PY.read_text()
        cls.js = "\n".join(re.findall(r"<script>\n(.*?)\n</script>", cls.source, re.S))

    def test_poll_does_not_redraw_over_an_open_answer_box(self):
        body = self.js.split("function renderTask()", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("querySelector('.answer-input')", body)
        self.assertIn("return", body.split("querySelector('.answer-input')", 1)[1][:40])

    def test_answer_box_sets_the_editing_flag(self):
        body = self.js.split("function startAnswer(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("isEditing = true", body)
        self.assertIn("isEditing = false", body)
        self.assertIn("e.key === 'Enter'", body)
        self.assertIn("e.key === 'Escape'", body)

    def test_task_page_only_redraws_on_change(self):
        body = self.js.split("async function fetchTask(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("lastTaskJson", body)

    def test_popup_is_routed_by_hash(self):
        self.assertIn("#task/", self.js)
        self.assertIn("hashchange", self.js)
        body = self.js.split("function closeTaskModal(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("hidden = true", body)

    def test_board_opens_the_popup(self):
        """The name opens the task; the circle checks it off."""
        self.assertIn('onclick="openTask(${item.id})"', self.js)
        self.assertIn("togglePriorityItemFromEl(this.parentElement)", self.js)
        self.assertIn("openTask(${p.next.id})", self.js)
        self.assertIn("openTask(${ta.taskId})", self.js)
        self.assertIn('onclick="openTask(${t.id})"', self.js, "strip chips")

    def test_linking_a_conversation_closes_the_picker_before_redrawing(self):
        """The open picker blocks redraws; leaving it open hid the new row."""
        body = self.js.split("async function linkConversation(", 1)[1].split("\nasync function ", 1)[0]
        self.assertLess(body.index("classList.remove('open')"), body.index("storeAction("))
        self.assertIn("fetchTask(true)", body)

    def test_escape_does_not_close_over_an_open_answer_box(self):
        self.assertIn("e.key === 'Escape' && openTaskId && !document.querySelector('#taskView .answer-input')", self.js)

    def test_scheduled_runs_show_on_the_task_page(self):
        """The board hides them; on a task page the sweep's work is the point."""
        body = self.js.split("function renderTask()", 1)[1].split("\nfunction ", 1)[0]
        self.assertNotIn("showAutomation", body)
        self.assertIn("scheduled", body)


if __name__ == "__main__":
    unittest.main()
