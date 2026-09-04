#!/usr/bin/env python3
"""
Tests for server.py's store-backed read paths (workstream 2.3).

server.py resolves MELLONHEAD_ROOT at import, so each case points the env at
a fresh fixture repo and reloads the module.
"""

import importlib
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mhstore


def _monday(day=None):
    day = day or date.today()
    return day - timedelta(days=day.weekday())


class ServerCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "operations").mkdir(parents=True, exist_ok=True)
        self._prev_root = os.environ.get("MELLONHEAD_ROOT")
        self._prev_state = os.environ.get("CSM_STATE_DIR")
        os.environ["MELLONHEAD_ROOT"] = str(self.repo)
        os.environ["CSM_STATE_DIR"] = str(self.repo / ".state")
        self.store = None

    def tearDown(self):
        # server.py opens its own handle; close it too or the temp dir is
        # removed with the database still mapped.
        try:
            import server
            if getattr(server, "_STORE", None):
                server._STORE.close()
                server._STORE = None
        except Exception:
            pass
        if self.store:
            self.store.close()
        for key, prev in (("MELLONHEAD_ROOT", self._prev_root),
                          ("CSM_STATE_DIR", self._prev_state)):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        self._tmp.cleanup()

    def build_store(self):
        self.store = mhstore.open_store(root=self.repo)
        return self.store

    def load_server(self):
        import server
        importlib.reload(server)
        server._STORE = None          # drop any handle from a previous case
        return server


class TestPrioritiesFromStore(ServerCase):

    def setUp(self):
        super().setUp()
        store = self.build_store()
        store.add_project("proj", "A Project", dir="proj")
        self.monday = _monday()
        self.a = store.add_task("proj", "First thing", display_ord="1", seq=10,
                                planned_day=self.monday.isoformat(),
                                status="planned", load="deep")
        self.b = store.add_task("proj", "Second thing", display_ord="2", seq=20,
                                planned_day=(self.monday + timedelta(days=1)).isoformat(),
                                status="done")
        store.add_task("proj", "Blocked thing", display_ord="3", status="waiting")
        store.add_task("proj", "Backlog thing", display_ord="4", status="backlog")
        store.upsert_week(self.monday.isoformat(), notes="Week of testing")

    def test_reads_from_the_store(self):
        server = self.load_server()
        self.assertTrue(server.store_is_live())
        self.assertEqual(server.load_priorities()["source"], "store")

    def test_days_grouped_and_labelled(self):
        p = self.load_server().load_priorities()
        days = p["weeks"][0]["days"]
        self.assertEqual(len(days), 2, "only days with work appear")
        self.assertTrue(days[0]["day"].startswith("Monday"))
        self.assertEqual(days[0]["items"][0]["text"], "First thing")

    def test_items_carry_id_and_key(self):
        p = self.load_server().load_priorities()
        item = p["weeks"][0]["days"][0]["items"][0]
        self.assertEqual(item["id"], self.a["id"])
        self.assertEqual(item["key"], "proj#1")
        self.assertEqual(item["project"], "proj")
        self.assertEqual(item["load"], "deep")
        self.assertFalse(item["done"])

    def test_done_state_reflected(self):
        p = self.load_server().load_priorities()
        tuesday = p["weeks"][0]["days"][1]
        self.assertTrue(tuesday["items"][0]["done"])

    def test_week_title_from_week_row(self):
        p = self.load_server().load_priorities()
        self.assertEqual(p["weeks"][0]["title"], "Week of testing")
        self.assertTrue(p["weeks"][0]["isCurrent"])
        self.assertFalse(p["noCurrentWeek"])

    def test_blocked_listed_without_a_day(self):
        p = self.load_server().load_priorities()
        titles = [b["text"] for b in p["blocked"]]
        self.assertIn("Blocked thing", titles)
        self.assertNotIn("Backlog thing", titles)

    def test_next_week_included(self):
        nxt = self.monday + timedelta(days=8)
        self.store.add_task("proj", "Next week thing", display_ord="9",
                            planned_day=nxt.isoformat(), status="planned")
        p = self.load_server().load_priorities()
        self.assertEqual(len(p["weeks"]), 2)
        self.assertFalse(p["weeks"][1]["isCurrent"])


class TestStoreHandle(ServerCase):

    def test_reopens_when_the_database_is_replaced(self):
        """
        A long-lived handle keeps writing to the old inode after tasks.db is
        swapped out. SQLite reports success and the row never changes; three
        real checkbox clicks were lost that way, with an audit trail saying
        they had happened.
        """
        store = self.build_store()
        store.add_project("proj", "P", dir="proj")
        task = store.add_task("proj", "Original")
        store.close()

        server = self.load_server()
        first = server.get_store()
        self.assertIsNotNone(first)

        # Replace the file, the way a rebuild or a restore does.
        (self.repo / "operations" / "tasks.db").unlink()
        for sidecar in ("tasks.db-wal", "tasks.db-shm"):
            p = self.repo / "operations" / sidecar
            if p.exists():
                p.unlink()
        fresh = mhstore.open_store(root=self.repo)
        fresh.add_project("proj", "P", dir="proj")
        fresh.add_task("proj", "Replacement")
        fresh.close()

        second = server.get_store()
        titles = [t["title"] for t in second.tasks()]
        self.assertIn("Replacement", titles,
                      "the server must read the file that is on disk now")
        self.assertNotIn("Original", titles)

    def test_same_file_is_not_reopened(self):
        store = self.build_store()
        store.add_project("proj", "P", dir="proj")
        store.add_task("proj", "Steady")
        store.close()
        server = self.load_server()
        self.assertIs(server.get_store(), server.get_store())


class TestFallback(ServerCase):

    def test_markdown_used_when_store_absent(self):
        (self.repo / "priorities.md").write_text(
            "**Week of August 24, 2026**\n\n"
            "## Weekly Goals\n\n"
            "### Monday 8/24\n\n"
            "- [ ] Something from markdown\n")
        server = self.load_server()
        self.assertFalse(server.store_is_live())
        self.assertEqual(server.load_priorities()["source"], "markdown")

    def test_empty_store_falls_back(self):
        """A store with no planned work must not blank the dashboard."""
        store = self.build_store()
        store.add_project("proj", "A Project", dir="proj")
        store.add_task("proj", "Unscheduled", display_ord="1")
        (self.repo / "priorities.md").write_text(
            "**Week of August 24, 2026**\n\n## Weekly Goals\n\n"
            "### Monday 8/24\n\n- [ ] From markdown\n")
        server = self.load_server()
        self.assertFalse(server.store_is_live())
        self.assertEqual(server.load_priorities()["source"], "markdown")


class TestProjectLookup(ServerCase):

    def setUp(self):
        super().setUp()
        store = self.build_store()
        store.add_project("outer", "Outer", dir="course/outer")
        store.add_project("inner", "Inner", dir="course/outer/inner")
        (self.repo / "course" / "outer" / "inner").mkdir(parents=True)
        store.add_task("inner", "Inner task", display_ord="1", seq=10)
        store.add_task("outer", "Outer task", display_ord="1", seq=10)

    def test_deepest_directory_wins(self):
        server = self.load_server()
        found = server.project_for_cwd(str(self.repo / "course/outer/inner"))
        self.assertEqual(found["key"], "inner")

    def test_parent_directory_matches_outer(self):
        server = self.load_server()
        found = server.project_for_cwd(str(self.repo / "course/outer"))
        self.assertEqual(found["key"], "outer")

    def test_unrelated_directory_matches_nothing(self):
        server = self.load_server()
        self.assertIsNone(server.project_for_cwd("/tmp"))

    def test_task_list_payload_shape(self):
        server = self.load_server()
        payload = server.task_list_from_store("inner")
        self.assertEqual(payload["projectKey"], "inner")
        self.assertEqual(payload["projectName"], "Inner")
        self.assertEqual(payload["source"], "store")
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertEqual(payload["nextStep"]["title"], "Inner task")

    def test_unknown_project_returns_none(self):
        server = self.load_server()
        self.assertIsNone(server.task_list_from_store("nope"))


class TestSessionMatching(ServerCase):

    def test_project_key_beats_keyword_guess(self):
        server = self.load_server()
        item = {"project": "aba-academy", "text": "Some unrelated words"}
        session = {"taskList": {"projectKey": "aba-academy"}}
        other = {"taskList": {"projectKey": "website"}}
        self.assertTrue(server.match_priority_to_session(item, session, {}))
        self.assertFalse(server.match_priority_to_session(item, other, {}))

    def test_one_off_falls_through_to_the_old_matching(self):
        server = self.load_server()
        item = {"project": "one-off", "text": "prioritization rebuild store"}
        session = {"taskList": {"projectKey": "website"},
                   "name": "prioritization rebuild store work", "cwd": "",
                   "allTags": []}
        # Not short-circuited to False by the project rule; the keyword path runs.
        self.assertTrue(server.match_priority_to_session(item, session, {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
