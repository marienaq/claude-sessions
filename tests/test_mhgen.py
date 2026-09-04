#!/usr/bin/env python3
"""
Tests for mhgen.

The important one is the round trip: store -> markdown -> store has to come
back with the same tasks. If the generator and the parser disagree, work
disappears on the first regeneration and nothing else in the system would
notice.
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mhgen
import mhmigrate
import mhstore

from test_mhmigrate import make_list


class GenCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "operations").mkdir(parents=True)
        self.store = mhstore.open_store(root=self.repo)
        self.store.add_project("proj", "A Project", dir="proj",
                               goal="Ship the thing.")
        (self.repo / "proj").mkdir()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def write_registry(self, keys=("proj",)):
        lines = ["# Project registry", "",
                 "| Key | Name | Task list | Status | Owner | Due |",
                 "|---|---|---|---|---|---|"]
        for k in keys:
            lines.append(f"| `{k}` | {k} | `{k}/task-list.md` | active | MQ | none |")
        (self.repo / "operations" / "project-registry.md").write_text(
            "\n".join(lines) + "\n")


class TestTaskListGeneration(GenCase):

    def test_hand_written_sections_survive(self):
        """
        Goal, Key Decisions and the rest are hand-written. A generator that
        rewrote the whole file would delete them.
        """
        path = self.repo / "proj" / "task-list.md"
        make_list(path, "proj", [{"n": 1, "task": "Existing"}],
                  extra="\n## Key Decisions\n\nSomething decided.\n\n"
                        "## Open Questions\n\nSomething unresolved.\n")
        self.store.add_task("proj", "Existing", display_ord="1", seq=10)

        mhgen.generate_task_list(self.store, self.repo, "proj")
        text = path.read_text()
        self.assertIn("## Goal", text)
        self.assertIn("## Key Decisions", text)
        self.assertIn("Something decided.", text)
        self.assertIn("## Open Questions", text)
        self.assertIn("Something unresolved.", text)

    def test_generated_marker_added_once(self):
        path = self.repo / "proj" / "task-list.md"
        make_list(path, "proj", [{"n": 1, "task": "Existing"}])
        self.store.add_task("proj", "Existing", display_ord="1")
        mhgen.generate_task_list(self.store, self.repo, "proj")
        mhgen.generate_task_list(self.store, self.repo, "proj")
        self.assertEqual(path.read_text().count("<!-- GENERATED"), 1)

    def test_rows_render_with_phase1_columns(self):
        path = self.repo / "proj" / "task-list.md"
        make_list(path, "proj", [{"n": 1, "task": "Existing"}])
        self.store.add_task("proj", "A task", display_ord="7", seq=20,
                            owner="jennifer", due="2026-09-30",
                            notion_task_id="abc-123", notes="One sentence.")
        text = mhgen.generate_task_list(self.store, self.repo, "proj")
        self.assertIn("| # | Task | Status | Notion Task ID | Notes | Owner | Due | Seq |",
                      text)
        row = next(l for l in text.splitlines() if "A task" in l)
        self.assertIn("| 7 |", row)
        self.assertIn("Jennifer", row)
        self.assertIn("2026-09-30", row)
        self.assertIn("`abc-123`", row)
        self.assertIn("One sentence.", row)

    def test_pipes_in_content_are_escaped(self):
        """An unescaped pipe silently shifts every later column."""
        path = self.repo / "proj" / "task-list.md"
        make_list(path, "proj", [{"n": 1, "task": "x"}])
        self.store.add_task("proj", "Compare A | B options", display_ord="1",
                            notes="First | second")
        text = mhgen.generate_task_list(self.store, self.repo, "proj")
        row = next(l for l in text.splitlines() if "Compare A" in l)
        self.assertIn(r"A \| B", row, "the pipe must be escaped")
        # And the parser must read it back as one cell, not two.
        cells = mhmigrate.split_cells(row)
        self.assertEqual(len(cells), 8)
        self.assertEqual(cells[1], "Compare A | B options")
        self.assertEqual(cells[4], "First | second")

    def test_open_rows_sort_before_finished(self):
        path = self.repo / "proj" / "task-list.md"
        make_list(path, "proj", [{"n": 1, "task": "x"}])
        self.store.add_task("proj", "Finished", display_ord="1", status="done", seq=5)
        self.store.add_task("proj", "Open", display_ord="2", seq=10)
        text = mhgen.generate_task_list(self.store, self.repo, "proj")
        self.assertLess(text.index("| Open "), text.index("| Finished "))

    def test_multiple_tables_keep_their_sections(self):
        path = self.repo / "proj" / "task-list.md"
        make_list(path, "proj", [{"n": 1, "task": "Phase one"}])
        with open(path, "a") as f:
            f.write("\n### Phase 2\n\n")
            f.write(mhgen.TASK_TABLE_HEADER + "\n")
            f.write(mhgen.TASK_TABLE_DIVIDER + "\n")
            f.write("| 2 | Phase two | Not started | none |  | MQ |  | 20 |\n")
        self.store.add_task("proj", "Phase one", display_ord="1", section="Tasks")
        self.store.add_task("proj", "Phase two", display_ord="2", section="Phase 2")

        text = mhgen.generate_task_list(self.store, self.repo, "proj")
        self.assertEqual(text.count(mhgen.TASK_TABLE_HEADER), 2,
                         "both tables must survive")
        phase2_at = text.index("### Phase 2")
        self.assertLess(text.index("Phase one"), phase2_at)
        self.assertGreater(text.index("Phase two"), phase2_at)

    def test_no_temp_file_left(self):
        path = self.repo / "proj" / "task-list.md"
        make_list(path, "proj", [{"n": 1, "task": "x"}])
        self.store.add_task("proj", "x", display_ord="1")
        mhgen.generate_task_list(self.store, self.repo, "proj")
        self.assertFalse(path.with_suffix(".md.tmp").exists())


class TestPrioritiesGeneration(GenCase):

    def setUp(self):
        super().setUp()
        self.monday = date.today() - timedelta(days=date.today().weekday())
        self.store.upsert_week(self.monday.isoformat(), locked_at="2026-08-31T10:00:00")
        self.store.add_task("proj", "Scheduled work", display_ord="1",
                            planned_day=self.monday.isoformat(),
                            status="planned", load="deep")
        self.store.add_task("proj", "Waiting on someone", display_ord="2",
                            status="waiting")
        self.store.add_task("proj", "In the backlog", display_ord="3",
                            status="backlog")
        self.store.add_task("proj", "A proposal", display_ord="4",
                            status="backlog", source="capture", confirmed=0)

    def test_hand_written_preamble_is_preserved(self):
        path = self.repo / "priorities.md"
        path.write_text(
            "# Mellonhead Priorities\n\n"
            "## Capacity Dashboard\n\n"
            "### Commitment Rules (v0.3)\n\n"
            "Rule 1: something MQ wrote.\n\n"
            "**Week of August 24, 2026**\n\n"
            "## Weekly Goals\n\n"
            "### Monday 8/24\n\n- [ ] old content\n")
        mhgen.generate_priorities(self.store, self.repo)
        text = path.read_text()
        self.assertIn("## Capacity Dashboard", text)
        self.assertIn("### Commitment Rules (v0.3)", text)
        self.assertIn("Rule 1: something MQ wrote.", text)
        self.assertNotIn("old content", text, "the week itself is regenerated")

    def test_a_proposed_week_does_not_evict_the_running_one(self):
        """
        Proposing next week on a Friday used to delete the running week from
        the file. The store still had it and the dashboard still showed it,
        but the markdown MQ works from lost Thursday and Friday mid-week.
        """
        nxt = self.monday + timedelta(days=7)
        self.store.upsert_week(nxt.isoformat(), proposed_at="2026-09-04T13:06:00")
        self.store.add_task("proj", "Next week work", display_ord="9",
                            planned_day=(nxt + timedelta(days=1)).isoformat(),
                            status="planned")
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")
        text = mhgen.generate_priorities(self.store, self.repo)

        self.assertIn("Scheduled work", text, "the running week must survive")
        self.assertIn("Next week work", text, "the proposal must appear")
        headers = [l for l in text.splitlines() if l.startswith("**Week of")]
        self.assertEqual(len(headers), 2)
        self.assertIn("not confirmed", headers[1])
        self.assertEqual(text.count("<!-- GENERATED"), 1,
                         "one boundary marker, not one per week")

    def test_only_the_current_week_when_nothing_is_proposed(self):
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")
        text = mhgen.generate_priorities(self.store, self.repo)
        self.assertEqual(len([l for l in text.splitlines()
                              if l.startswith("**Week of")]), 1)

    def test_sections_rendered(self):
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")
        text = mhgen.generate_priorities(self.store, self.repo)
        self.assertIn("## Weekly Goals", text)
        self.assertIn("### Blocked or waiting", text)
        self.assertIn("## Backlog", text)
        self.assertIn("## Proposed (unconfirmed)", text)
        self.assertIn("Waiting on someone", text)
        self.assertIn("In the backlog", text)
        self.assertIn("A proposal", text)

    def test_day_line_shape(self):
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")
        text = mhgen.generate_priorities(self.store, self.repo)
        line = next(l for l in text.splitlines() if "Scheduled work" in l)
        self.assertTrue(line.startswith("- [ ] **Scheduled work**"))
        self.assertIn("[Deep].", line)
        self.assertIn("`proj#1`", line, "the tag is what makes the checkbox exact")

    def test_done_marker(self):
        task = self.store.tasks(project_key="proj")[0]
        self.store.complete_task(task["id"], done_at="2026-09-01")
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")
        text = mhgen.generate_priorities(self.store, self.repo)
        line = next(l for l in text.splitlines() if "Scheduled work" in l)
        self.assertTrue(line.startswith("- [x]"))
        self.assertIn("(done 2026-09-01)", line)

    def test_locked_state_in_header(self):
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")
        text = mhgen.generate_priorities(self.store, self.repo)
        header = next(l for l in text.splitlines() if l.startswith("**Week of"))
        self.assertIn("confirmed", header)

    def test_proposed_state_in_header(self):
        self.store.conn.execute("UPDATE weeks SET locked_at = NULL, "
                                "proposed_at = '2026-08-28T13:00:00'")
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")
        text = mhgen.generate_priorities(self.store, self.repo)
        header = next(l for l in text.splitlines() if l.startswith("**Week of"))
        self.assertIn("not confirmed", header)


class TestRoundTrip(GenCase):
    """store -> markdown -> store must not lose or alter anything."""

    def _snapshot(self, store):
        return sorted(
            (t["project_key"], t["title"], t["status"], t["owner"],
             t["seq"], t["planned_day"], t["display_ord"])
            for t in store.tasks())

    def test_tasks_survive_generate_then_migrate(self):
        monday = date.today() - timedelta(days=date.today().weekday())
        make_list(self.repo / "proj" / "task-list.md", "proj",
                  [{"n": 1, "task": "placeholder"}])
        self.write_registry()
        for i, (title, status, seq) in enumerate([
                ("First task", "planned", 10),
                ("Second task", "in_progress", 20),
                ("Third task", "done", 30),
                ("Waiting task", "waiting", 40)], start=1):
            self.store.add_task("proj", title, display_ord=str(i), seq=seq,
                                status=status, section="Tasks",
                                planned_day=monday.isoformat() if i == 1 else None)
        self.store.upsert_week(monday.isoformat(), locked_at="2026-08-31T10:00:00")
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")

        before = self._snapshot(self.store)
        mhgen.generate_all(self.store, self.repo)
        mhmigrate.migrate(self.repo, self.store)
        after = self._snapshot(self.store)

        self.assertEqual(len(before), len(after), "no task may vanish")
        self.assertEqual([b[1] for b in before], [a[1] for a in after],
                         "titles must survive the round trip")
        self.assertEqual([b[2] for b in before], [a[2] for a in after],
                         "statuses must survive the round trip")
        self.assertEqual([b[4] for b in before], [a[4] for a in after],
                         "seq must survive the round trip")

    def test_planned_day_survives(self):
        monday = date.today() - timedelta(days=date.today().weekday())
        make_list(self.repo / "proj" / "task-list.md", "proj",
                  [{"n": 1, "task": "placeholder"}])
        self.write_registry()
        self.store.add_task("proj", "Scheduled", display_ord="1", seq=10,
                            status="planned", section="Tasks",
                            planned_day=monday.isoformat())
        self.store.upsert_week(monday.isoformat(), locked_at="2026-08-31T10:00:00")
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")

        mhgen.generate_all(self.store, self.repo)
        mhmigrate.migrate(self.repo, self.store)
        task = next(t for t in self.store.tasks() if t["title"] == "Scheduled")
        self.assertEqual(task["planned_day"], monday.isoformat())

    def test_repeated_cycles_converge(self):
        """
        generate -> migrate -> generate -> migrate must reach a fixed point.
        Each of these drifted at first: task rows duplicated because migration
        deleted and re-inserted rather than matching, and one-off rows
        duplicated because they have no key#N and were matched on nothing.
        Seventeen extra rows per run, growing without bound.
        """
        monday = date.today() - timedelta(days=date.today().weekday())
        make_list(self.repo / "proj" / "task-list.md", "proj",
                  [{"n": 1, "task": "placeholder"}])
        self.write_registry()
        self.store.add_task("proj", "Tracked", display_ord="1", seq=10,
                            status="planned", section="Tasks",
                            planned_day=monday.isoformat())
        self.store.add_task(mhstore.ONE_OFF, "An untracked errand",
                            status="planned", planned_day=monday.isoformat())
        self.store.upsert_week(monday.isoformat(), locked_at="2026-08-31T10:00:00")
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")

        counts = []
        for _ in range(3):
            mhgen.generate_all(self.store, self.repo)
            mhmigrate.migrate(self.repo, self.store)
            counts.append((
                len(self.store.tasks()),
                len(self.store.tasks(project_key=mhstore.ONE_OFF)),
                len([t for t in self.store.tasks() if t["planned_day"]]),
            ))
        self.assertEqual(counts[0], counts[1], "second cycle must not add rows")
        self.assertEqual(counts[1], counts[2], "third cycle must not add rows")

    def test_file_content_converges_not_just_row_counts(self):
        """
        The counts held at 224 for three cycles while the Notes cell grew
        every time: the generator writes "[note](x.md)", migration read it
        back as prose, and the next generation appended a second copy. A
        convergence check on row counts cannot see that. Compare the bytes.
        """
        monday = date.today() - timedelta(days=date.today().weekday())
        make_list(self.repo / "proj" / "task-list.md", "proj",
                  [{"n": 1, "task": "placeholder"}])
        self.write_registry()
        self.store.add_task("proj", "Has a note", display_ord="1", seq=10,
                            section="Tasks", notes="A short sentence.",
                            note_path="proj/task-notes/1-has-a-note.md",
                            planned_day=monday.isoformat(), status="planned")
        (self.repo / "proj" / "task-notes").mkdir(parents=True)
        (self.repo / "proj" / "task-notes" / "1-has-a-note.md").write_text(
            "# Has a note\n\n## History\n\nSomething long.\n")
        self.store.upsert_week(monday.isoformat(), locked_at="2026-08-31T10:00:00")
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")

        strip = lambda t: "\n".join(l for l in t.splitlines()
                                    if not l.startswith("<!-- GENERATED"))
        seen = []
        for _ in range(3):
            mhgen.generate_all(self.store, self.repo)
            mhmigrate.migrate(self.repo, self.store)
            seen.append((
                strip((self.repo / "proj" / "task-list.md").read_text()),
                strip((self.repo / "priorities.md").read_text()),
            ))
        self.assertEqual(seen[0], seen[1], "second cycle changed the files")
        self.assertEqual(seen[1], seen[2], "third cycle changed the files")

        row = next(l for l in seen[-1][0].splitlines() if "Has a note" in l)
        self.assertEqual(row.count("[note]"), 1, "one link, not a growing list")
        day = next(l for l in seen[-1][1].splitlines() if "Has a note" in l)
        self.assertEqual(day.count("Notes:"), 1)

    def test_ids_are_stable_across_migrations(self):
        """Note files are named <id>-<slug>.md, so a changed id orphans one."""
        make_list(self.repo / "proj" / "task-list.md", "proj",
                  [{"n": 1, "task": "placeholder"}])
        self.write_registry()
        self.store.add_task("proj", "Keeps its id", display_ord="1", seq=10,
                            section="Tasks")
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")
        mhgen.generate_all(self.store, self.repo)

        before = {t["title"]: t["id"] for t in self.store.tasks()}
        mhmigrate.migrate(self.repo, self.store)
        after = {t["title"]: t["id"] for t in self.store.tasks()}
        self.assertEqual(before["Keeps its id"], after["Keeps its id"])

    def test_generation_is_stable(self):
        """Generating twice with no writes between must not change the file."""
        make_list(self.repo / "proj" / "task-list.md", "proj",
                  [{"n": 1, "task": "placeholder"}])
        self.store.add_task("proj", "Steady", display_ord="1", seq=10,
                            section="Tasks")
        first = mhgen.generate_task_list(self.store, self.repo, "proj")
        second = mhgen.generate_task_list(self.store, self.repo, "proj")
        strip = lambda t: "\n".join(l for l in t.splitlines()
                                    if not l.startswith("<!-- GENERATED"))
        self.assertEqual(strip(first), strip(second))


class TestDashboard(GenCase):

    def test_dashboard_written(self):
        self.store.add_task("proj", "Next thing", display_ord="1", seq=10)
        mhgen.generate_dashboard(self.store, self.repo)
        md = (self.repo / "operations" / "projects-dashboard.md").read_text()
        self.assertIn("# Projects dashboard", md)
        self.assertIn("`proj`", md)
        self.assertIn("Next thing", md)
        js = (self.repo / "operations" / "projects-dashboard.json").read_text()
        self.assertIn('"openCount"', js)

    def test_archived_projects_separated(self):
        self.store.add_project("dead", "Dead", dir="dead", status="cancelled")
        mhgen.generate_dashboard(self.store, self.repo)
        md = (self.repo / "operations" / "projects-dashboard.md").read_text()
        self.assertIn("## Archived", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
