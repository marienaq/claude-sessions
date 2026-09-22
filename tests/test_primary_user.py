#!/usr/bin/env python3
"""
The primary user: whose store it is. MQ's store predates the setting and must
read exactly as before; anyone else's must never mention MQ.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mhcli
import mhgen
import mhstore
from mhstore import StoreError


class TestLegacyStore(unittest.TestCase):
    """No setting at all: MQ's live store, unchanged."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = mhstore.open_store(root=Path(self._tmp.name))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_falls_back_to_mq(self):
        self.assertEqual(self.store.primary_user, "mq")
        self.assertEqual(self.store.primary_name, "MQ")
        self.assertEqual(self.store.normalize_owner("Mariena"), "mq")
        self.assertEqual(self.store.normalize_owner(""), "mq")
        self.assertEqual(self.store.project(mhstore.ONE_OFF)["owner"], "mq")

    def test_no_slack_channel_inherited(self):
        self.assertEqual(self.store.setting("slack_channel"), "")


class TestNamedStore(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = mhstore.open_store(root=self.root, primary_user="alex")
        self.store.set_primary_user("alex", name="Alex", aliases=["Alex Kim"])
        self.store.add_project("demo", "Demo", dir="demo")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_one_off_belongs_to_the_user(self):
        self.assertEqual(self.store.project(mhstore.ONE_OFF)["owner"], "alex")

    def test_owner_defaults_and_folds(self):
        t = self.store.add_task("demo", "Thing")
        self.assertEqual(t["owner"], "alex")
        t = self.store.add_task("demo", "Other", owner="Alex Kim")
        self.assertEqual(t["owner"], "alex")
        # MQ's aliases mean nothing here.
        t = self.store.add_task("demo", "Third", owner="Mariena")
        self.assertEqual(t["owner"], "mariena")

    def test_next_task_is_the_users(self):
        self.store.add_task("demo", "Theirs", owner="sam", seq=1)
        mine = self.store.add_task("demo", "Mine", seq=2)
        self.assertEqual(self.store.next_task("demo")["id"], mine["id"])

    def test_markdown_names_the_user(self):
        self.store.add_task("demo", "Thing", status="review")
        (self.root / "demo").mkdir()
        (self.root / "demo" / "task-list.md").write_text(
            f"# Demo\n\n{mhgen.TASK_TABLE_HEADER}\n{mhgen.TASK_TABLE_DIVIDER}\n")
        mhgen.generate_task_list(self.store, self.root, "demo")
        text = (self.root / "demo" / "task-list.md").read_text()
        self.assertIn("Ready for Alex review", text)
        self.assertNotIn("MQ", text)

    def test_rename_moves_owned_rows(self):
        t = self.store.add_task("demo", "Thing")
        other = self.store.add_task("demo", "Theirs", owner="sam")
        self.store.set_primary_user("alexk", name="Alex")
        self.assertEqual(self.store.task(t["id"])["owner"], "alexk")
        self.assertEqual(self.store.task(other["id"])["owner"], "sam")
        self.assertEqual(self.store.project("demo")["owner"], "alexk")

    def test_display_name_cannot_carry_markup(self):
        """It lands in the dashboard's HTML and in markdown table cells."""
        self.store.set_primary_user("alex", name='"><img src=x onerror=alert(1)>|\nX')
        name = self.store.primary_name
        for bad in '<>"|\n`\'':
            self.assertNotIn(bad, name)
        self.store.set_primary_user("alex", name="x" * 100)
        self.assertEqual(len(self.store.primary_name), 40)
        self.store.set_primary_user("alex", name="<>")
        self.assertEqual(self.store.primary_name, "Alex")

    def test_bad_ids_refused(self):
        for bad in ("", "Alex Kim", "1alex", "a" * 40):
            with self.assertRaises(StoreError):
                self.store.set_primary_user(bad)


class TestInit(unittest.TestCase):

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mhcli.main(list(argv))
        return code, buf.getvalue()

    def test_init_creates_a_named_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "ws"
            code, out = self.run_cli("--repo", str(repo), "init",
                                     "--user", "alex", "--name", "Alex")
            self.assertEqual(code, 0, out)
            self.assertTrue((repo / "operations" / "tasks.db").exists())
            code, out = self.run_cli("--repo", str(repo), "project", "add",
                                     "demo", "Demo", "--dir", "demo")
            self.assertEqual(code, 0, out)
            code, out = self.run_cli("--repo", str(repo), "task", "add",
                                     "demo", "Hello")
            self.assertEqual(code, 0)
            listing = (repo / "demo" / "task-list.md").read_text()
            self.assertIn("| Hello |", listing)
            self.assertIn("| Alex |", listing)
            store = mhstore.open_store(root=repo, seed_settings=False)
            try:
                self.assertEqual(store.primary_user, "alex")
                self.assertEqual(store.tasks()[0]["owner"], "alex")
            finally:
                store.close()

    def test_init_on_existing_store_needs_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.run_cli("--repo", str(repo), "init", "--user", "alex")
            with redirect_stdout(io.StringIO()):
                code = mhcli.main(["--repo", str(repo), "init"])
            self.assertEqual(code, 1)

    def test_reference_is_personalized(self):
        text = mhcli.render_reference("alex", "Alex")
        self.assertNotIn("MQ", text)
        self.assertIn("ask Alex something", text)
        self.assertEqual(mhcli.render_reference(),
                         mhcli.render_reference("mq", "MQ"))


if __name__ == "__main__":
    unittest.main()
