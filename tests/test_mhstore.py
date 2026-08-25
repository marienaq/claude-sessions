#!/usr/bin/env python3
"""
Tests for mhstore.

Run: python3 -m unittest discover -s tests -v

The status cases are the real strings found in the 24 task-lists, not
invented ones. If a mapping changes, this file is where the argument
about it happens.
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mhstore
from mhstore import StoreError, normalize_status, extract_date, slugify


class TempRepo(unittest.TestCase):
    """A store on a throwaway repo, so audit log and snapshot are real files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = mhstore.open_store(root=self.root)
        self.store.add_project("aba-academy", "AI Academy",
                               dir="course-material/ABA/AI_Academy")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------

class TestNormalizeStatus(unittest.TestCase):

    def check(self, raw, expected):
        got, _, kept = normalize_status(raw)
        self.assertEqual(got, expected, f"{raw!r} -> {got!r}, wanted {expected!r}")
        self.assertEqual(kept, raw.strip(), "raw text must survive verbatim")

    def test_done_family(self):
        for raw in ("Done", "Done 2026-08-12", "Done 8/12",
                    "Done 8/23 (scope narrowed)", "Done 2026-08-18",
                    "**Done 8/24 (a day early)**", "**Draft DONE 8/5**",
                    "Shipped 2026-07-15", "Draft complete, branded docx generated",
                    "**DECIDED 8/3**", "Delivered 2026-08-24, MQ to pick three",
                    "Shared with Rick + Sharla 2026-07-15"):
            self.check(raw, "done")

    def test_closed_reconciliation_is_done(self):
        # MQ's ruling: Closed means resolved, Killed means abandoned.
        self.check("Closed 2026-08-17", "done")
        self.check("Closed 2026-08-24", "done")
        self.check(
            "Closed 2026-08-24 (reconciliation: MQ confirmed both proposals "
            'submitted 8/14; was "Not Started")', "done")

    def test_killed_family_is_canceled(self):
        for raw in ("Killed", "Canceled", "Superseded", "Not needed"):
            self.check(raw, "canceled")

    def test_backlog_family(self):
        for raw in ("Not started", "Not Started", "Backlog"):
            self.check(raw, "backlog")

    def test_in_progress_family(self):
        for raw in ("In progress", "In Progress", "In progress, reopen with MQ",
                    "Ongoing", "Scoping", "Drafted 2026-08-19",
                    "Draft 8/20, skill built 8/24", "Rulings in, one blank left",
                    "Unblocked, MQ to integrate"):
            self.check(raw, "in_progress")

    def test_waiting_family(self):
        for raw in ("Waiting", "Blocked", "Waiting on MQ", "Waiting (ATD)"):
            self.check(raw, "waiting")

    def test_review_beats_draft(self):
        # "Draft ready for MQ review" must not be read as in_progress.
        for raw in ("Ready for MQ review", "Draft ready for MQ review",
                    "**Draft ready for MQ review**"):
            self.check(raw, "review")

    def test_prose_status_cells(self):
        """The four cells that are sentences. Never crash, always classify."""
        cases = {
            "**Scope and objectives DONE 8/4. Anushka handoff NOT done. "
            "Blocking her.**": "done",
            "**In progress (Jennifer). NOT blocked.**": "in_progress",
            "**SUPERSEDED. Both proposals submitted 8/14 per MQ.** Open "
            "question: whether Proposal 2 went in naming Sharla.": "canceled",
        }
        for raw, expected in cases.items():
            self.check(raw, expected)

    def test_empty_is_backlog(self):
        for raw in ("", "   ", None):
            got, done_at, _ = normalize_status(raw)
            self.assertEqual(got, "backlog")
            self.assertIsNone(done_at)

    def test_unrecognized_does_not_guess(self):
        got, _, kept = normalize_status("Purple monkey dishwasher")
        self.assertEqual(got, "backlog")
        self.assertEqual(kept, "Purple monkey dishwasher")

    def test_done_at_extracted_only_when_done(self):
        _, done_at, _ = normalize_status("Done 2026-08-12")
        self.assertEqual(done_at, "2026-08-12")
        _, done_at, _ = normalize_status("Done 8/12", default_year=2026)
        self.assertEqual(done_at, "2026-08-12")
        # A date on a non-done row is not a completion date.
        _, done_at, _ = normalize_status("Waiting since 2026-08-12")
        self.assertIsNone(done_at)


class TestPhase1Conventions(unittest.TestCase):
    """
    Phase 1 writes conventions the plan did not specify. These lock them in
    so migration and the plan cannot silently disagree.
    """

    def test_project_status_vocabulary_matches_phase1(self):
        # build-dashboard.py archives on ("done", "cancelled"), not "closed".
        for value in ("active", "waiting", "parked", "done", "cancelled"):
            self.assertEqual(mhstore.normalize_project_status(value), value)

    def test_closed_folds_into_done(self):
        self.assertEqual(mhstore.normalize_project_status("closed"), "done")
        self.assertEqual(mhstore.normalize_project_status("canceled"), "cancelled")

    def test_project_status_defaults_to_active(self):
        for value in ("", None, "none", "who knows"):
            self.assertEqual(mhstore.normalize_project_status(value), "active")

    def test_none_sentinels_become_null(self):
        for value in ("none", "None", "(none)", "--", "-", "", "  ", "n/a",
                      "(no Notion task)", "—", "TBD"):
            self.assertIsNone(mhstore.clean(value),
                              f"{value!r} should be NULL")

    def test_real_values_survive_cleaning(self):
        self.assertEqual(mhstore.clean("2026-08-24"), "2026-08-24")
        self.assertEqual(mhstore.clean("`3416d218-9ed6`"), "3416d218-9ed6")

    def test_owner_casing_normalized(self):
        self.assertEqual(mhstore.normalize_owner("MQ"), "mq")
        self.assertEqual(mhstore.normalize_owner(""), "mq")
        self.assertEqual(mhstore.normalize_owner(None), "mq")
        self.assertEqual(mhstore.normalize_owner("Jennifer"), "jennifer")

    def test_arrow_stripped_from_title(self):
        self.assertEqual(mhstore.clean_title("→ Review Sharla's feedback"),
                         "Review Sharla's feedback")
        self.assertEqual(mhstore.clean_title("**→ Draft the deck**"),
                         "Draft the deck")
        self.assertTrue(mhstore.has_next_marker("→ Review"))
        self.assertTrue(mhstore.has_next_marker("**→ Review"))
        self.assertFalse(mhstore.has_next_marker("Review the arrow → thing"))

    def test_strikethrough_means_done(self):
        self.assertTrue(mhstore.is_struck_through("~~Old task~~"))
        self.assertFalse(mhstore.is_struck_through("Live task"))
        self.assertEqual(mhstore.clean_title("~~Old task~~"), "Old task")


class TestAmbiguousStatus(unittest.TestCase):
    """
    Prose cells that no rule classifies honestly. These get flagged for a
    human instead of being guessed at silently.
    """

    def test_contradictory_cell_flagged(self):
        # Reads as done; says the handoff is not done and is blocking someone.
        self.assertTrue(mhstore.status_is_ambiguous(
            "**Scope and objectives DONE 8/4. Anushka handoff NOT done. "
            "Blocking her.**"))

    def test_unrecognized_cell_flagged(self):
        for raw in ("**Answered, date still open**",
                    "MQ feedback processed; research dispatch pending",
                    "Open program-design question"):
            self.assertTrue(mhstore.status_is_ambiguous(raw), raw)

    def test_terminal_plus_open_flagged(self):
        # "Closed ... was 'Not Started'" carries both states at once.
        self.assertTrue(mhstore.status_is_ambiguous(
            'Closed 2026-08-24 (reconciliation: MQ confirmed both proposals '
            'submitted 8/14; was "Not Started")'))

    def test_two_open_states_not_flagged(self):
        """Precedence resolves review-over-draft correctly; no human needed."""
        for raw in ("Draft ready for MQ review", "**Draft ready for MQ review**"):
            self.assertFalse(mhstore.status_is_ambiguous(raw), raw)
            self.assertEqual(normalize_status(raw)[0], "review")

    def test_plain_values_not_flagged(self):
        for raw in ("Done", "Not started", "Killed", "In progress", "Waiting",
                    "Blocked", "Done 2026-08-12", "", None):
            self.assertFalse(mhstore.status_is_ambiguous(raw), repr(raw))


class TestHelpers(unittest.TestCase):

    def test_extract_date_formats(self):
        self.assertEqual(extract_date("Done 2026-08-12"), "2026-08-12")
        self.assertEqual(extract_date("Done 8/12", 2026), "2026-08-12")
        self.assertEqual(extract_date("Done 8/12/25"), "2025-08-12")
        self.assertIsNone(extract_date("no date at all"))
        self.assertIsNone(extract_date("2026-13-45"), "impossible date -> None")

    def test_slugify(self):
        self.assertEqual(slugify("Copilot change validation"),
                         "copilot-change-validation")
        self.assertEqual(slugify("Review Sharla's feedback!"),
                         "review-sharla-s-feedback")
        self.assertEqual(slugify(""), "task")
        self.assertLessEqual(len(slugify("word " * 40)), 48)


# ---------------------------------------------------------------------------
# Schema and writes
# ---------------------------------------------------------------------------

class TestSchema(TempRepo):

    def test_one_off_project_seeded(self):
        self.assertIsNotNone(self.store.project(mhstore.ONE_OFF))

    def test_default_settings_seeded(self):
        self.assertEqual(self.store.setting("stale_days"), "5")
        self.assertEqual(self.store.setting("max_deep_per_week"), "3")

    def test_duplicate_project_rejected(self):
        with self.assertRaises(StoreError):
            self.store.add_project("aba-academy", "Dupe")

    def test_notion_project_id_not_unique(self):
        """The two TWG lists share one id and must both migrate."""
        self.store.add_project("twg-a", "TWG A", notion_project_id="726432a0")
        self.store.add_project("twg-b", "TWG B", notion_project_id="726432a0")
        self.assertEqual(len(self.store.projects()), 4)  # +one-off, +academy

    def test_bad_status_rejected(self):
        with self.assertRaises(StoreError):
            self.store.add_project("x", "X", status="frozen")
        with self.assertRaises(StoreError):
            self.store.add_task("aba-academy", "t", status="almost")

    def test_task_needs_real_project(self):
        with self.assertRaises(StoreError):
            self.store.add_task("nope", "orphan")

    def test_task_needs_title(self):
        with self.assertRaises(StoreError):
            self.store.add_task("aba-academy", "   ")


class TestSurveyedShapes(TempRepo):
    """Fields added because the markdown actually contained them."""

    def test_display_ord_holds_non_integers(self):
        for raw in ("3-17", "18b", "W1", "25"):
            t = self.store.add_task("aba-academy", f"row {raw}", display_ord=raw)
            self.assertEqual(t["display_ord"], raw)

    def test_section_preserved(self):
        t = self.store.add_task("aba-academy", "Draft outline", section="Phase 2")
        self.assertEqual(t["section"], "Phase 2")

    def test_status_raw_survives_normalization(self):
        raw = "**Done 8/24 (a day early)**"
        status, done_at, kept = normalize_status(raw, default_year=2026)
        t = self.store.add_task("aba-academy", "Ship it", status=status,
                                status_raw=kept, done_at=done_at)
        self.assertEqual(t["status"], "done")
        self.assertEqual(t["status_raw"], raw)
        self.assertEqual(t["done_at"], "2026-08-24")

    def test_is_next_flag(self):
        a = self.store.add_task("aba-academy", "first", is_next=1)
        self.store.add_task("aba-academy", "second")
        self.assertEqual(self.store.next_task("aba-academy")["id"], a["id"])


class TestNextTask(TempRepo):

    def test_lowest_open_seq_wins(self):
        self.store.add_task("aba-academy", "third", seq=3)
        a = self.store.add_task("aba-academy", "first", seq=1)
        self.store.add_task("aba-academy", "second", seq=2)
        self.assertEqual(self.store.next_task("aba-academy")["id"], a["id"])

    def test_done_tasks_skipped(self):
        a = self.store.add_task("aba-academy", "first", seq=1)
        b = self.store.add_task("aba-academy", "second", seq=2)
        self.store.complete_task(a["id"])
        self.assertEqual(self.store.next_task("aba-academy")["id"], b["id"])

    def test_contractor_rows_skipped(self):
        self.store.add_task("aba-academy", "jennifer work", seq=1, owner="jennifer")
        mine = self.store.add_task("aba-academy", "my work", seq=2)
        self.assertEqual(self.store.next_task("aba-academy")["id"], mine["id"])

    def test_blocked_by_depends_on_skipped(self):
        blocker = self.store.add_task("aba-academy", "blocker", seq=1)
        blocked = self.store.add_task("aba-academy", "blocked", seq=2,
                                      depends_on=blocker["id"])
        later = self.store.add_task("aba-academy", "later", seq=3)
        self.store.complete_task(blocker["id"])
        # blocker done -> blocked is now the next action
        self.assertEqual(self.store.next_task("aba-academy")["id"], blocked["id"])

        other = self.store.add_task("aba-academy", "other", seq=4)
        self.store.update_task(blocked["id"], depends_on=other["id"])
        # blocked again -> skip to the next unblocked row
        self.assertEqual(self.store.next_task("aba-academy")["id"], later["id"])

    def test_no_open_tasks(self):
        t = self.store.add_task("aba-academy", "only")
        self.store.complete_task(t["id"])
        self.assertIsNone(self.store.next_task("aba-academy"))

    def test_self_dependency_rejected(self):
        t = self.store.add_task("aba-academy", "t")
        with self.assertRaises(StoreError):
            self.store.update_task(t["id"], depends_on=t["id"])


class TestCompletion(TempRepo):

    def test_done_at_set_and_cleared(self):
        t = self.store.add_task("aba-academy", "t")
        self.assertIsNone(t["done_at"])
        done = self.store.complete_task(t["id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["done_at"], date.today().isoformat())
        reopened = self.store.reopen_task(t["id"])
        self.assertEqual(reopened["status"], "planned")
        self.assertIsNone(reopened["done_at"], "uncheck must clear done_at")

    def test_plan_and_unplan(self):
        t = self.store.add_task("aba-academy", "t")
        planned = self.store.plan_task(t["id"], "2026-08-31")
        self.assertEqual(planned["planned_day"], "2026-08-31")
        self.assertEqual(planned["status"], "planned")
        self.assertIsNone(self.store.unplan_task(t["id"])["planned_day"])

    def test_reorder_sets_seq_and_clears_is_next(self):
        a = self.store.add_task("aba-academy", "a", is_next=1)
        b = self.store.add_task("aba-academy", "b")
        self.store.reorder("aba-academy", [b["id"], a["id"]])
        self.assertEqual(self.store.task(b["id"])["seq"], 1)
        self.assertEqual(self.store.task(a["id"])["seq"], 2)
        self.assertEqual(self.store.task(a["id"])["is_next"], 0)
        self.assertEqual(self.store.next_task("aba-academy")["id"], b["id"])

    def test_reorder_rejects_foreign_task(self):
        self.store.add_project("other", "Other")
        foreign = self.store.add_task("other", "not mine")
        with self.assertRaises(StoreError):
            self.store.reorder("aba-academy", [foreign["id"]])

    def test_unknown_field_rejected(self):
        t = self.store.add_task("aba-academy", "t")
        with self.assertRaises(StoreError):
            self.store.update_task(t["id"], colour="blue")


class TestCapture(TempRepo):

    def test_capture_lands_unconfirmed(self):
        t = self.store.add_task("aba-academy", "from slack",
                                source="capture", confirmed=0)
        self.assertEqual(t["confirmed"], 0)
        self.assertEqual(t["status"], "backlog")
        self.assertEqual(self.store.confirm_task(t["id"])["confirmed"], 1)

    def test_filter_by_confirmed(self):
        self.store.add_task("aba-academy", "proposed", source="capture", confirmed=0)
        self.store.add_task("aba-academy", "real")
        self.assertEqual(len(self.store.tasks(confirmed=False)), 1)
        self.assertEqual(len(self.store.tasks(confirmed=True)), 1)


class TestWeeks(TempRepo):

    def test_lock_promotes_planned_rows(self):
        t = self.store.add_task("aba-academy", "t", planned_day="2026-09-02")
        self.store.upsert_week("2026-08-31", proposed_at="2026-08-28T13:00:00")
        self.store.lock_week("2026-08-31", rules_report={"rule1": "ok"})
        week = self.store.week("2026-08-31")
        self.assertIsNotNone(week["locked_at"])
        self.assertEqual(json.loads(week["rules_report"]), {"rule1": "ok"})
        self.assertEqual(self.store.task(t["id"])["status"], "planned")

    def test_lock_ignores_tasks_outside_the_week(self):
        outside = self.store.add_task("aba-academy", "next month",
                                      planned_day="2026-09-30")
        self.store.lock_week("2026-08-31")
        self.assertEqual(self.store.task(outside["id"])["status"], "backlog")


class TestAuditAndSnapshot(TempRepo):

    def test_every_write_audits(self):
        self.store.add_task("aba-academy", "t")
        lines = self.store.audit_log.read_text().strip().split("\n")
        actions = [json.loads(x)["action"] for x in lines]
        self.assertIn("project.add", actions)
        self.assertIn("task.add", actions)

    def test_audit_records_before_and_after(self):
        t = self.store.add_task("aba-academy", "t", actor="mq")
        self.store.complete_task(t["id"], actor="orca")
        last = json.loads(self.store.audit_log.read_text().strip().split("\n")[-1])
        self.assertEqual(last["actor"], "orca")
        self.assertEqual(last["task_id"], t["id"])
        self.assertEqual(last["before"]["status"], "backlog")
        self.assertEqual(last["after"]["status"], "done")

    def test_snapshot_written_and_valid(self):
        self.store.add_task("aba-academy", "t")
        data = json.loads(self.store.snapshot_path.read_text())
        self.assertEqual(data["schema_version"], mhstore.SCHEMA_VERSION)
        self.assertEqual(len(data["tasks"]), 1)
        self.assertTrue(any(p["key"] == "aba-academy" for p in data["projects"]))

    def test_no_tmp_file_left(self):
        self.store.add_task("aba-academy", "t")
        self.assertFalse(self.store.snapshot_path.with_suffix(".json.tmp").exists())

    def test_batch_writes_once(self):
        with self.store.write():
            for i in range(5):
                self.store.add_task("aba-academy", f"t{i}")
        self.assertEqual(len(json.loads(self.store.snapshot_path.read_text())["tasks"]), 5)

    def test_failed_batch_rolls_back(self):
        before = len(self.store.tasks())
        with self.assertRaises(StoreError):
            with self.store.write():
                self.store.add_task("aba-academy", "good")
                self.store.add_task("nonexistent-project", "bad")
        self.assertEqual(len(self.store.tasks()), before,
                         "a failed batch must leave nothing behind")

    def test_failed_batch_writes_no_audit(self):
        """Audit is a file and cannot roll back, so it must not be written early."""
        self.store.add_task("aba-academy", "real")
        lines_before = self.store.audit_log.read_text().count("\n")
        with self.assertRaises(StoreError):
            with self.store.write():
                self.store.add_task("aba-academy", "ghost")
                self.store.add_task("nonexistent-project", "bad")
        self.assertEqual(self.store.audit_log.read_text().count("\n"),
                         lines_before,
                         "rolled-back writes must not appear in the audit log")
        self.assertNotIn("ghost", self.store.audit_log.read_text())

    def test_snapshot_not_refreshed_on_failed_batch(self):
        self.store.add_task("aba-academy", "real")
        before = self.store.snapshot_path.read_text()
        with self.assertRaises(StoreError):
            with self.store.write():
                self.store.add_task("aba-academy", "ghost")
                self.store.add_task("nonexistent-project", "bad")
        self.assertEqual(self.store.snapshot_path.read_text(), before)


class TestPersistence(unittest.TestCase):

    def test_reopen_sees_prior_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            s1 = mhstore.open_store(root=tmp)
            s1.add_project("p", "P")
            s1.add_task("p", "survives restart")
            s1.close()

            s2 = mhstore.open_store(root=tmp)
            self.assertEqual(len(s2.tasks(project_key="p")), 1)
            self.assertEqual(s2.tasks(project_key="p")[0]["title"],
                             "survives restart")
            s2.close()

    def test_wal_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = mhstore.open_store(root=tmp)
            mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
