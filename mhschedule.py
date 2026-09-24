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
        if total > r["day_cap"] + 1e-9 and movable:
            found.append(_finding(
                "day_cap", "violation",
                f"{d} carries {total:.2f} days against a {r['day_cap']:g} cap",
                rows, d, load=round(total, 2)))

        name = DAY_NAMES[date.fromisoformat(d).weekday()]
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
