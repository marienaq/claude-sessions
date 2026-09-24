#!/usr/bin/env python3
"""
mhschedule — the scheduling rules, and the check that holds a plan to them.

One module, imported by mh and by the session manager, so the command line
and the page can never disagree about what a violation is. Stdlib only.

Two failures justified this (operations/ai-workflows/scheduling-and-capacity/
design.md): a placer with no dependency logic wrote an impossible schedule
into the live store, and every rule found in conversation was lost between
runs. So the rules live in the store's settings, the dependencies in its
deps table, and validate() reads nothing else.

validate() takes an optional placements map (task id -> day) laid over the
committed days. That is what a scenario is: the same check, run against a
plan nobody has written down yet.
"""

import json
from datetime import date, timedelta

import mhstore
from mhstore import OPEN_STATUSES, tag

# The rules and their defaults. A settings row named schedule.<name> holds
# JSON and wins over the default; nothing else is read. The older settings
# business_days, max_aba_days and max_deep_per_week are not consulted: they
# were never read by any code, and two competing sets is what this replaces.
DEFAULTS = {
    # Days of work per load tag. MQ: full day is Deep, half day is Medium,
    # small is Shallow.
    "load_weights": {"deep": 1.0, "medium": 0.5, "shallow": 0.25},
    # The day a row with no load tag is counted as, so it is not free.
    "untagged_load": "medium",
    "day_cap": 1.5,
    # A cap for a named day that differs from day_cap, e.g. {"Fri": 0.5}.
    "day_caps": {},
    "week_target": 2.0,
    "week_stretch": 2.5,
    # MQ 2026-09-24: Monday and Wednesday are ABA, Tuesday and Thursday are
    # business, Friday floats and takes small tasks only unless she grants
    # an exception. A day not listed is not a working day.
    "days": {"Mon": "aba", "Tue": "business", "Wed": "aba",
             "Thu": "business", "Fri": "float"},
    # The heaviest load a float day takes.
    "float_max_load": "shallow",
}

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_KINDS = ("aba", "business", "float")

# The order findings are reported in, and what each is called.
RULES = (
    ("dependency", "A row is planned before what it waits on, or too soon after"),
    ("same_day", "Rows that must share a day do not"),
    ("ready", "A row is planned before the other party delivers"),
    ("day_cap", "A day carries more than the day cap"),
    ("lane", "A row sits on a day that belongs to another kind of work"),
    ("week", "A week is over the stretch line, or under the target"),
    ("untagged", "Open rows with no load tag, counted as the untagged load"),
    ("backlog", "Open rows with no planned day"),
)


class RulesError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _check_rule(name, value):
    """A rule value as validate() needs it, or RulesError."""
    if name not in DEFAULTS:
        raise RulesError(f"no rule {name!r}; rules are {', '.join(DEFAULTS)}")
    if name in ("day_cap", "week_target", "week_stretch"):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise RulesError(f"{name} is a number of days, not {value!r}")
        if value <= 0:
            raise RulesError(f"{name} must be more than zero")
        return value
    if name in ("untagged_load", "float_max_load"):
        if value not in mhstore.LOADS:
            raise RulesError(f"{name} is one of {', '.join(mhstore.LOADS)}")
        return value
    if name == "load_weights":
        if not isinstance(value, dict) or set(value) != set(mhstore.LOADS):
            raise RulesError(f"load_weights names each of {', '.join(mhstore.LOADS)}")
        return {k: float(v) for k, v in value.items()}
    if name == "day_caps":
        if not isinstance(value, dict) or any(d not in DAY_NAMES for d in value):
            raise RulesError("day_caps maps a day (Mon..Sun) to a number of days")
        try:
            return {d: float(v) for d, v in value.items()}
        except (TypeError, ValueError):
            raise RulesError("day_caps values are numbers of days")
    if name == "days":
        if not isinstance(value, dict):
            raise RulesError("days maps Mon..Sun to aba, business or float")
        for day, kind in value.items():
            if day not in DAY_NAMES or kind not in DAY_KINDS:
                raise RulesError(f"days: {day}={kind} is not a day and one of "
                                 f"{', '.join(DAY_KINDS)}")
        return dict(value)
    return value


def rules(store):
    """Every rule's effective value, with where it came from."""
    out = {}
    for name, default in DEFAULTS.items():
        raw = store.setting(f"schedule.{name}")
        if raw is None:
            out[name] = {"value": default, "source": "default"}
            continue
        try:
            out[name] = {"value": _check_rule(name, json.loads(raw)),
                         "source": "setting"}
        except (ValueError, RulesError):
            # A bad setting must not silently become the default: say so.
            raise RulesError(f"setting schedule.{name} = {raw!r} is not valid")
    return out


def set_rule(store, name, text, actor="system"):
    """Parse text as JSON (or a bare word), check it, store it."""
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    value = _check_rule(name, value)
    store.set_setting(f"schedule.{name}", json.dumps(value), actor=actor)
    return value


def weight(task, r):
    """Days of work a row stands for, and whether its load was assumed."""
    load = task.get("load")
    if load in r["load_weights"]:
        return r["load_weights"][load], False
    return r["load_weights"][r["untagged_load"]], True


def week_start(day):
    d = date.fromisoformat(day) if isinstance(day, str) else day
    return (d - timedelta(days=d.weekday())).isoformat()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _finding(rule, level, message, tasks=(), day=None, **extra):
    return {"rule": rule, "level": level, "message": message,
            "tasks": [t["id"] for t in tasks], "tags": [tag(t) for t in tasks],
            "day": day, **extra}


def validate(store, placements=None, start=None):
    """
    Every rule broken by the plan, as a list of findings.

    placements  {task id: 'YYYY-MM-DD' or None}, laid over planned_day; a
                scenario. None means the committed schedule.
    start       the first day capacity is checked from, default today. The
                ordering rules look at every open row regardless: a stage
                planned before its predecessor is wrong even in the past.

    Each finding is {rule, level, message, tasks, tags, day}. level is
    'violation' for a broken rule and 'notice' for something to look at
    (an under-filled week, the backlog). Only the primary user's rows count
    toward capacity; a contractor's build window is not MQ's day.
    """
    r = {k: v["value"] for k, v in rules(store).items()}
    placements = placements or {}
    start = start or date.today().isoformat()
    me = store.primary_user

    tasks = {t["id"]: t for t in store.tasks()}

    def day_of(t):
        # A placement of None is a scenario taking the row off the plan.
        return placements[t["id"]] if t["id"] in placements else t["planned_day"]

    def is_open(t):
        return t["status"] in OPEN_STATUSES

    found = []

    # -- ordering ---------------------------------------------------------
    for dep in store.deps():
        t, p = tasks.get(dep["task_id"]), tasks.get(dep["depends_on_id"])
        if not t or not p or not is_open(t) or p["status"] == "canceled":
            continue
        t_day, p_day = day_of(t), day_of(p)
        if dep["kind"] == "same_day":
            if is_open(p) and t_day and p_day and t_day != p_day:
                found.append(_finding(
                    "same_day", "violation",
                    f"{tag(t)} is on {t_day} and {tag(p)} on {p_day}; "
                    f"they go together", (t, p), t_day))
            continue
        if p["status"] == "done" or not t_day:
            continue
        if not p_day:
            found.append(_finding(
                "dependency", "violation",
                f"{tag(t)} is planned for {t_day} but waits on {tag(p)}, "
                f"which is not planned", (t, p), t_day))
            continue
        gap = (date.fromisoformat(t_day) - date.fromisoformat(p_day)).days
        need = dep["min_gap_days"]
        if gap < need:
            if gap < 0:
                how = f"{-gap} days before {tag(p)}"
            elif gap == 0:
                how = f"the same day as {tag(p)}"
            else:
                how = f"{gap} days after {tag(p)}"
            rule = f"; needs {need}" if need else ""
            found.append(_finding("dependency", "violation",
                                  f"{tag(t)} ({t_day}) is {how} ({p_day}){rule}",
                                  (t, p), t_day, gap=gap, min_gap=need))

    for t in tasks.values():
        ready, t_day = t.get("external_ready_date"), day_of(t)
        if is_open(t) and ready and t_day and t_day < ready:
            found.append(_finding(
                "ready", "violation",
                f"{tag(t)} is planned for {t_day}; what it reviews "
                f"arrives {ready}", (t,), t_day))

    # -- capacity: MQ's open rows from start on ------------------------------
    mine = [t for t in tasks.values() if is_open(t) and t["owner"] == me]
    by_day = {}
    for t in mine:
        d = day_of(t)
        if d and d >= start:
            by_day.setdefault(d, []).append(t)

    for d in sorted(by_day):
        rows = by_day[d]
        total = sum(weight(t, r)[0] for t in rows)
        movable = [t for t in rows if not t["fixed"]]
        name = DAY_NAMES[date.fromisoformat(d).weekday()]
        cap = r["day_caps"].get(name, r["day_cap"])
        if total > cap + 1e-9 and movable:
            found.append(_finding(
                "day_cap", "violation",
                f"{d} carries {total:.2f} days against a {cap:g} cap",
                rows, d, load=round(total, 2)))

        kind = r["days"].get(name)
        for t in movable:
            lane = store.lane(t["project_key"])
            if kind is None:
                found.append(_finding("lane", "violation",
                                      f"{tag(t)} is on {name} {d}, not a working day",
                                      (t,), d))
            elif kind == "float":
                w, _ = weight(t, r)
                if w > r["load_weights"][r["float_max_load"]] + 1e-9:
                    found.append(_finding(
                        "lane", "violation",
                        f"{tag(t)} is {t['load'] or 'untagged'} on {name} {d}; "
                        f"{name} takes {r['float_max_load']} work only", (t,), d))
            elif lane != "any" and lane != kind:
                found.append(_finding(
                    "lane", "violation",
                    f"{tag(t)} is {lane} work on {name} {d}, "
                    f"{'an' if kind == 'aba' else 'a'} {kind} day",
                    (t,), d))

    if by_day:
        weeks = {}
        for d, rows in by_day.items():
            weeks.setdefault(week_start(d), []).extend(rows)
        first = date.fromisoformat(week_start(start))
        last = date.fromisoformat(week_start(max(by_day)))
        w = first
        while w <= last:
            key = w.isoformat()
            rows = weeks.get(key, [])
            total = sum(weight(t, r)[0] for t in rows)
            if total > r["week_stretch"] + 1e-9:
                found.append(_finding(
                    "week", "violation",
                    f"week of {key} carries {total:.2f} days, over the "
                    f"{r['week_stretch']:g} stretch", rows, key, load=round(total, 2)))
            elif total < r["week_target"] - 1e-9:
                found.append(_finding(
                    "week", "notice",
                    f"week of {key} carries {total:.2f} days, under the "
                    f"{r['week_target']:g} target", rows, key, load=round(total, 2)))
            w += timedelta(days=7)

    # -- what is not in the plan --------------------------------------------
    untagged = [t for t in mine if not t["load"]]
    if untagged:
        found.append(_finding(
            "untagged", "notice",
            f"{len(untagged)} open rows have no load tag and are counted as "
            f"{r['untagged_load']}", untagged))

    backlog = [t for t in mine if not day_of(t)]
    if backlog:
        by_project = {}
        for t in backlog:
            by_project.setdefault(t["project_key"], []).append(t)
        total = sum(weight(t, r)[0] for t in backlog)
        found.append(_finding(
            "backlog", "notice",
            f"{len(backlog)} open rows ({total:.1f} days) have no planned day",
            backlog, None, load=round(total, 2),
            by_project={k: {"rows": len(v),
                            "days": round(sum(weight(t, r)[0] for t in v), 2)}
                        for k, v in sorted(by_project.items())}))

    order = {name: i for i, (name, _) in enumerate(RULES)}
    found.sort(key=lambda f: (order[f["rule"]], f["day"] or "", f["tags"]))
    return found


def violations(findings):
    return [f for f in findings if f["level"] == "violation"]


# ---------------------------------------------------------------------------
# Pipeline templates
#
# Transcribed from methodology/course-build-pipeline.md and
# champions-session-pipeline.md. When one of those changes, this changes
# with it; the tests hold the shape, not the prose.
#
# Each stage: (n, name, load, after, same_day, fixed, done_when). after is
# ((stage, min_gap_days), ...); same_day names stages it shares a day with.
# ---------------------------------------------------------------------------

def _stage(n, name, load, after=(), same_day=(), fixed=False, done_when=None):
    return {"n": n, "name": name, "load": load,
            "after": tuple((a, 0) if isinstance(a, int) else a for a in after),
            "same_day": tuple(same_day), "fixed": fixed,
            "done_when": done_when}


# The course pipeline is not a chain. 1 to 6 are sequential; then four review
# tracks (Rise, facilitation guide, slides, QRG), each gated on 6 alone and on
# Anushka delivering, each an initial and a final at least 3 days apart; 15
# waits on all four finals. The guide and slides reviews share a day: MQ
# reviews them together before anything goes to Sharla (the Sharla gate).
COURSE = (
    _stage(1, "research", "shallow",
           done_when="Best practice and what the course should cover, written up"),
    _stage(2, "scoping", "deep", after=(1,),
           done_when="The teaching thesis and a test plan kit"),
    _stage(3, "testing", "deep", after=(2,),
           done_when="Raw notes from running the test plan kit in Copilot"),
    _stage(4, "finalized scope", "deep", after=(3,),
           done_when="Recommendation and course outline sent to Anushka and on Drive"),
    _stage(5, "review with Anushka", "medium", after=(4,),
           done_when="Findings shared and the build handed over"),
    _stage(6, "review course outline", "medium", after=(5,),
           done_when="Feedback on the course outline and the VILT agenda and session outline"),
    _stage(7, "initial Rise review", "medium", after=(6,),
           done_when="Feedback on Anushka's Rise draft"),
    _stage(8, "final Rise review", "medium", after=((7, 3),),
           done_when="Rise course signed off"),
    _stage(9, "initial facilitation guide review", "medium", after=(6,),
           done_when="Feedback on Anushka's facilitation guide draft"),
    _stage(10, "final facilitation guide review", "medium", after=((9, 3),),
           done_when="Facilitation guide signed off"),
    _stage(11, "initial slides review", "medium", after=(6,), same_day=(9,),
           done_when="Feedback on Anushka's slides draft"),
    _stage(12, "final slides review", "medium", after=((11, 3),), same_day=(10,),
           done_when="Slides signed off, and with the guide, sent to Sharla"),
    _stage(13, "initial QRG review", "medium", after=(6,),
           done_when="Feedback on Anushka's QRG draft"),
    _stage(14, "final QRG review", "medium", after=((13, 3),),
           done_when="QRG signed off"),
    _stage(15, "VILT prep", "medium", after=(8, 10, 12, 14),
           done_when="Ready to run"),
    _stage(16, "deliver the workshop", "medium", after=(15,), fixed=True,
           done_when="The session ran"),
)

# One monthly session, and a week per stage rather than a load sum. Concepts
# go to Sharla and the build waits a week for her; the guide, slides and
# reference guide are built on one day on purpose (1.5 days, accepted). No
# rehearsal: MQ does not run one.
CHAMPIONS = (
    _stage(1, "session brief and scope", "medium",
           done_when="Brief and scope agreed"),
    _stage(2, "finalize concepts", "medium", after=(1,),
           done_when="Concepts sent to Sharla"),
    _stage(3, "pre-work email", "shallow", after=(1,),
           done_when="Sharla's revisions in and the email sent"),
    _stage(4, "facilitation guide", "medium", after=((2, 7),),
           done_when="Facilitation guide built and aligned with the slides"),
    _stage(5, "slides", "medium", after=((2, 7),), same_day=(4,),
           done_when="Slides built and aligned with the guide"),
    _stage(6, "reference guide", "medium", after=((2, 7),), same_day=(4,),
           done_when="Reference guide built"),
    _stage(7, "polls", "shallow", after=(1,),
           done_when="Polls written"),
    _stage(8, "build the demo", "medium", after=((2, 7),),
           done_when="Demo built and run through once"),
    _stage(9, "incorporate Sharla's feedback and finalize", "medium",
           after=(4, 5, 6),
           done_when="Sharla's feedback in and the materials final"),
    _stage(10, "deliver the session", "shallow", after=(3, 7, 8, 9), fixed=True,
           done_when="The session ran"),
    _stage(11, "post-session follow-up", "shallow", after=(10,),
           done_when="Follow-up sent"),
)

TEMPLATES = {
    "course": {"stages": COURSE,
               "about": "course build, 16 stages, four parallel review tracks",
               # Self-paced courses drop the guide, slides and QRG reviews
               # and the VILT (course-build-pipeline.md, Known variations).
               "self_paced_drops": (9, 10, 11, 12, 13, 14, 15, 16)},
    "champions": {"stages": CHAMPIONS,
                  "about": "one AI Champions session, 11 stages",
                  "self_paced_drops": ()},
}


def template(name, self_paced=False):
    if name not in TEMPLATES:
        raise RulesError(f"no template {name!r}; there are {', '.join(TEMPLATES)}")
    t = TEMPLATES[name]
    if self_paced and not t["self_paced_drops"]:
        raise RulesError(f"{name} has no self-paced variant")
    drop = set(t["self_paced_drops"]) if self_paced else set()
    return [st for st in t["stages"] if st["n"] not in drop]


def stage_title(prefix, st):
    return f"{prefix}: {st['name']} (stage {st['n']})"


def _wire(store, stages, rows, actor):
    """
    Dependencies between stage rows. rows maps stage -> [task rows]; a stage
    with no rows is bridged, so its dependents wait on its predecessors
    instead (a closed stage 5 that was never recorded must not free stage 6
    from stage 4). Returns the number of dependencies written.
    """
    by_n = {st["n"]: st for st in stages}

    def preds(n):
        """
        {row id: gap} stage n waits on, walking past unmapped stages. Only
        the gap on the edge that reaches a real row counts: a missing stage
        passes on what it waited for, not its own spacing. Extracting has no
        QRG reviews, and their 3-day pair must not delay its VILT prep.
        """
        out = {}
        for p, g in by_n[n]["after"]:
            found = {r["id"]: g for r in rows.get(p, [])} or preds(p)
            for rid, gap in found.items():
                out[rid] = max(out.get(rid, 0), gap)
        return out

    n_deps = 0
    for st in stages:
        for row in rows.get(st["n"], []):
            for pred_id, gap in preds(st["n"]).items():
                store.add_dep(row["id"], pred_id, actor=actor, gap=gap)
                n_deps += 1
            for other in st["same_day"]:
                for pred in rows.get(other, []):
                    store.add_dep(row["id"], pred["id"], actor=actor,
                                  kind="same_day")
                    n_deps += 1
    return n_deps


def apply_template(store, name, project_key, prefix, actor="system",
                   self_paced=False, owner=None):
    """Create every stage row and its dependencies in one transaction."""
    stages = template(name, self_paced)
    prefix = prefix.strip()
    if not prefix:
        raise RulesError("a template needs --prefix, the course or session name")
    rows = {}
    with store.write():
        for st in stages:
            rows[st["n"]] = [store.add_task(
                project_key, stage_title(prefix, st), actor=actor,
                load=st["load"], fixed=st["fixed"], done_when=st["done_when"],
                owner=owner, source="planning")]
        n_deps = _wire(store, stages, rows, actor)
    return rows, n_deps


def link_template(store, name, project_key, prefix, overrides=None,
                  actor="system", self_paced=False):
    """
    Wire an in-flight course's existing rows to the template's dependencies.

    A row belongs to stage N if its title starts with the prefix and says
    "(stage N)"; overrides ({N: [task rows]}) name rows whose titles do not,
    or a stage split into two rows. Nothing is created. Returns
    (rows by stage, stages with no row, dependencies written).
    """
    import re
    stages = template(name, self_paced)
    overrides = overrides or {}
    known = {st["n"] for st in stages}
    bad = set(overrides) - known
    if bad:
        raise RulesError(f"{name} has no stage {sorted(bad)}")
    rows = {}
    want = prefix.strip().lower()
    for t in store.tasks(project_key=project_key):
        if t["status"] == "canceled" or not t["title"].lower().startswith(want):
            continue
        m = re.search(r"\(stage (\d+)\)", t["title"])
        if m and int(m.group(1)) in known:
            rows.setdefault(int(m.group(1)), []).append(t)
    rows.update(overrides)
    with store.write():
        n_deps = _wire(store, stages, rows, actor)
    missing = [st["n"] for st in stages if not rows.get(st["n"])]
    return rows, missing, n_deps
