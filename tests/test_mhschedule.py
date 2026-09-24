#!/usr/bin/env python3
"""
Tests for dependencies (schema 3) and the scheduling validator.

Dates are in January 2030 so nothing depends on today: 2030-01-07 is a
Monday. The course fixture is the real pipeline shape, stages 1 to 6 in a
line and four parallel review tracks after it, because a validator tested
against a chain would pass exactly the defect that invalidated the first
draft of the requirements.
"""

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_mh_usage
import mhcli
import mhschedule
import mhsession
import mhstore
from mhstore import StoreError

MON, TUE, WED, THU, FRI, SAT = ("2030-01-07", "2030-01-08", "2030-01-09",
                                "2030-01-10", "2030-01-11", "2030-01-12")
START = MON


def day(n):
    """2030-01-07 plus n days."""
    from datetime import date, timedelta
    return (date(2030, 1, 7) + timedelta(days=n)).isoformat()


class StoreCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "operations").mkdir()
        self.store = self.open()
        self.store.add_project("aba-academy", "Academy")
        self.store.add_project("outreach", "Outreach")

    def open(self):
        store = mhstore.open_store(root=self.repo)
        store._session = dict(mhsession.EMPTY)
        return store

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def add(self, title, project="aba-academy", **fields):
        fields.setdefault("load", "medium")
        return self.store.add_task(project, title, **fields)

    def findings(self, rule=None, **kw):
        found = mhschedule.validate(self.store, start=kw.pop("start", START), **kw)
        return [f for f in found if rule is None or f["rule"] == rule]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

class TestDeps(StoreCase):

    def test_many_predecessors_each_with_a_gap(self):
        prep = self.add("VILT prep")
        preds = [self.add(f"final review {n}") for n in range(4)]
        for i, p in enumerate(preds):
            self.store.add_dep(prep["id"], p["id"], gap=i)
        rows = self.store.deps(task_id=prep["id"])
        self.assertEqual([r["depends_on_id"] for r in rows], [p["id"] for p in preds])
        self.assertEqual([r["min_gap_days"] for r in rows], [0, 1, 2, 3])

    def test_cycle_is_rejected_naming_both_tags(self):
        a, b, c = self.add("a"), self.add("b"), self.add("c")
        self.store.add_dep(b["id"], a["id"])
        self.store.add_dep(c["id"], b["id"])
        with self.assertRaises(StoreError) as caught:
            self.store.add_dep(a["id"], c["id"])
        self.assertIn(mhstore.tag(a), str(caught.exception))
        self.assertIn(mhstore.tag(c), str(caught.exception))
        self.assertEqual(len(self.store.deps()), 2)

    def test_same_day_pairs_do_not_count_as_cycles(self):
        guide, slides = self.add("guide review"), self.add("slides review")
        self.store.add_dep(slides["id"], guide["id"], kind="same_day")
        self.store.add_dep(guide["id"], slides["id"], kind="same_day")
        self.assertEqual(len(self.store.deps()), 2)

    def test_self_dependency_and_negative_gap_are_rejected(self):
        a, b = self.add("a"), self.add("b")
        with self.assertRaises(StoreError):
            self.store.add_dep(a["id"], a["id"])
        with self.assertRaises(StoreError):
            self.store.add_dep(a["id"], b["id"], gap=-1)

    def test_readding_updates_the_gap_and_audits_once(self):
        a, b = self.add("a"), self.add("b")
        self.store.add_dep(b["id"], a["id"])
        self.store.add_dep(b["id"], a["id"], gap=3)
        self.store.add_dep(b["id"], a["id"], gap=3)
        self.assertEqual(self.store.deps(task_id=b["id"])[0]["min_gap_days"], 3)
        dep_events = [e for e in self.store.events(task_id=b["id"], limit=None)
                      if json.loads(e["payload"])["action"] == "dep.add"]
        self.assertEqual(len(dep_events), 2)

    def test_next_task_waits_on_every_predecessor(self):
        first = self.add("one", seq=1)
        second = self.add("two", seq=2)
        after = self.add("after both", seq=0)
        self.store.add_dep(after["id"], first["id"])
        self.store.add_dep(after["id"], second["id"])
        self.store.complete_task(first["id"])
        self.assertEqual(self.store.next_task("aba-academy")["id"], second["id"])
        self.store.complete_task(second["id"])
        self.assertEqual(self.store.next_task("aba-academy")["id"], after["id"])

    def test_deleting_a_task_drops_its_dependencies(self):
        a, b = self.add("a"), self.add("b")
        self.store.add_dep(b["id"], a["id"])
        self.store.conn.execute("DELETE FROM tasks WHERE id = ?", (a["id"],))
        self.assertEqual(self.store.deps(), [])

    def test_deps_are_in_the_snapshot(self):
        a, b = self.add("a"), self.add("b")
        self.store.add_dep(b["id"], a["id"], gap=7)
        self.store.write_snapshot()
        snap = json.loads(self.store.snapshot_path.read_text())
        self.assertEqual(snap["deps"][0]["min_gap_days"], 7)


class TestLegacyMigration(unittest.TestCase):
    """A v2 file with both legacy formats opens as v3 with one clean dep."""

    def test_integer_moves_and_strings_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "operations").mkdir()
            store = mhstore.open_store(root=repo)
            store._session = dict(mhsession.EMPTY)
            store.add_project("copilot-updates", "Updates")
            store.add_project("aba-champions", "Champions")
            target = store.add_task("copilot-updates", "target")
            live = store.add_task("copilot-updates", "live")
            closed = store.add_task("aba-champions", "closed", status="done")
            gone = store.add_task("copilot-updates", "points nowhere")
            # The legacy strings never satisfied the column's foreign key;
            # they got in with it off, as they did in the live store.
            store.conn.execute("PRAGMA foreign_keys=OFF")
            store.conn.execute("UPDATE tasks SET depends_on = ? WHERE id = ?",
                               (str(target["id"]), live["id"]))
            store.conn.execute("UPDATE tasks SET depends_on = ? WHERE id = ?",
                               ("aba-champions#297,aba-champions#298", closed["id"]))
            store.conn.execute("UPDATE tasks SET depends_on = 9999 WHERE id = ?",
                               (gone["id"],))
            store.conn.execute("DELETE FROM deps")
            store.conn.execute("PRAGMA user_version = 2")
            store.close()

            store = mhstore.open_store(root=repo, seed_settings=False)
            try:
                self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual([(d["task_id"], d["depends_on_id"]) for d in store.deps()],
                                 [(live["id"], target["id"])])
                left = store.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE depends_on IS NOT NULL").fetchone()[0]
                self.assertEqual(left, 0)
                # The dropped value is still in the audit log.
                self.assertIn("aba-champions#297", store.audit_log.read_text())
                cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(tasks)")}
                self.assertTrue({"fixed", "external_ready_date", "done_when"} <= cols)
            finally:
                store.close()


# ---------------------------------------------------------------------------
# The course pipeline, as methodology/course-build-pipeline.md has it
# ---------------------------------------------------------------------------

TRACKS = ("Rise", "facilitation guide", "slides", "QRG")


def build_course(case, days):
    """
    Sixteen stages with the real dependency graph. `days` maps stage number
    to a day offset; stages are 1-6 in a line, then initial/final pairs 7/8,
    9/10, 11/12, 13/14 each gated only on 6, then 15 on all four finals and
    16 on 15. Returns {stage: row}.
    """
    s = case.store
    rows = {}
    for n in range(1, 17):
        rows[n] = case.add(f"stage {n}", planned_day=day(days[n]) if n in days else None,
                           load="deep" if n in (2, 3, 4) else "medium")
    for n in range(2, 7):
        s.add_dep(rows[n]["id"], rows[n - 1]["id"])
    finals = []
    for initial in (7, 9, 11, 13):
        s.add_dep(rows[initial]["id"], rows[6]["id"])
        s.add_dep(rows[initial + 1]["id"], rows[initial]["id"], gap=3)
        finals.append(initial + 1)
    # The Sharla gate: the guide and slides reviews land together.
    s.add_dep(rows[11]["id"], rows[9]["id"], kind="same_day")
    s.add_dep(rows[12]["id"], rows[10]["id"], kind="same_day")
    for f in finals:
        s.add_dep(rows[15]["id"], rows[f]["id"])
    s.add_dep(rows[16]["id"], rows[15]["id"])
    return rows


# Monday/Wednesday only (ABA days), one medium-or-deep row per day: a
# schedule that breaks nothing, with all four tracks in parallel.
GOOD = {1: 0, 2: 2, 3: 7, 4: 9, 5: 14, 6: 16,
        7: 21, 9: 21, 11: 21, 13: 23,
        8: 28, 10: 28, 12: 28, 14: 30,
        15: 35, 16: 37}


class TestCourseShape(StoreCase):

    def setUp(self):
        super().setUp()
        # One course alone would sit under the weekly target every week;
        # that is a notice, not what these tests are about.
        mhschedule.set_rule(self.store, "week_target", "0.5")

    def test_parallel_tracks_and_the_sharla_gate_pass(self):
        build_course(self, GOOD)
        self.assertEqual(mhschedule.violations(self.findings()), [])

    def test_tracks_are_not_chained(self):
        """Every initial review waits on stage 6 only, not on another track."""
        rows = build_course(self, GOOD)
        for initial in (7, 9, 11, 13):
            preds = {d["depends_on_id"] for d in self.store.deps(task_id=rows[initial]["id"])
                     if d["kind"] == "finish_to_start"}
            self.assertEqual(preds, {rows[6]["id"]})

    def test_stage_order_error_is_named_with_both_tags(self):
        bad = {**GOOD, 5: 1}          # stage 5 before stage 2
        rows = build_course(self, bad)
        found = self.findings("dependency")
        self.assertTrue(any(set(f["tags"]) == {mhstore.tag(rows[5]), mhstore.tag(rows[4])}
                            and "before" in f["message"] for f in found), found)

    def test_review_pair_under_three_days_is_a_violation(self):
        bad = {**GOOD, 10: 23, 12: 23, 8: 23}   # finals two days after initials
        rows = build_course(self, bad)
        found = self.findings("dependency")
        msgs = [f["message"] for f in found]
        self.assertTrue(any(mhstore.tag(rows[10]) in m and "needs 3" in m for m in msgs), msgs)

    def test_splitting_the_sharla_gate_is_a_violation(self):
        build_course(self, {**GOOD, 11: 22})
        self.assertEqual(len(self.findings("same_day")), 1)

    def test_waiting_on_an_unplanned_stage(self):
        plan = {k: v for k, v in GOOD.items() if k != 6}
        rows = build_course(self, plan)
        msgs = [f["message"] for f in self.findings("dependency")]
        self.assertTrue(any("not planned" in m and mhstore.tag(rows[6]) in m for m in msgs))

    def test_a_done_predecessor_satisfies_its_dependents(self):
        plan = {k: v for k, v in GOOD.items() if k not in (1, 2)}
        rows = build_course(self, plan)
        self.store.complete_task(rows[1]["id"])
        self.store.complete_task(rows[2]["id"])
        self.assertEqual(self.findings("dependency"), [])


# ---------------------------------------------------------------------------
# The other rules
# ---------------------------------------------------------------------------

class TestRules(StoreCase):

    def test_ready_date(self):
        t = self.add("review the draft", planned_day=MON,
                     external_ready_date=WED)
        found = self.findings("ready")
        self.assertEqual(found[0]["tags"], [mhstore.tag(t)])

    def test_day_cap_counts_fixed_rows_but_they_alone_do_not_trip_it(self):
        self.add("session", planned_day=MON, load="deep", fixed=1)
        self.add("prep", planned_day=MON, load="medium", fixed=1)
        self.assertEqual(self.findings("day_cap"), [])
        self.add("review", planned_day=MON, load="medium")
        found = self.findings("day_cap")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["load"], 2.0)

    def test_contractor_rows_do_not_use_mq_capacity(self):
        for n in range(4):
            self.add(f"anushka builds {n}", planned_day=MON, load="deep", owner="anushka")
        self.assertEqual(self.findings("day_cap"), [])

    def test_lanes(self):
        aba_on_tue = self.add("aba on a business day", planned_day=TUE)
        biz_on_wed = self.add("outreach on an aba day", project="outreach", planned_day=WED)
        self.add("aba on monday", planned_day=MON)
        self.add("small on friday", planned_day=FRI, load="shallow")
        big_fri = self.add("medium on friday", planned_day=FRI)
        sat = self.add("saturday", planned_day=SAT, load="shallow")
        self.add("fixed session on friday", planned_day=FRI, load="deep", fixed=1)
        tags = sorted(t for f in self.findings("lane") for t in f["tags"])
        self.assertEqual(tags, sorted(mhstore.tag(t) for t in
                                      (aba_on_tue, biz_on_wed, big_fri, sat)))

    def test_lane_can_be_set_per_project(self):
        self.store.update_project("outreach", lane="aba")
        self.add("outreach on wednesday", project="outreach", planned_day=WED)
        self.assertEqual(self.findings("lane"), [])
        self.store.update_project("outreach", lane="any")
        self.add("outreach on tuesday", project="outreach", planned_day=TUE)
        self.assertEqual(self.findings("lane"), [])
        with self.assertRaises(StoreError):
            self.store.update_project("outreach", lane="weekends")

    def test_weeks_over_stretch_and_under_target(self):
        for d in (MON, WED):
            self.add("deep", planned_day=d, load="deep")
            self.add("medium", planned_day=d, load="medium")
        self.add("next week", planned_day=day(7), load="medium")
        weeks = {f["day"]: f for f in self.findings("week")}
        self.assertEqual(weeks[MON]["level"], "violation")
        self.assertEqual(weeks[MON]["load"], 3.0)
        self.assertEqual(weeks[day(7)]["level"], "notice")

    def test_an_empty_week_between_plans_is_an_opportunity(self):
        self.add("a", planned_day=MON, load="deep")
        self.add("b", planned_day=MON, load="deep", fixed=1)
        self.add("c", planned_day=day(14), load="deep")
        weeks = {f["day"]: f for f in self.findings("week")}
        self.assertEqual(weeks[day(7)]["load"], 0)

    def test_untagged_rows_are_counted_and_flagged(self):
        self.store.add_task("aba-academy", "untagged", planned_day=MON)
        self.add("deep", planned_day=MON, load="deep")
        # Untagged counts as medium: 0.5 + 1.0 = 1.5, at the cap, not over.
        self.assertEqual(self.findings("day_cap"), [])
        self.store.add_task("aba-academy", "untagged too", planned_day=MON)
        self.assertEqual(self.findings("day_cap")[0]["load"], 2.0)
        self.assertEqual(len(self.findings("untagged")[0]["tasks"]), 2)

    def test_backlog_is_grouped_by_project_in_days(self):
        self.add("a")
        self.add("b", load="deep")
        self.add("c", project="outreach", load="shallow")
        self.add("mine planned", planned_day=MON)
        self.add("not mine", owner="anushka")
        f = self.findings("backlog")[0]
        self.assertEqual(f["by_project"],
                         {"aba-academy": {"rows": 2, "days": 1.5},
                          "outreach": {"rows": 1, "days": 0.25}})

    def test_placements_are_a_scenario_over_the_committed_days(self):
        a = self.add("a", planned_day=MON, load="deep")
        b = self.add("b", planned_day=WED, load="deep")
        self.assertEqual(self.findings("day_cap"), [])
        found = self.findings("day_cap", placements={b["id"]: MON})
        self.assertEqual(len(found), 1)
        # Nothing was written.
        self.assertEqual(self.store.task(b["id"])["planned_day"], WED)
        # None takes a row off the plan.
        backlog = self.findings("backlog", placements={a["id"]: None})
        self.assertIn(a["id"], backlog[0]["tasks"])

    def test_capacity_before_start_is_not_checked(self):
        for _ in range(3):
            self.add("past", planned_day=MON, load="deep")
        self.assertEqual(self.findings("day_cap", start=TUE), [])


class TestRuleSettings(StoreCase):

    def test_defaults_then_settings(self):
        r = mhschedule.rules(self.store)
        self.assertEqual(r["day_cap"], {"value": 1.5, "source": "default"})
        mhschedule.set_rule(self.store, "day_cap", "2")
        self.assertEqual(mhschedule.rules(self.store)["day_cap"],
                         {"value": 2.0, "source": "setting"})

    def test_bad_values_are_refused(self):
        for name, value in (("day_cap", "lots"), ("day_cap", "0"),
                            ("float_max_load", "huge"),
                            ("days", '{"Mon": "gym"}'), ("nonsense", "1")):
            with self.assertRaises(mhschedule.RulesError, msg=(name, value)):
                mhschedule.set_rule(self.store, name, value)

    def test_a_corrupt_setting_is_an_error_not_the_default(self):
        self.store.set_setting("schedule.day_cap", "not json")
        with self.assertRaises(mhschedule.RulesError):
            mhschedule.rules(self.store)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _after(stages, n):
    return {a for a, _ in next(st for st in stages if st["n"] == n)["after"]}


class TestTemplateShape(unittest.TestCase):
    """The templates against the methodology docs, stage by stage."""

    def test_course_loads_match_the_methodology_table(self):
        loads = {st["n"]: st["load"] for st in mhschedule.COURSE}
        self.assertEqual(loads, {1: "shallow", 2: "deep", 3: "deep", 4: "deep",
                                 **{n: "medium" for n in range(5, 17)}})

    def test_course_is_a_line_then_four_parallel_tracks(self):
        c = mhschedule.COURSE
        for n in range(2, 7):
            self.assertEqual(_after(c, n), {n - 1})
        for initial in (7, 9, 11, 13):
            self.assertEqual(_after(c, initial), {6}, f"stage {initial}")
            final = next(st for st in c if st["n"] == initial + 1)
            self.assertEqual(final["after"], ((initial, 3),))
        self.assertEqual(_after(c, 15), {8, 10, 12, 14})
        self.assertEqual(_after(c, 16), {15})
        self.assertTrue(next(st for st in c if st["n"] == 16)["fixed"])

    def test_the_sharla_gate(self):
        by_n = {st["n"]: st for st in mhschedule.COURSE}
        self.assertEqual(by_n[11]["same_day"], (9,))
        self.assertEqual(by_n[12]["same_day"], (10,))

    def test_champions_matches_its_week_shape(self):
        c = mhschedule.CHAMPIONS
        loads = {st["n"]: st["load"] for st in c}
        self.assertEqual({n for n, l in loads.items() if l == "shallow"}, {3, 7, 10, 11})
        by_n = {st["n"]: st for st in c}
        for n in (4, 5, 6):
            self.assertEqual(by_n[n]["after"], ((2, 7),), f"stage {n}")
        self.assertEqual(by_n[5]["same_day"], (4,))
        self.assertEqual(by_n[6]["same_day"], (4,))
        self.assertTrue(by_n[10]["fixed"])
        self.assertFalse(any("rehears" in st["name"] for st in c))
        self.assertTrue(all(st["done_when"] for st in c + mhschedule.COURSE))

    def test_self_paced_keeps_one_to_eight(self):
        self.assertEqual([st["n"] for st in mhschedule.template("course", self_paced=True)],
                         list(range(1, 9)))


class TestTemplates(StoreCase):

    def setUp(self):
        super().setUp()
        mhschedule.set_rule(self.store, "week_target", "0.5")

    def test_apply_creates_the_rows_and_the_graph(self):
        rows, n_deps = mhschedule.apply_template(self.store, "course", "aba-academy",
                                                 "Creating Documents")
        self.assertEqual(len(rows), 16)
        first = rows[9][0]
        self.assertEqual(first["title"],
                         "Creating Documents: initial facilitation guide review (stage 9)")
        self.assertTrue(first["done_when"])
        self.assertEqual(rows[16][0]["fixed"], 1)
        # 5 in the line, 4 initials, 4 finals, 4 into VILT prep, 1 into delivery, 2 same-day
        self.assertEqual(len(self.store.deps()), 5 + 4 + 4 + 4 + 1 + 2)
        for n, offset in GOOD.items():
            self.store.plan_task(rows[n][0]["id"], day(offset))
        self.assertEqual(mhschedule.violations(self.findings()), [])

    def test_link_wires_existing_rows_and_bridges_a_gap(self):
        made = {}
        for n in (2, 3, 4, 6, 7, 8):
            st = next(x for x in mhschedule.COURSE if x["n"] == n)
            made[n] = self.add(mhschedule.stage_title("Extracting", st))
        self.add("Something else: scoping (stage 2)")
        rows, missing, _ = mhschedule.link_template(self.store, "course", "aba-academy",
                                                    "Extracting")
        self.assertEqual(missing, [1, 5, 9, 10, 11, 12, 13, 14, 15, 16])
        waits = {d["depends_on_id"] for d in self.store.deps(task_id=made[6]["id"])}
        self.assertEqual(waits, {made[4]["id"]})          # bridged past 5
        gap = self.store.deps(task_id=made[8]["id"])[0]["min_gap_days"]
        self.assertEqual(gap, 3)
        before = len(self.store.deps())
        mhschedule.link_template(self.store, "course", "aba-academy", "Extracting")
        self.assertEqual(len(self.store.deps()), before)

    def test_a_missing_track_passes_on_no_gap(self):
        outline = self.add("Extracting: review course outline (stage 6)")
        final = self.add("Extracting: final slides review (stage 12)")
        prep = self.add("Extracting: VILT prep (stage 15)")
        mhschedule.link_template(self.store, "course", "aba-academy", "Extracting")
        waits = {d["depends_on_id"]: d["min_gap_days"]
                 for d in self.store.deps(task_id=prep["id"])}
        # Via 8, 10 and 14, all missing, prep reaches 6; via 12 it stops there.
        self.assertEqual(waits, {outline["id"]: 0, final["id"]: 0})

    def test_link_takes_a_split_stage(self):
        agenda = self.add("Extracting: review the VILT agenda")
        outline = self.add("Extracting: review the course outline")
        guide = self.add("Extracting: initial facilitation guide review (stage 9)")
        mhschedule.link_template(self.store, "course", "aba-academy", "Extracting",
                                 overrides={6: [agenda, outline]})
        waits = {d["depends_on_id"] for d in self.store.deps(task_id=guide["id"])}
        self.assertEqual(waits, {agenda["id"], outline["id"]})


class TestDayCaps(StoreCase):

    def test_friday_can_take_a_half_day(self):
        mhschedule.set_rule(self.store, "float_max_load", "medium")
        mhschedule.set_rule(self.store, "day_caps", '{"Fri": 0.5}')
        self.add("half day", planned_day=FRI, load="medium")
        self.assertEqual(self.findings("lane") + self.findings("day_cap"), [])
        self.add("another", planned_day=FRI, load="shallow")
        self.assertEqual(self.findings("day_cap")[0]["message"],
                         f"{FRI} carries 0.75 days against a 0.5 cap")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CliCase(StoreCase):

    def setUp(self):
        super().setUp()
        (self.repo / "priorities.md").write_text("# P\n\n## Weekly Goals\n")

    def run_cli(self, *argv):
        buf, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stdout(buf), redirect_stderr(err):
            code = mhcli.main(["--repo", str(self.repo), "--no-regen", *argv])
        return code, buf.getvalue() + err.getvalue()


class TestCli(CliCase):

    def test_task_add_takes_the_scheduling_fields(self):
        code, out = self.run_cli("task", "add", "outreach", "Tagged", "--load", "deep",
                                 "--done-when", "Sent", "--fixed", "--ready", MON)
        self.assertEqual(code, 0, out)
        row = self.store.tasks(project_key="outreach")[-1]
        self.assertEqual((row["load"], row["done_when"], row["fixed"],
                          row["external_ready_date"]), ("deep", "Sent", 1, MON))

    def test_skill_check_accepts_the_new_verbs(self):
        parser = mhcli.build_parser()
        for line in ('mh task add <project> "<title>" --load <load> --done-when "<done-when>"',
                     "mh dep add <task> --on <task> --gap 3",
                     "mh dep add <task> --on <task> --same-day",
                     "mh schedule validate --project <project>"):
            self.assertIsNone(check_mh_usage.check(line, parser), line)

    def test_dep_add_list_rm(self):
        a = self.add("initial review", display_ord="1")
        b = self.add("final review", display_ord="2")
        code, out = self.run_cli("dep", "add", "aba-academy#2", "--on", "aba-academy#1",
                                 "--gap", "3", "--actor", "sam")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.deps(task_id=b["id"])[0]["min_gap_days"], 3)
        code, out = self.run_cli("dep", "list", "aba-academy#1")
        self.assertIn("blocks", out)
        self.assertIn("aba-academy#2", out)
        code, out = self.run_cli("dep", "list", "aba-academy")
        self.assertIn("waits on", out)
        self.assertIn("+3d", out)
        code, out = self.run_cli("dep", "add", "aba-academy#1", "--on", "aba-academy#2")
        self.assertEqual(code, 1)
        self.assertIn("cycle", out)
        code, _ = self.run_cli("dep", "rm", "aba-academy#2", "--on", "aba-academy#1")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.deps(), [])
        code, _ = self.run_cli("dep", "rm", "aba-academy#2", "--on", "aba-academy#1")
        self.assertEqual(code, 1)

    def test_validate_is_read_only_and_exits_on_violations(self):
        self.add("a", planned_day=MON, load="deep", display_ord="1")
        before = self.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        code, out = self.run_cli("schedule", "validate", "--from", MON)
        self.assertEqual(code, 0, out)
        self.add("b", planned_day=MON, load="deep", display_ord="2")
        code, out = self.run_cli("schedule", "validate", "--from", MON)
        self.assertEqual(code, 1)
        self.assertIn("aba-academy#1, aba-academy#2", out)
        code, out = self.run_cli("schedule", "validate", "--from", MON, "--json")
        self.assertEqual(json.loads(out)[0]["rule"], "day_cap")
        after = self.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(after, before + 1)       # the one add, no validate writes

    def test_rules_and_set(self):
        code, out = self.run_cli("schedule", "set", "day_cap", "2", "--actor", "sam")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("schedule", "rules")
        self.assertIn("day_cap", out)
        self.assertIn("setting", out)
        code, _ = self.run_cli("schedule", "set", "day_cap", "lots")
        self.assertEqual(code, 1)

    def test_template_verbs(self):
        code, out = self.run_cli("template", "show", "course")
        self.assertIn("after 6", out)
        self.assertIn("same day as 9", out)
        code, out = self.run_cli("template", "apply", "champions", "aba-academy",
                                 "--prefix", "Champions November", "--actor", "sam")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.store.tasks(project_key="aba-academy")), 11)
        agenda = self.add("Extracting: review the VILT agenda", display_ord="90")
        guide = self.add("Extracting: initial facilitation guide review (stage 9)",
                         display_ord="91")
        code, out = self.run_cli("template", "link", "course", "aba-academy",
                                 "--prefix", "Extracting", "--stage", "6=aba-academy#90")
        self.assertEqual(code, 0, out)
        self.assertIn("stage  6: aba-academy#90", out)
        self.assertEqual(self.store.deps(task_id=guide["id"])[0]["depends_on_id"],
                         agenda["id"])
        code, out = self.run_cli("template", "link", "course", "aba-academy",
                                 "--prefix", "Extracting", "--stage", "six=aba-academy#90")
        self.assertEqual(code, 1)

    def test_project_lane(self):
        code, out = self.run_cli("project", "set", "outreach", "--lane", "aba")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.lane("outreach"), "aba")
        self.run_cli("project", "set", "outreach", "--lane", "auto")
        self.assertEqual(self.store.lane("outreach"), "business")

    def test_task_set_fixed_ready_done_when(self):
        t = self.add("x", display_ord="1")
        code, out = self.run_cli("task", "set", "aba-academy#1", "--fixed", "yes",
                                 "--ready", MON, "--done-when", "Signed off")
        self.assertEqual(code, 0, out)
        row = self.store.task(t["id"])
        self.assertEqual((row["fixed"], row["external_ready_date"], row["done_when"]),
                         (1, MON, "Signed off"))
        self.run_cli("task", "set", "aba-academy#1", "--fixed", "no", "--ready", "none")
        row = self.store.task(t["id"])
        self.assertEqual((row["fixed"], row["external_ready_date"]), (0, None))
        with self.assertRaises(SystemExit):
            self.run_cli("task", "set", "aba-academy#1", "--ready", "10/12")


if __name__ == "__main__":
    unittest.main()
