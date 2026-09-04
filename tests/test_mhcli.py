#!/usr/bin/env python3
"""
Tests for the mh CLI and for the checker that validates mh commands quoted
in skill and agent files.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_mh_usage
import mhcli
import mhstore

from test_mhmigrate import make_list


class CliCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "operations").mkdir(parents=True)
        (self.repo / "proj").mkdir()
        self.store = mhstore.open_store(root=self.repo)
        self.store.add_project("proj", "A Project", dir="proj")
        make_list(self.repo / "proj" / "task-list.md", "proj",
                  [{"n": 1, "task": "placeholder"}])
        (self.repo / "operations" / "project-registry.md").write_text(
            "| Key | Name | Task list |\n|---|---|---|\n"
            "| `proj` | proj | `proj/task-list.md` |\n")
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def run_cli(self, *argv):
        """Returns (exit code, stdout)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = mhcli.main(["--repo", str(self.repo), *argv])
        return code, buf.getvalue()


class TestIdentifiers(CliCase):

    def test_all_three_forms_resolve(self):
        task = self.store.add_task("proj", "Findable", display_ord="7")
        for ident in (f"proj#7", f"#{task['id']}", str(task["id"])):
            self.assertEqual(mhcli.resolve(self.store, ident)["id"], task["id"],
                             f"{ident} should resolve")

    def test_printed_identifier_can_be_pasted_back(self):
        """The CLI prints #id for a row with no key; it must accept that."""
        task = self.store.add_task(mhstore.ONE_OFF, "An errand")
        printed = mhcli.tag(task)
        self.assertEqual(mhcli.resolve(self.store, printed)["id"], task["id"])

    def test_backticks_are_tolerated(self):
        """Agents copy `proj#7` out of markdown, backticks included."""
        self.store.add_task("proj", "Findable", display_ord="7")
        self.assertEqual(mhcli.resolve(self.store, "`proj#7`")["display_ord"], "7")

    def test_unknown_row_is_an_error(self):
        with self.assertRaises(mhcli.CliError):
            mhcli.resolve(self.store, "proj#999")
        with self.assertRaises(mhcli.CliError):
            mhcli.resolve(self.store, "not-an-id")


class TestWrites(CliCase):

    def test_add_assigns_a_project_number(self):
        """
        Without one the row has no key#N, so it cannot be referenced by the
        convention every skill and every day-card line uses.
        """
        code, out = self.run_cli("task", "add", "proj", "New work", "--no-regen")
        self.assertEqual(code, 0)
        self.assertIn("proj#1", out, "first row in an empty project")
        code, out = self.run_cli("task", "add", "proj", "More work", "--no-regen")
        self.assertIn("proj#2", out, "the number continues")

    def test_add_numbers_continue_from_the_highest(self):
        self.store.add_task("proj", "Existing", display_ord="17")
        _, out = self.run_cli("task", "add", "proj", "Next one", "--no-regen")
        self.assertIn("proj#18", out)

    def test_global_flags_work_after_the_subcommand(self):
        """`mh task add ... --no-regen` is what anyone writes by reflex."""
        code, _ = self.run_cli("task", "add", "proj", "X",
                               "--no-regen", "--actor", "orca")
        self.assertEqual(code, 0)
        line = self.store.audit_log.read_text().strip().splitlines()[-1]
        self.assertIn('"actor": "orca"', line)

    def test_done_reports_the_next_action(self):
        a = self.store.add_task("proj", "First", display_ord="1", seq=10)
        self.store.add_task("proj", "Second", display_ord="2", seq=20)
        code, out = self.run_cli("task", "done", "proj#1", "--no-regen")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.task(a["id"])["status"], "done")
        self.assertIn("Second", out, "closing a task should surface the next")

    def test_done_twice_is_not_an_error(self):
        self.store.add_task("proj", "First", display_ord="1")
        self.run_cli("task", "done", "proj#1", "--no-regen")
        code, out = self.run_cli("task", "done", "proj#1", "--no-regen")
        self.assertEqual(code, 0)
        self.assertIn("already done", out)

    def test_status_records_what_it_waits_on(self):
        self.store.add_task("proj", "Blocked work", display_ord="1")
        self.run_cli("task", "status", "proj#1", "waiting",
                     "--waiting-on", "Sharla", "--no-regen")
        task = self.store.tasks(project_key="proj")[0]
        self.assertEqual(task["status"], "waiting")
        self.assertEqual(task["waiting_on"], "Sharla")

    def test_leaving_waiting_clears_the_reason(self):
        """'done, waiting on Sharla' reads as unfinished."""
        self.store.add_task("proj", "Blocked work", display_ord="1")
        self.run_cli("task", "status", "proj#1", "waiting",
                     "--waiting-on", "Sharla", "--no-regen")
        self.run_cli("task", "done", "proj#1", "--no-regen")
        self.assertIsNone(self.store.tasks(project_key="proj")[0]["waiting_on"])

    def test_note_appends_and_never_rewrites(self):
        self.store.add_task("proj", "Has history", display_ord="1")
        self.run_cli("task", "note", "proj#1", "First entry.", "--no-regen")
        self.run_cli("task", "note", "proj#1", "Second entry.", "--no-regen")
        path = self.repo / self.store.tasks(project_key="proj")[0]["note_path"]
        body = path.read_text()
        self.assertIn("First entry.", body)
        self.assertIn("Second entry.", body)
        self.assertEqual(body.count("## History"), 1)

    def test_bad_status_is_rejected(self):
        self.store.add_task("proj", "X", display_ord="1")
        with self.assertRaises(SystemExit):
            self.run_cli("task", "status", "proj#1", "almost-done")

    def test_add_to_unknown_project_fails(self):
        code, _ = self.run_cli("task", "add", "nope", "X", "--no-regen")
        self.assertEqual(code, 1)


class TestPlanning(CliCase):
    """
    Planning a week is mostly moving already-open rows onto days. Without
    these the CLI could create work but never schedule it, so Mode 5 could
    not finalize and the Friday job could not write a proposed week at all.
    """

    def test_plan_an_existing_row_onto_a_day(self):
        self.store.add_task("proj", "Schedulable", display_ord="1")
        self.run_cli("task", "plan", "proj#1", "2026-09-04", "--no-regen")
        task = self.store.tasks(project_key="proj")[0]
        self.assertEqual(task["planned_day"], "2026-09-04")
        self.assertEqual(task["status"], "planned")

    def test_plan_with_no_day_unschedules(self):
        self.store.add_task("proj", "Schedulable", display_ord="1",
                            planned_day="2026-09-04", status="planned")
        self.run_cli("task", "plan", "proj#1", "--no-regen")
        task = self.store.tasks(project_key="proj")[0]
        self.assertIsNone(task["planned_day"])
        self.assertEqual(task["status"], "backlog")

    def test_load_can_be_set_and_cleared(self):
        self.store.add_task("proj", "Weighted", display_ord="1")
        self.run_cli("task", "load", "proj#1", "deep", "--no-regen")
        self.assertEqual(self.store.tasks(project_key="proj")[0]["load"], "deep")
        self.run_cli("task", "load", "proj#1", "none", "--no-regen")
        self.assertIsNone(self.store.tasks(project_key="proj")[0]["load"])

    def test_propose_marks_the_week_without_locking(self):
        code, out = self.run_cli("plan", "propose", "2026-09-07",
                                 "--notes", "Assembled by the Friday job",
                                 "--no-regen")
        self.assertEqual(code, 0)
        week = self.store.week("2026-09-07")
        self.assertIsNotNone(week["proposed_at"])
        self.assertIsNone(week["locked_at"], "a proposal is not a commitment")
        self.assertEqual(week["notes"], "Assembled by the Friday job")

    def test_propose_refuses_a_locked_week_unless_forced(self):
        self.store.upsert_week("2026-09-07", locked_at="2026-09-05T10:00:00")
        code, _ = self.run_cli("plan", "propose", "2026-09-07", "--no-regen")
        self.assertEqual(code, 1)
        code, _ = self.run_cli("plan", "propose", "2026-09-07", "--force",
                               "--no-regen")
        self.assertEqual(code, 0)
        self.assertIsNone(self.store.week("2026-09-07")["locked_at"])


class TestStatusSpelling(CliCase):
    """
    The task enum says "canceled" and the project enum says "cancelled".
    Neither can change without breaking the other, so both spellings are
    accepted. The developer doing 2.6 hit this on their first run.
    """

    def test_both_spellings_are_accepted(self):
        self.store.add_task("proj", "Doomed", display_ord="1")
        for spelling in ("cancelled", "canceled"):
            self.run_cli("task", "status", "proj#1", spelling, "--no-regen")
            self.assertEqual(
                self.store.tasks(project_key="proj")[0]["status"], "canceled")
            self.run_cli("task", "status", "proj#1", "backlog", "--no-regen")

    def test_markdown_wording_is_accepted(self):
        self.store.add_task("proj", "Wordy", display_ord="1")
        for word, expected in (("Killed", "canceled"), ("blocked", "waiting"),
                               ("not started", "backlog"),
                               ("in progress", "in_progress")):
            self.run_cli("task", "status", "proj#1", word, "--no-regen")
            self.assertEqual(
                self.store.tasks(project_key="proj")[0]["status"], expected,
                f"{word!r} should map to {expected}")

    def test_nonsense_is_still_rejected(self):
        self.store.add_task("proj", "X", display_ord="1")
        with self.assertRaises(SystemExit):
            self.run_cli("task", "status", "proj#1", "almost-done")


class TestRegeneration(CliCase):

    def test_a_write_regenerates_the_task_list(self):
        """
        Invariant 4. Without this the markdown an agent reads next is behind
        the store it just wrote to.
        """
        self.store.add_task("proj", "Visible soon", display_ord="1", seq=10,
                            section="Tasks")
        path = self.repo / "proj" / "task-list.md"
        self.assertNotIn("Visible soon", path.read_text())
        self.run_cli("task", "done", "proj#1")
        self.assertIn("Visible soon", path.read_text())

    def test_no_regen_skips_it(self):
        self.store.add_task("proj", "Not yet", display_ord="1", section="Tasks")
        self.run_cli("task", "done", "proj#1", "--no-regen")
        self.assertNotIn("Not yet", (self.repo / "proj" / "task-list.md").read_text())

    def test_verify_is_clean_after_regen(self):
        self.store.add_task("proj", "Something", display_ord="1", section="Tasks")
        self.run_cli("regen")
        code, out = self.run_cli("verify")
        self.assertEqual(code, 0, out)

    def test_verify_catches_a_hand_edited_file(self):
        """The whole point: a generated file edited by hand is reported."""
        self.store.add_task("proj", "Something", display_ord="1", section="Tasks")
        self.run_cli("regen")
        path = self.repo / "proj" / "task-list.md"
        path.write_text(path.read_text().replace("Something", "Tampered"))
        code, out = self.run_cli("verify")
        self.assertEqual(code, 1)
        self.assertIn("task-list.md", out)


class TestNeverCreatesAStore(unittest.TestCase):
    """
    open_store() creates a database wherever it is pointed. A missing
    MELLONHEAD_ROOT therefore wrote an empty tasks.db into the live repo and
    regenerated its dashboard from nothing, blanking a real generated file.
    The CLI must refuse a repo that has no store rather than making one.
    """

    def test_missing_store_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "operations").mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = mhcli.main(["--repo", str(repo), "task", "next", "--all"])
            self.assertEqual(code, 2)
            self.assertFalse((repo / "operations" / "tasks.db").exists(),
                             "a read must not create a database")


class TestReference(unittest.TestCase):
    """
    The reference is generated from the parser. A hand-written one drifts and
    nobody notices until an agent runs a command that no longer exists.
    """

    def test_committed_reference_matches_the_parser(self):
        committed = Path(__file__).resolve().parent.parent / "mh-reference.md"
        self.assertTrue(committed.exists(), "mh-reference.md is missing")
        self.assertEqual(
            committed.read_text().strip(),
            mhcli.render_reference().strip(),
            "mh-reference.md is stale; regenerate with `mh docs > "
            "mh-reference.md`")

    def test_every_command_appears(self):
        text = mhcli.render_reference()
        for command in ("mh task done", "mh task add", "mh task note",
                        "mh task find", "mh plan lock", "mh verify",
                        "mh regen", "mh export"):
            self.assertIn(command, text, f"{command} is undocumented")

    def test_the_rules_are_stated(self):
        text = mhcli.render_reference()
        self.assertIn("Never edit", text)
        self.assertIn("mh verify", text)

    def test_docs_runs_without_a_store(self):
        """The reference has to be readable before anything is migrated."""
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = mhcli.main(["--repo", tmp, "docs"])
            self.assertEqual(code, 0)
            self.assertIn("command reference", buf.getvalue())


class TestReadCommands(CliCase):

    def test_next_across_projects(self):
        self.store.add_task("proj", "The next thing", display_ord="1", seq=10)
        code, out = self.run_cli("task", "next", "--all")
        self.assertEqual(code, 0)
        self.assertIn("The next thing", out)

    def test_find_matches_on_words(self):
        self.store.add_task("proj", "Champion survey results", display_ord="1")
        self.store.add_task("proj", "Something unrelated", display_ord="2")
        code, out = self.run_cli("task", "find", "champion", "survey")
        self.assertEqual(code, 0)
        self.assertIn("Champion survey results", out)
        self.assertNotIn("Something unrelated", out)

    def test_find_with_no_match_exits_nonzero(self):
        code, _ = self.run_cli("task", "find", "nothingmatchesthis")
        self.assertEqual(code, 1)

    def test_export_needs_no_sqlite_to_read(self):
        self.store.add_task("proj", "Exported work", display_ord="1")
        code, _ = self.run_cli("export")
        self.assertEqual(code, 0)
        body = (self.repo / "operations" / "tasks-export.md").read_text()
        self.assertIn("Exported work", body)
        self.assertIn("A Project", body)

    def test_plan_show(self):
        monday = date.today() - timedelta(days=date.today().weekday())
        self.store.upsert_week(monday.isoformat(), locked_at="2026-08-31T10:00:00")
        self.store.add_task("proj", "Scheduled", display_ord="1",
                            planned_day=monday.isoformat(), status="planned")
        code, out = self.run_cli("plan", "show")
        self.assertEqual(code, 0)
        self.assertIn("locked", out)
        self.assertIn("Scheduled", out)


class TestSkillCommandChecker(unittest.TestCase):
    """
    2.6 rewrites the skills to call mh. A command that does not exist fails
    silently at agent runtime, so the invocations get parsed here instead.
    """

    def setUp(self):
        self.parser = mhcli.build_parser()

    def check(self, text):
        return [check_mh_usage.check(cmd, self.parser)
                for _, cmd in check_mh_usage.extract(text)]

    def test_real_commands_pass(self):
        text = ("Close it with `mh task done aba-academy#25`.\n"
                "- Run `mh task next --all` first.\n"
                "Then mh plan lock 2026-08-31\n")
        self.assertEqual([r for r in self.check(text) if r], [])

    def test_placeholders_are_checked_for_shape(self):
        text = "Record status with `mh task status <key#id> <status>`."
        self.assertEqual([r for r in self.check(text) if r], [])

    def test_invented_subcommand_is_caught(self):
        text = "Use `mh task complete aba-academy#25` to close it."
        problems = [r for r in self.check(text) if r]
        self.assertEqual(len(problems), 1)

    def test_wrong_group_is_caught(self):
        text = "Run `mh tasks next --all`."
        self.assertTrue([r for r in self.check(text) if r])

    def test_bad_status_value_is_caught(self):
        text = "Set it with `mh task status proj#1 finished`."
        self.assertTrue([r for r in self.check(text) if r])

    def test_naming_a_command_without_arguments_is_a_reference(self):
        """
        "close it with `mh task done`" names a real command; it is not a
        prescription missing an argument. Treating those as errors made
        eleven of thirteen findings in the handoff brief false positives,
        which is how a checker gets ignored.
        """
        text = ("Close it with `mh task done`.\n"
                "Add one with `mh task add`.\n"
                "Record it with `mh task note`.\n")
        self.assertEqual([r for r in self.check(text) if r], [])

    def test_positionals_are_matched_by_position(self):
        """`mh task status <task> <status>` must not check <task> against the
        status choices."""
        text = "Run `mh task status aba-academy#25 waiting`."
        self.assertEqual([r for r in self.check(text) if r], [])

    def test_unknown_option_is_caught(self):
        text = "Run `mh task done proj#1 --force`."
        self.assertTrue([r for r in self.check(text) if r])

    def test_option_values_are_not_counted_as_positionals(self):
        """
        `mh task add --source capture --unconfirmed` supplies no title.
        Counting "capture" as a positional made the reference look complete,
        and it was then rejected for an argument prose never supplies.
        """
        text = "Propose it with `mh task add --source capture --unconfirmed`."
        self.assertEqual([r for r in self.check(text) if r], [])

    def test_a_complete_invocation_is_fully_validated(self):
        """Once every positional is supplied, argparse checks the values."""
        self.assertTrue([r for r in self.check(
            "Run `mh task status proj#1 stuck`.") if r])
        self.assertEqual([r for r in self.check(
            "Run `mh task status proj#1 waiting`.") if r], [])

    def test_prose_mentioning_mh_is_not_a_false_positive(self):
        text = ("The mh CLI is the write path.\n"
                "Never edit the file by hand.\n"
                "See `operations/mh` for the implementation.\n")
        self.assertEqual([r for r in self.check(text) if r], [])

    def test_trailing_prose_is_trimmed(self):
        text = "Run `mh task next --all`, then read the dashboard."
        self.assertEqual([r for r in self.check(text) if r], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
