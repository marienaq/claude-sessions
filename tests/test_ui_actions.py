#!/usr/bin/env python3
"""
Tests for the 2.4 UI actions: the panels payload, and a syntax guard on the
inline front-end code.

The JS guard exists because the page is one big inline <script>. A single
syntax error there does not degrade the page, it blanks it: nothing renders
at all and the server still returns 200, so it looks healthy from the
outside. That has happened once already.
"""

import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mhstore

SERVER_PY = Path(__file__).resolve().parent.parent / "server.py"


class TestInlineFrontEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.source = SERVER_PY.read_text()

    def _scripts(self):
        return re.findall(r"<script>\n(.*?)\n</script>", self.source, re.S)

    def test_inline_script_present(self):
        self.assertTrue(self._scripts(), "no inline <script> found")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_inline_script_parses(self):
        """A syntax error here blanks the whole dashboard."""
        for i, js in enumerate(self._scripts()):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(js)
                path = f.name
            try:
                result = subprocess.run(["node", "--check", path],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0,
                                 f"script {i} does not parse:\n{result.stderr}")
            finally:
                os.unlink(path)

    def test_open_quick_add_is_protected_from_redraw(self):
        """
        The page polls every 5 seconds and redraws by replacing innerHTML,
        which destroys an open quick-add box and whatever was typed into it.
        The isEditing flag alone was not enough: a document-level click
        listener clears it whenever the tag overlay is hidden, and it runs
        after the handler that opens the box, so the flag was already false
        by the time the poll fired.

        Two guards, both asked of the DOM rather than a flag. If either is
        removed, typing a task name becomes impossible again.
        """
        js = "\n".join(self._scripts())
        render = js[js.index("function renderPriorities()"):]
        render = render[:render.index("\n}")]
        self.assertIn("quick-add-input", render,
                      "renderPriorities must not redraw over an open input")

        watchdog = js[js.index("document.addEventListener('click'"):]
        watchdog = watchdog[:watchdog.index("});") + 3]
        self.assertIn("quick-add-input", watchdog,
                      "the isEditing watchdog must not clear an open quick-add")

    def test_quick_add_keeps_text_on_blur(self):
        """Losing a half-written task to an incidental focus change is worse
        than saving one the user meant to abandon; Escape still discards."""
        js = "\n".join(self._scripts())
        block = js[js.index("function startQuickAdd"):]
        block = block[:block.index("\n}\n")]
        self.assertIn("input.onblur = () => finish(true)", block)
        self.assertIn("Escape", block)

    def test_every_onclick_handler_exists(self):
        """
        An onclick naming a function that was renamed away throws at click
        time and silently does nothing.
        """
        js = "\n".join(self._scripts())
        defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", js))
        defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", js))
        called = set(re.findall(r'on(?:click|dragstart|dragover|dragleave|drop|keydown)="([A-Za-z_$][\w$]*)\(',
                                self.source))
        builtins = {"event", "window", "document"}
        missing = {c for c in called if c not in defined and c not in builtins}
        self.assertEqual(missing, set(), f"handlers referenced but not defined: {missing}")


class TestPanels(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "operations").mkdir(parents=True)
        self._prev = os.environ.get("MELLONHEAD_ROOT")
        os.environ["MELLONHEAD_ROOT"] = str(self.repo)
        os.environ["CSM_STATE_DIR"] = str(self.repo / ".state")

        self.store = mhstore.open_store(root=self.repo)
        self.store.add_project("proj", "A Project", dir="proj")
        self.store.add_project("parked", "Parked", dir="parked", status="parked")
        monday = date.today() - timedelta(days=date.today().weekday())
        self.store.add_task("proj", "Scheduled", planned_day=monday.isoformat(),
                            status="planned", display_ord="1")
        self.store.add_task("proj", "In the backlog", status="backlog",
                            display_ord="2", seq=10)
        self.store.add_task("proj", "A capture proposal", status="backlog",
                            source="capture", confirmed=0, display_ord="3")
        self.store.add_task("parked", "Parked work", status="backlog",
                            display_ord="1")

    def tearDown(self):
        try:
            import server
            if getattr(server, "_STORE", None):
                server._STORE.close()
                server._STORE = None
        except Exception:
            pass
        self.store.close()
        if self._prev is None:
            os.environ.pop("MELLONHEAD_ROOT", None)
        else:
            os.environ["MELLONHEAD_ROOT"] = self._prev
        self._tmp.cleanup()

    def load(self):
        import server
        importlib.reload(server)
        server._STORE = None
        return server

    def test_backlog_and_proposals_separated(self):
        panels = self.load().load_panels()
        self.assertTrue(panels["available"])
        backlog = [i["text"] for i in panels["backlog"]]
        proposed = [i["text"] for i in panels["proposed"]]
        self.assertIn("In the backlog", backlog)
        self.assertIn("A capture proposal", proposed)
        self.assertNotIn("A capture proposal", backlog)

    def test_scheduled_work_is_not_in_the_backlog(self):
        panels = self.load().load_panels()
        self.assertNotIn("Scheduled", [i["text"] for i in panels["backlog"]])

    def test_projects_exclude_one_off_and_archived(self):
        panels = self.load().load_panels()
        keys = [p["key"] for p in panels["projects"]]
        self.assertIn("proj", keys)
        self.assertNotIn(mhstore.ONE_OFF, keys)

    def test_parked_project_listed_after_active(self):
        """Parked work stays visible but never sorts above live work."""
        self.store.update_project("parked", status="waiting")
        panels = self.load().load_panels()
        keys = [p["key"] for p in panels["projects"]]
        self.assertLess(keys.index("proj"), keys.index("parked"))

    def test_project_next_action(self):
        panels = self.load().load_panels()
        proj = next(p for p in panels["projects"] if p["key"] == "proj")
        self.assertIsNotNone(proj["next"])
        self.assertEqual(proj["openCount"], 3)

    def test_obsidian_url_built_for_notes(self):
        server = self.load()
        url = server.obsidian_url("proj/task-notes/1-thing.md")
        self.assertTrue(url.startswith("obsidian://open?path="))
        self.assertIn("task-notes", urllib_unquote(url))
        self.assertIsNone(server.obsidian_url(None))

    def test_panels_empty_without_a_store(self):
        self.store.close()
        (self.repo / "operations" / "tasks.db").unlink()
        panels = self.load().load_panels()
        self.assertFalse(panels["available"])
        self.assertEqual(panels["backlog"], [])


def urllib_unquote(text):
    import urllib.parse
    return urllib.parse.unquote(text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
