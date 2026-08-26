#!/usr/bin/env python3
"""
Tests for mhmigrate, against a fixture repo built per test.

The fixtures reproduce the shapes that actually caused trouble in the real
repo: multiple task tables under sub-headings, ragged rows, a narrative-first
list, a byte-identical duplicate, and a 6KB Notes cell.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mhstore
import mhmigrate


HEADER = """# {title}

```yaml
key: {key}
name: {name}
status: {status}
owner: MQ
due: {due}
notion_project_id: {notion}
milestones:
```

## Goal
{goal}

## Tasks

| # | Task | Status | Notion Task ID | Notes | Owner | Due | Seq |
|---|------|--------|----------------|-------|-------|-----|-----|
"""

ROW = "| {n} | {task} | {status} | {notion} | {notes} | {owner} | {due} | {seq} |\n"


def make_list(path, key, rows, name=None, status="active", due="none",
              notion="none", goal="A goal.", title=None, extra=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = HEADER.format(title=title or key, key=key, name=name or key,
                         status=status, due=due, notion=notion, goal=goal)
    for r in rows:
        text += ROW.format(
            n=r.get("n", 1), task=r.get("task", "A task"),
            status=r.get("status", "Not started"), notion=r.get("notion", "none"),
            notes=r.get("notes", ""), owner=r.get("owner", "MQ"),
            due=r.get("due", ""), seq=r.get("seq", ""))
    text += extra
    path.write_text(text)


class MigrationCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "operations").mkdir(parents=True, exist_ok=True)
        self.registry_rows = []
        self._stores = []

    def tearDown(self):
        for store in self._stores:
            store.close()
        self._tmp.cleanup()

    def register(self, key, rel_path, name=None):
        self.registry_rows.append((key, name or key, rel_path))

    def write_registry(self):
        lines = ["# Project registry", "",
                 "| Key | Name | Task list | Status | Owner | Due | Context files |",
                 "|---|---|---|---|---|---|---|"]
        for key, name, path in self.registry_rows:
            lines.append(f"| `{key}` | {name} | `{path}` | active | MQ | none |  |")
        (self.repo / "operations" / "project-registry.md").write_text(
            "\n".join(lines) + "\n")

    def run_migration(self, **kwargs):
        self.write_registry()
        store = mhstore.open_store(root=self.repo)
        self._stores.append(store)
        report = mhmigrate.migrate(self.repo, store, **kwargs)
        return store, report


class TestBasicMigration(MigrationCase):

    def test_project_and_tasks_created(self):
        make_list(self.repo / "proj" / "task-list.md", "proj",
                  [{"n": 1, "task": "First", "status": "Not started", "seq": 10},
                   {"n": 2, "task": "Second", "status": "Done"}],
                  name="Proj Name", goal="Ship the thing.")
        self.register("proj", "proj/task-list.md")
        store, report = self.run_migration()

        self.assertEqual(report.projects, 1)
        self.assertEqual(report.tasks, 2)
        project = store.project("proj")
        self.assertEqual(project["name"], "Proj Name")
        self.assertEqual(project["dir"], "proj")
        self.assertEqual(project["goal"], "Ship the thing.")

        titles = [t["title"] for t in store.tasks(project_key="proj")]
        self.assertIn("First", titles)
        self.assertIn("Second", titles)

    def test_status_normalized_and_raw_kept(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "T", "status": "**Done 8/24 (a day early)**"}])
        self.register("p", "p/task-list.md")
        store, _ = self.run_migration()
        task = store.tasks(project_key="p")[0]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["status_raw"], "**Done 8/24 (a day early)**")
        self.assertEqual(task["done_at"], "2026-08-24")

    def test_arrow_stripped_and_flagged(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "→ Do this next", "seq": 10}])
        self.register("p", "p/task-list.md")
        store, _ = self.run_migration()
        task = store.tasks(project_key="p")[0]
        self.assertEqual(task["title"], "Do this next")
        self.assertEqual(task["is_next"], 1)

    def test_strikethrough_is_done(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "~~Old work~~", "status": "Not started"}])
        self.register("p", "p/task-list.md")
        store, _ = self.run_migration()
        task = store.tasks(project_key="p")[0]
        self.assertEqual(task["status"], "done",
                         "a struck-through title overrides the Status cell")
        self.assertEqual(task["title"], "Old work")

    def test_none_sentinels_become_null(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "T", "notion": "none", "due": ""}],
                  due="none", notion="none")
        self.register("p", "p/task-list.md")
        store, _ = self.run_migration()
        project, task = store.project("p"), store.tasks(project_key="p")[0]
        self.assertIsNone(project["due"])
        self.assertIsNone(project["notion_project_id"])
        self.assertIsNone(task["notion_task_id"])
        self.assertIsNone(task["due"])

    def test_owner_lowercased(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "A", "owner": "MQ"},
                   {"n": 2, "task": "B", "owner": "Jennifer"}])
        self.register("p", "p/task-list.md")
        store, _ = self.run_migration()
        owners = sorted(t["owner"] for t in store.tasks(project_key="p"))
        self.assertEqual(owners, ["jennifer", "mq"])

    def test_display_ord_kept_for_non_integer(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": "3-17", "task": "A range"}, {"n": "18b", "task": "A suffix"}])
        self.register("p", "p/task-list.md")
        store, _ = self.run_migration()
        ords = sorted(t["display_ord"] for t in store.tasks(project_key="p"))
        self.assertEqual(ords, ["18b", "3-17"])


class TestSections(MigrationCase):

    def test_sub_headings_captured(self):
        """Three phase tables must not flatten into one undifferentiated list."""
        path = self.repo / "p" / "task-list.md"
        make_list(path, "p", [{"n": 1, "task": "Phase one work", "seq": 10}])
        with open(path, "a") as f:
            f.write("\n### Phase 2: Build\n\n")
            f.write("| # | Task | Status | Notion Task ID | Notes | Owner | Due | Seq |\n")
            f.write("|---|------|--------|----------------|-------|-------|-----|-----|\n")
            f.write("| 2 | Phase two work |  Not started | none |  | MQ |  | 20 |\n")
        self.register("p", "p/task-list.md")
        store, _ = self.run_migration()
        by_title = {t["title"]: t for t in store.tasks(project_key="p")}
        self.assertEqual(by_title["Phase two work"]["section"], "Phase 2: Build")
        self.assertEqual(by_title["Phase one work"]["section"], "Tasks")

    def test_non_task_table_ignored(self):
        """A ticket or workshop-status table is not a task table."""
        path = self.repo / "p" / "task-list.md"
        make_list(path, "p", [{"n": 1, "task": "Real task"}])
        with open(path, "a") as f:
            f.write("\n### Copilot change validation tickets\n\n")
            f.write("| # | Ticket | Status | Notion Task ID | Time box |\n")
            f.write("|---|--------|--------|----------------|----------|\n")
            f.write("| 0 | Not a task | Not Started | abc | 2 hrs |\n")
        self.register("p", "p/task-list.md")
        store, report = self.run_migration()
        titles = [t["title"] for t in store.tasks(project_key="p")]
        self.assertEqual(titles, ["Real task"])
        self.assertNotIn("Not a task", titles)


class TestNoteFiles(MigrationCase):

    def test_long_notes_extracted(self):
        long_note = "This is a sentence about the work. " * 12   # >200 chars
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "Big", "notes": long_note},
                   {"n": 2, "task": "Small", "notes": "Short note."}])
        self.register("p", "p/task-list.md")
        store, report = self.run_migration()

        self.assertEqual(report.notes_written, 1, "only the long note extracts")
        tasks = {t["title"]: t for t in store.tasks(project_key="p")}
        self.assertIsNotNone(tasks["Big"]["note_path"])
        self.assertIsNone(tasks["Small"]["note_path"])

        note = self.repo / tasks["Big"]["note_path"]
        self.assertTrue(note.exists())
        self.assertIn(long_note.strip()[:80], note.read_text(),
                      "note text must be copied verbatim, not truncated")
        self.assertIn("task-notes", tasks["Big"]["note_path"])

    def test_note_count_matches_long_cells(self):
        rows = [{"n": i, "task": f"T{i}", "notes": "Long sentence here. " * 15}
                for i in range(1, 6)]
        rows.append({"n": 6, "task": "T6", "notes": "tiny"})
        make_list(self.repo / "p" / "task-list.md", "p", rows)
        self.register("p", "p/task-list.md")
        _, report = self.run_migration()
        self.assertEqual(report.notes_written, 5)

    def test_narrative_sections_migrate(self):
        """The ken-yarmosh shape: real content lives outside the table."""
        narrative = (
            "\n## 1. First task\n\n"
            "**Owed to Ken:** the whole story lives here, not in the table.\n"
            "- MQ: and my inline response is here too.\n"
        )
        make_list(self.repo / "ky" / "task-list.md", "ky",
                  [{"n": 1, "task": "First task", "notes": "Detail in section 1 below."}],
                  extra=narrative)
        self.register("ky", "ky/task-list.md")
        store, report = self.run_migration()

        task = store.tasks(project_key="ky")[0]
        self.assertIsNotNone(task["note_path"],
                             "a narrative section must produce a note file "
                             "even when the Notes cell is short")
        body = (self.repo / task["note_path"]).read_text()
        self.assertIn("the whole story lives here", body)
        self.assertIn("MQ: and my inline response", body)


class TestDuplicatesAndAnomalies(MigrationCase):

    def test_identical_files_deduplicated(self):
        rows = [{"n": 1, "task": "Same task"}]
        make_list(self.repo / "a" / "task-list.md", "twg", rows)
        (self.repo / "b").mkdir()
        (self.repo / "b" / "task-list.md").write_text(
            (self.repo / "a" / "task-list.md").read_text())
        self.register("twg-a", "a/task-list.md")
        self.register("twg-b", "b/task-list.md")
        store, report = self.run_migration()
        self.assertEqual(len(report.duplicates), 1)
        self.assertEqual(report.projects, 1, "the duplicate must not migrate twice")

    def test_ragged_row_reported_not_silently_dropped(self):
        path = self.repo / "p" / "task-list.md"
        make_list(path, "p", [{"n": 1, "task": "Good row"}])
        with open(path, "a") as f:
            f.write("| 2 | Ragged | Not started | none | notes |\n")   # 5 of 8
        self.register("p", "p/task-list.md")
        store, report = self.run_migration()
        self.assertEqual(len(report.malformed), 1)
        self.assertEqual(report.malformed[0][2], 5)
        self.assertEqual(report.malformed[0][3], 8)

    def test_row_missing_leading_pipe_is_reported(self):
        """
        A hand-edit that strips the leading pipe makes a row invisible: it is
        not a table row, so it is not even malformed. Work disappears with no
        error anywhere. This happened for real; it must never be silent.
        """
        path = self.repo / "p" / "task-list.md"
        make_list(path, "p", [{"n": 1, "task": "Good row"}])
        with open(path, "a") as f:
            f.write(" 2 | Orphaned row | Not started | none |  | MQ |  | 20 \n")
        self.register("p", "p/task-list.md")
        store, report = self.run_migration()

        self.assertEqual(len(report.orphan_rows), 1,
                         "a row without its leading pipe must be reported")
        self.assertIn("Orphaned row", report.orphan_rows[0][2])
        self.assertEqual(report.tasks, 1)

    def test_normal_prose_is_not_mistaken_for_an_orphan_row(self):
        path = self.repo / "p" / "task-list.md"
        make_list(path, "p", [{"n": 1, "task": "Good row"}])
        with open(path, "a") as f:
            f.write("\nSome prose about A | B choices and other things.\n")
            f.write("\n> A quote with a | pipe in it.\n")
        self.register("p", "p/task-list.md")
        store, report = self.run_migration()
        self.assertEqual(report.orphan_rows, [])

    def test_non_iso_due_reported_and_dropped(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "Standing item", "due": "recurring"}])
        self.register("p", "p/task-list.md")
        store, report = self.run_migration()
        self.assertEqual(len(report.non_iso_dates), 1)
        self.assertIsNone(store.tasks(project_key="p")[0]["due"])

    def test_unregistered_list_reported(self):
        make_list(self.repo / "reg" / "task-list.md", "reg", [{"n": 1, "task": "A"}])
        make_list(self.repo / "orphan" / "task-list.md", "orphan",
                  [{"n": 1, "task": "B"}, {"n": 2, "task": "C"}])
        self.register("reg", "reg/task-list.md")
        store, report = self.run_migration()
        paths = [u[0] for u in report.unregistered]
        self.assertIn("orphan/task-list.md", paths)
        self.assertEqual(report.tasks, 1, "an unregistered list must not migrate")

    def test_ambiguous_status_flagged(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "Mixed", "status":
                    "**Scope and objectives DONE 8/4. Anushka handoff NOT "
                    "done. Blocking her.**"},
                   {"n": 2, "task": "Clear", "status": "Done"}])
        self.register("p", "p/task-list.md")
        store, report = self.run_migration()
        self.assertEqual(len(report.ambiguous), 1)
        self.assertEqual(report.ambiguous[0][1], "Mixed")


class TestDryRunAndIdempotency(MigrationCase):

    def _fixture(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "A", "notes": "Long note text here. " * 15},
                   {"n": 2, "task": "B"}])
        self.register("p", "p/task-list.md")

    def test_dry_run_writes_nothing(self):
        self._fixture()
        store, report = self.run_migration(dry_run=True)
        self.assertEqual(report.tasks, 2)
        self.assertEqual(report.notes_written, 1,
                         "a dry run must still report what it would write")
        self.assertEqual(len(store.tasks()), 0, "no rows may be written")
        self.assertFalse((self.repo / "p" / "task-notes").exists())

    def test_rerun_does_not_duplicate(self):
        self._fixture()
        self.write_registry()
        store = mhstore.open_store(root=self.repo)
        self._stores.append(store)
        mhmigrate.migrate(self.repo, store)
        first = len(store.tasks())
        mhmigrate.migrate(self.repo, store)
        self.assertEqual(len(store.tasks()), first)
        self.assertEqual(first, 2)

    def test_id_map_written(self):
        self._fixture()
        store, _ = self.run_migration()
        id_map = self.repo / "operations" / "migration" / "id-map.csv"
        self.assertTrue(id_map.exists())
        text = id_map.read_text()
        self.assertIn("project,old_row,new_id,title,source_line", text)
        self.assertIn("p,1,", text)


PRIORITIES = """# Priorities

## Capacity Dashboard

**Week of August 24, 2026:** a passing reference that is not the header.

**Week of August 24, 2026** (confirmed by MQ)

## Weekly Goals

### Monday 8/24 | ABA, admin-heavy

- [x] **Tagged and finished** [Shallow]. `p#1` (done 2026-08-24)
- [ ] **Tagged and open** [Deep]. `p#2`
- [ ] **No tag at all**, just a line.

### Tuesday 8/25 | Outreach

- [ ] **Second pass on the same task** [Medium]. `p#2`

### Blocked or waiting

- [ ] **Also on a day card** `p#2`
- [ ] **Only blocked** `p#3`

---

## Backlog

- [ ] **Sitting in the backlog** `p#4`
"""


class TestPriorities(MigrationCase):
    """
    priorities.md is where the week lives. Without it the store has no
    planned_day and the dashboard renders an empty week.
    """

    def setUp(self):
        super().setUp()
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "One"}, {"n": 2, "task": "Two"},
                   {"n": 3, "task": "Three"}, {"n": 4, "task": "Four"}])
        self.register("p", "p/task-list.md")
        (self.repo / "priorities.md").write_text(PRIORITIES)

    def _by_ord(self, store):
        return {t["display_ord"]: t for t in store.tasks(project_key="p")}

    def test_week_header_picked_from_the_right_line(self):
        week = mhmigrate.parse_priorities_file(self.repo)
        self.assertEqual(week["week_start"], "2026-08-24")
        self.assertEqual(len(week["days"]), 2)

    def test_day_cards_get_planned_day(self):
        store, report = self.run_migration()
        tasks = self._by_ord(store)
        self.assertEqual(tasks["1"]["planned_day"], "2026-08-24")
        self.assertEqual(tasks["2"]["planned_day"], "2026-08-24")

    def test_checkbox_marks_done(self):
        store, _ = self.run_migration()
        self.assertEqual(self._by_ord(store)["1"]["status"], "done")

    def test_load_tag_captured(self):
        store, _ = self.run_migration()
        tasks = self._by_ord(store)
        self.assertEqual(tasks["1"]["load"], "shallow")
        self.assertEqual(tasks["2"]["load"], "deep")

    def test_blocked_does_not_clear_a_planned_day(self):
        """
        p#2 is on Monday's card and also under Blocked. The blocked pass runs
        later and must not evict it from the week.
        """
        store, _ = self.run_migration()
        self.assertEqual(self._by_ord(store)["2"]["planned_day"], "2026-08-24")

    def test_backlog_does_not_clear_a_planned_day(self):
        store, _ = self.run_migration()
        planned = [t for t in store.tasks() if t["planned_day"]]
        self.assertEqual(len(planned), 3, "one per distinct day-card line")

    def test_multi_day_keeps_the_first_and_reports(self):
        """p#2 is on Monday and Tuesday. A task has one planned_day."""
        store, report = self.run_migration()
        self.assertEqual(self._by_ord(store)["2"]["planned_day"], "2026-08-24")
        self.assertEqual(len(report.priority_multi_day), 1)
        tag, kept, also, _ = report.priority_multi_day[0]
        self.assertEqual((tag, kept, also), ("p#2", "2026-08-24", "2026-08-25"))

    def test_blocked_only_row_gets_no_day(self):
        store, _ = self.run_migration()
        self.assertIsNone(self._by_ord(store)["3"]["planned_day"])

    def test_untagged_line_becomes_a_one_off(self):
        store, report = self.run_migration()
        one_offs = store.tasks(project_key=mhstore.ONE_OFF)
        self.assertEqual(len(one_offs), 1)
        self.assertEqual(one_offs[0]["title"], "No tag at all, just a line")
        self.assertEqual(one_offs[0]["planned_day"], "2026-08-24")
        self.assertEqual(len(report.priority_unmatched), 1)

    def test_week_row_created(self):
        store, _ = self.run_migration()
        self.assertIsNotNone(store.week("2026-08-24"))

    def test_day_card_never_reopens_finished_work(self):
        """The task list is the authority on done, not the checkbox."""
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "One"},
                   {"n": 2, "task": "Two", "status": "Done"},
                   {"n": 3, "task": "Three"}, {"n": 4, "task": "Four"}])
        store, _ = self.run_migration()
        self.assertEqual(self._by_ord(store)["2"]["status"], "done")


class TestVerify(MigrationCase):

    def test_clean_migration_verifies(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "A", "notes": "Long note text here. " * 15}])
        self.register("p", "p/task-list.md")
        store, _ = self.run_migration()
        self.assertEqual(mhmigrate.verify(self.repo, store), [])

    def test_verify_catches_missing_note_file(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "A", "notes": "Long note text here. " * 15}])
        self.register("p", "p/task-list.md")
        store, _ = self.run_migration()
        task = store.tasks(project_key="p")[0]
        (self.repo / task["note_path"]).unlink()
        problems = mhmigrate.verify(self.repo, store)
        self.assertTrue(any("note file missing" in p for p in problems))

    def test_verify_catches_missing_task(self):
        make_list(self.repo / "p" / "task-list.md", "p",
                  [{"n": 1, "task": "A"}, {"n": 2, "task": "B"}])
        self.register("p", "p/task-list.md")
        store, _ = self.run_migration()
        store.conn.execute("DELETE FROM tasks WHERE title = 'B'")
        problems = mhmigrate.verify(self.repo, store)
        self.assertTrue(any("no store row" in p for p in problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
