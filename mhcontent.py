"""
mh content — the content-planning app's database, for agents.

The app (mellonhead-internal-operations, Supabase schema `content`) is where
ideas, takes, stories, posts and signals live. Agents read and write them
here instead of editing ideas.md, takes.md or story-bank.md.

    mh content ideas --status proposed
    mh content idea show dyslexia
    mh content idea add "Chatbot told me to quit" --pillar Messy --channel TikTok
    mh content idea set dyslexia feeling cheeky
    mh content idea status dyslexia "on deck"
    mh content idea note dyslexia "Storyboard is in marketing/tiktok/..."
    mh content post add context YouTube --on 2026-12-02 --url https://...
    mh content signal add <post> --n views=1200 --n likes=80 --more yes

Rules it keeps, the same ones the app keeps:
- Every write is checked against the row's updated_at and retried on a clash,
  so an agent never overwrites an edit she made in the app a moment earlier.
- A pitch field an agent fills in is marked drafted (dashed in the app) until
  she confirms it. --hers records her own words as hers.
- Changing the feeling, take, angle, style or on-camera setup of an idea that
  has hooks sets "re-hook needed".
- Option fields only take values from the latest config version.
- Nothing is deleted: `archive` hides a row.
- The writer is recorded by name (--actor, sent as the x-mh-actor header).

Credentials: SUPABASE_URL and SUPABASE_SECRET_KEY from the environment, or
from ~/.config/mellonhead/supabase.env. The key is never printed.

Stdlib only, like the rest of mh.
"""

import json
import os
import random
import time
import unicodedata
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ENV_FILE = Path.home() / ".config" / "mellonhead" / "supabase.env"
SCHEMA = "content"

# Statuses that sit in a week (the app clears the week on the others).
WEEK_STATUSES = ("on deck", "in progress", "scheduled", "published")
STATUSES = ["proposed", "on deck", "in progress", "scheduled", "published",
            "backlog", "rejected"]
ORIGINS = ["experience", "read", "research", "client", "agent"]
INNER_WORLD = ["coreProblem", "hurting", "tried", "thinking", "afraid", "wants"]
MORE = ["yes", "no", "maybe"]
NO_SERIES = "none"
HER_NAMES = {"you", "mq", "mariena", "her", "mariena quintanilla"}
# "No story from the bank", a decision. `unset` puts it back to not picked.
STORY_NA = ("none", "n/a", "na", "no story", "")
DEFAULT_CHANNELS = ["TikTok", "LinkedIn", "LinkedIn newsletter", "YouTube",
                    "Mellonmail", "community"]

# What each platform reports. Keep in step with METRICS in the app's
# src/lib/types.ts: signals store numbers under these keys.
METRICS = {
    "TikTok": ["views", "likes", "comments", "saves", "shares", "avg_watch_seconds"],
    "YouTube": ["views", "watch_hours", "avg_view_seconds", "likes", "comments",
                "subscribers_gained"],
    "Mellonmail": ["sent", "open_rate", "click_rate", "unsubscribes", "replies"],
    "LinkedIn": ["impressions", "reactions", "comments", "reposts"],
    "LinkedIn newsletter": ["views", "reactions", "comments", "subscribers_gained"],
    "community": ["replies", "reactions"],
}

# Fields `idea set` can change: CLI name -> (column, drafted name or None,
# option list in config or a check). Fields that are hers alone (the gut
# check, the hook she picked, her own hook) are deliberately not here.
FIELDS = {
    "title": ("title", None, None),
    "summary": ("summary", "summary", None),
    "pillar": ("pillar", "pillar", "pillar"),
    "type": ("type", "type", "type"),
    "feeling": ("feeling", "feeling", "feeling"),
    "style": ("style", "style", "style"),
    "on-camera": ("on_camera", "onCamera", "onCamera"),
    "voice": ("voice", "voice", "voice"),
    "harm-help": ("harm_help", "harmHelp", "harmHelp"),
    "cta": ("cta", "cta", "cta"),
    "cta-link": ("cta_link", None, None),
    "series": ("series", "series", "series"),
    "ep": ("ep", None, "int"),
    "safety": ("safety", "safety", "safety"),
    "origin": ("origin", None, "origin"),
    "brief": ("brief", None, None),
    "source-url": ("source_url", None, None),
    "source-label": ("source_label", None, None),
    "slate": ("slate", None, None),
    "post-date": ("post_date", None, "date"),
    "week": ("week", None, "monday"),
    "take": ("take_n", "take", "take"),
    "story": ("story_key", "story", "story"),
}
LOOK = ["setup", "outfit", "location", "props", "edit"]
# Changing one of these on an idea with hooks means the hooks need redoing
# (REHOOK in the app's logic.ts). Keyed by drafted name.
REHOOK = {"feeling", "take", "summary", "style", "onCamera"}
NULLABLE = {"cta_link", "source_url", "source_label", "post_date", "week", "ep",
            "take_n", "story_key"}

# JSON fields `idea put` replaces whole, with the shape each must have.
PUT = {
    "hooks": "list of {spoken, onscreen, frame, format, payoff, rec}",
    "beats": "list of {t, gap?, waitingOn?}",
    "script": "list of {t, d, g?} (d true = drafted)",
    "prep": "list of strings",
    "tips": "{Dawn, Alex, Paloma}",
    "look": "{setup, outfit, location, props, edit}",
}


class ContentError(Exception):
    pass


class SkipMove(Exception):
    """--only-from: the idea is in some other status, so it is left alone."""
    def __init__(self, row):
        self.row = row


# ---------------------------------------------------------------- transport

def load_credentials(env=None, path=ENV_FILE):
    """URL and key always come from one place: both from the environment, or
    both from the file. Mixing them could send this project's key to a URL
    meant for another (a local `supabase start`, say)."""
    env = os.environ if env is None else env
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SECRET_KEY")
    if bool(url) != bool(key):
        # Half a setting is a typo, not a request for the default: falling back
        # to the file would point a sandbox session at the live database.
        raise ContentError("only one of SUPABASE_URL and SUPABASE_SECRET_KEY is set; "
                           "set both, or neither to use " + str(path))
    if not url and Path(path).exists():
        vals = {}
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            vals[k.strip()] = v
        url, key = vals.get("SUPABASE_URL"), vals.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        raise ContentError(
            f"no database settings: set SUPABASE_URL and SUPABASE_SECRET_KEY "
            f"together, or put both in {path}")
    url, key = url.strip(), key.strip()
    host = urllib.parse.urlsplit(url).hostname or ""
    if not url.startswith("https://") and host not in ("localhost", "127.0.0.1", "::1"):
        raise ContentError("SUPABASE_URL must be https (the key would travel in the clear)")
    if not re.fullmatch(r"[\x21-\x7e]+", key):
        raise ContentError("SUPABASE_SECRET_KEY has spaces or odd characters in it; check the setting")
    return url.rstrip("/"), key


class Api:
    """PostgREST, through urllib. Tests swap in a fake with the same methods."""

    def __init__(self, url, key, actor, timeout=20):
        self.base = url + "/rest/v1/"
        self.key = key
        actor = (actor or "agent").strip()[:80]
        if not re.fullmatch(r"[\x20-\x7e]+", actor):
            raise ContentError("--actor: plain letters, numbers and punctuation only")
        self.actor = actor
        self.timeout = timeout

    def _call(self, method, table, params=None, body=None):
        q = urllib.parse.urlencode(params or {}, safe=",.()*:")
        url = self.base + table + ("?" + q if q else "")
        headers = {
            "apikey": self.key, "Authorization": "Bearer " + self.key,
            "Accept-Profile": SCHEMA, "Content-Profile": SCHEMA,
            "Accept": "application/json", "x-mh-actor": self.actor,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            headers["Prefer"] = "return=representation"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode() or "null"
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode() or "{}")
            except ValueError:
                err = {}
            msg = err.get("message") or exc.reason
            code = err.get("code") or exc.code
            detail = f" ({err['details']})" if err.get("details") else ""
            raise ContentError(f"the database refused it: {msg}{detail} [{code}]") from None
        except urllib.error.URLError as exc:
            raise ContentError(f"can't reach the database: {exc.reason}") from None
        except (TimeoutError, OSError):
            if method == "GET":
                raise ContentError("the database didn't answer in time; try again") from None
            raise ContentError(
                "the database didn't answer in time, so this may or may not have been "
                "saved. Check (show/list) before running it again, or it may land twice") from None
        return json.loads(text)

    @staticmethod
    def _where(where):
        params = {}
        for col, val in (where or {}).items():
            if isinstance(val, (list, tuple)):
                params[col] = "in.(" + ",".join(
                    '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
                    for v in val) + ")"
            elif val is None:
                params[col] = "is.null"
            elif isinstance(val, bool):
                params[col] = f"is.{str(val).lower()}"
            else:
                params[col] = f"eq.{val}"
        return params

    def rows(self, table, where=None, order=None, limit=None):
        params = {"select": "*", **self._where(where)}
        if order:
            params["order"] = order
        if limit:
            params["limit"] = str(limit)
        return self._call("GET", table, params)

    def insert(self, table, row):
        out = self._call("POST", table, None, row)
        return out[0] if out else None

    def update(self, table, key_col, key, stamp, cols):
        """Write cols if the row still has updated_at == stamp. None on a clash."""
        params = {key_col: f"eq.{key}", "updated_at": f"eq.{stamp}"}
        out = self._call("PATCH", table, params, cols)
        return out[0] if out else None


# ---------------------------------------------------------------- helpers

def making_week(today=None, film_weekday=None):
    """The app's making week (dates.ts makingWeek): this Monday, or next from
    Friday on; a Friday film day holds the week until Saturday."""
    t = today or date.today()
    dow = (t.weekday() + 1) % 7          # 0 Sunday .. 6 Saturday, as in JS
    monday = t - timedelta(days=(dow + 6) % 7)
    roll = max(5, (3 if film_weekday is None else film_weekday) + 1)
    return monday + timedelta(days=7) if dow == 0 or dow >= roll else monday


def _date(value, what="date"):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ContentError(f"{value!r} is not a {what} (YYYY-MM-DD)") from None


def slug(title):
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    return s[:48].rstrip("-") or "idea"


def _bad_url(url):
    return not (re.match(r"^https?://\S+$", url) or re.match(r"^[\w~.][\w./ ~@()+,-]*$", url))


def check_list(value, what, item_keys=None, required=(), bools=()):
    if not isinstance(value, list):
        raise ContentError(f"{what}: expected a list")
    for n, item in enumerate(value, 1):
        if item_keys is None:
            if not isinstance(item, str):
                raise ContentError(f"{what} item {n}: expected text")
            continue
        if not isinstance(item, dict):
            raise ContentError(f"{what} item {n}: expected an object")
        extra = set(item) - set(item_keys)
        if extra:
            raise ContentError(f"{what} item {n}: unknown keys {sorted(extra)}; "
                               f"allowed {item_keys}")
        for k in required:
            if k not in item:
                raise ContentError(f"{what} item {n}: missing {k!r}")
        for k, v in item.items():
            if k in bools:
                if not isinstance(v, bool):
                    raise ContentError(f"{what} item {n}: {k!r} must be true or false")
            elif not isinstance(v, str):
                raise ContentError(f"{what} item {n}: {k!r} must be text")


# ---------------------------------------------------------------- the store

class Content:
    def __init__(self, api, actor="agent", today=None):
        self.api = api
        self.actor = actor or "agent"
        self.today = today or date.today()
        self._config = None

    # reads
    def config(self):
        if self._config is None:
            rows = self.api.rows("config_versions", order="n.desc", limit=1)
            if not rows:
                raise ContentError("no config version in the database")
            snap = rows[0]["snapshot"]
            snap.setdefault("opts", {}).setdefault("channel", list(DEFAULT_CHANNELS))
            snap.setdefault("retired", {}).setdefault("channel", [])
            self._config = {"n": rows[0]["n"], "label": rows[0]["label"], **snap}
        return self._config

    def this_week(self):
        fw = (self.config().get("config") or {}).get("filmWeekday")
        return making_week(self.today, fw)

    def _one(self, table, key_col, key, what, archived_ok=False):
        rows = self.api.rows(table, {key_col: key})
        if not rows:
            raise ContentError(f"no {what} {key!r}")
        if rows[0].get("archived") and not archived_ok:
            raise ContentError(f"{what} {key!r} is archived")
        return rows[0]

    def idea(self, ident, archived_ok=False):
        return self._one("ideas", "id", ident, "idea", archived_ok)

    def ideas(self, statuses=None, week=None, channel=None, include_archived=False,
              link=None, words=None):
        if week:
            week = self.parse_value("week", week)
        if channel:
            names = {c.lower(): c for c in self.config()["opts"]["channel"]}
            if channel.lower() not in names:
                raise ContentError(f"{channel!r} is not a channel; channels: {', '.join(names.values())}")
            channel = names[channel.lower()]
        where = {}
        if statuses:
            where["status"] = list(statuses)
        if week:
            where["week"] = week
        if not include_archived:
            where["archived"] = False
        rows = self.api.rows("ideas", where, order="updated_at.desc")
        if channel:
            rows = [r for r in rows if channel in (r.get("channels") or [])]
        if link:
            want = link.strip().rstrip("/")
            rows = [r for r in rows if want in
                    {(u or "").rstrip("/") for u in [r.get("brief"), r.get("source_url"), r.get("cta_link")]
                     + [l["url"] for l in r.get("links") or []]}]
        if words:
            ws = words.casefold().split()
            rows = [r for r in rows
                    if all(w in (r["title"] + " " + (r.get("summary") or "")).casefold() for w in ws)]
        return rows

    def post(self, ident):
        """A post by its id, or by the first characters of it as printed."""
        ident = ident.strip().lower()
        if re.fullmatch(r"[0-9a-f-]{36}", ident):
            return self._one("posts", "id", ident, "post")
        if not re.fullmatch(r"[0-9a-f]{6,}", ident):
            raise ContentError(f"{ident!r} is not a post id (give the id, or at least its first 6 characters)")
        hits = [p for p in self.api.rows("posts", {"archived": False})
                if p["id"].startswith(ident)]
        if not hits:
            raise ContentError(f"no post {ident!r}")
        if len(hits) > 1:
            raise ContentError(f"{ident!r} matches {len(hits)} posts; give more of the id")
        return hits[0]

    # the guarded write
    def change(self, table, key_col, key, fn, what, tries=8):
        """Read the row, let fn work out the new columns, write them only if
        nobody changed the row since, and start over if somebody did."""
        for attempt in range(tries):
            if attempt:
                time.sleep(random.uniform(0.05, 0.25) * attempt)   # let the other writer finish
            row = self._one(table, key_col, key, what)
            cols = fn(dict(row))
            if not cols:
                return row, False
            out = self.api.update(table, key_col, key, row["updated_at"], cols)
            if out is not None:
                return out, True
        raise ContentError(f"{what} {key!r} kept changing while this ran; try again")

    # option checks
    def check_opt(self, kind, value):
        if value == "" or (kind == "series" and value == NO_SERIES):
            return value
        cfg = self.config()
        if kind in ("pillar", "pillars"):
            names = [p["name"] for p in cfg.get("pillars", [])]
            parts = [x.strip() for x in value.split(",")] if kind == "pillars" else [value]
            for x in parts:
                if x not in names:
                    raise ContentError(f"{x!r} is not a pillar; pillars: {', '.join(names)}")
            return ", ".join(parts)
        opts = cfg.get("opts", {}).get(kind)
        if opts is None:
            return value
        if value not in opts:
            raise ContentError(f"{value!r} is not a {kind} option; options: {', '.join(opts)}")
        if value in cfg.get("retired", {}).get(kind, []):
            raise ContentError(f"{value!r} is retired in config; pick a current {kind}")
        return value

    def parse_value(self, field, raw):
        col, _drafted, kind = FIELDS[field]
        if raw in ("", "none", "null") and col in NULLABLE and kind != "story":
            return None
        if kind == "int":
            if not re.fullmatch(r"\d{1,4}", raw):
                raise ContentError(f"{field}: {raw!r} is not a number")
            return int(raw)
        if kind == "date":
            return _date(raw).isoformat()
        if kind == "monday":
            d = _date(raw, "week (a Monday)")
            if d.weekday() != 0:
                raise ContentError(f"week: {raw} is a {d:%A}; weeks start on a Monday")
            return d.isoformat()
        if kind == "origin":
            if raw not in ORIGINS:
                raise ContentError(f"origin: one of {', '.join(ORIGINS)}")
            return raw
        if kind == "take":
            if not re.fullmatch(r"\d+", raw):
                raise ContentError(f"take: a take number, or none")
            self._one("takes", "n", int(raw), "take")
            return int(raw)
        if kind == "story":
            return raw
        if kind:
            return self.check_opt(kind, raw)
        if col == "title" and not raw.strip():
            raise ContentError("an idea needs a title")
        if col in ("source_url", "cta_link") and _bad_url(raw):
            raise ContentError(f"{field}: {raw!r} doesn't look like a link")
        return raw

    def _next_ep(self, series, self_id):
        eps = [r.get("ep") or 0 for r in self.api.rows("ideas", {"series": series, "archived": False})
               if r["id"] != self_id]
        return (max(eps) if eps else 0) + 1

    # ideas: one field
    def set_field(self, ident, field, raw, hers=False):
        if field.startswith("look."):
            part = field[5:]
            if part not in LOOK:
                raise ContentError(f"look parts: {', '.join(LOOK)}")

            def fn(row):
                look = dict(row.get("look") or {})
                if look.get(part, "") == raw:
                    return {}
                look[part] = raw
                return {"look": look}
            return self.change("ideas", "id", ident, fn, "idea")
        if field not in FIELDS:
            raise ContentError(f"unknown field {field!r}; fields: "
                               f"{', '.join(list(FIELDS) + ['look.' + p for p in LOOK])}")
        col, dname, kind = FIELDS[field]
        value = self.parse_value(field, raw)
        if col == "week" and value is not None:
            status = self.idea(ident)["status"]
            if status not in WEEK_STATUSES:
                raise ContentError(f"{ident} is {status}, which has no week; "
                                   f"use `mh content idea status {ident} \"on deck\" --week {value}`")
        story = None
        if kind == "story":
            low = raw.strip().lower()
            if low in STORY_NA:
                story = (None, True)
            elif low == "unset":
                story = (None, False)
            else:
                self._one("stories", "key", raw, "story")
                story = (raw, False)

        def fn(row):
            cols = {}
            if story is not None:
                before = (row.get("story_key"), bool(row.get("no_story")))
                if before != story:
                    cols["story_key"], cols["no_story"] = story
                old, new = row.get("story_key"), story[0]
            else:
                old, new = row.get(col), value
                if old != new:
                    cols[col] = value
                if col == "series" and old != new:
                    cols["ep"] = None if value in ("", NO_SERIES) else self._next_ep(value, row["id"])
            if dname:
                drafted = list(row.get("drafted") or [])
                if story is not None:
                    changed, decided = before != story, story[0] or story[1]
                else:
                    changed, decided = old != new, new not in (None, "")
                # An agent re-setting what's there leaves her confirmation alone.
                if hers or not decided:
                    if dname in drafted:
                        drafted.remove(dname)
                elif changed and dname not in drafted:
                    drafted.append(dname)
                if drafted != (row.get("drafted") or []):
                    cols["drafted"] = drafted
            if dname in REHOOK and old != new and row.get("hooks"):
                show = lambda x: "" if dname == "summary" else (f"#{x}" if dname == "take" and x else str(x or ""))
                cols["rehook"] = {"field": dname,
                                  "from": "the earlier wording" if dname == "summary" else show(old),
                                  "to": "the new wording" if dname == "summary" else show(new)}
            return cols
        return self.change("ideas", "id", ident, fn, "idea")

    # ideas: status, the app's move()
    def set_status(self, ident, status, week=None, post_date=None, note=None, only_from=None):
        if status not in STATUSES:
            raise ContentError(f"status: one of {', '.join(STATUSES)}")
        if week and status not in WEEK_STATUSES:
            raise ContentError(f"{status} has no week; drop --week")
        if week:
            week = self.parse_value("week", week)
        if post_date:
            post_date = _date(post_date).isoformat()
        lock_weeks = None

        def fn(row):
            nonlocal lock_weeks
            if only_from and row["status"] not in only_from and row["status"] != status:
                raise SkipMove(row)
            if status == "scheduled" and not (post_date or row.get("post_date")):
                raise ContentError("scheduled needs --post-date")
            cols = {"status": status} if row["status"] != status else {}
            if status in ("on deck", "in progress"):
                wk = week or row.get("week") or self.this_week().isoformat()
                if row.get("week") != wk:
                    cols["week"] = wk
                if status == "on deck":
                    if lock_weeks is None:
                        lock_weeks = {w["week"] for w in self.api.rows("weeks")
                                      if w.get("reviewed_on")}
                    if wk in lock_weeks and not row.get("added_after_lock"):
                        cols["added_after_lock"] = True
            if status in ("proposed", "backlog", "rejected"):
                if row.get("week") is not None:
                    cols["week"] = None
                if row.get("added_after_lock"):
                    cols["added_after_lock"] = False
            if post_date and row.get("post_date") != post_date:
                cols["post_date"] = post_date
            if note is not None and row.get("reject_note") != note:
                cols["reject_note"] = note
            return cols
        return self.change("ideas", "id", ident, fn, "idea")

    def edit_list(self, ident, col, add=(), remove=(), allowed=None, dname=None, order=None,
                  hers=False):
        for v in list(add) + list(remove):
            if allowed is not None and v not in allowed:
                raise ContentError(f"{v!r} is not one of: {', '.join(allowed)}")

        def fn(row):
            cur = list(row.get(col) or [])
            new = [v for v in cur if v not in remove] + [v for v in add if v not in cur]
            if order:
                new.sort(key=lambda v: order.index(v) if v in order else len(order))
            cols = {col: new} if new != cur else {}
            if dname and cols:
                # An agent's pick is drafted until she confirms it; hers clears it.
                drafted = [d for d in (row.get("drafted") or []) if d != dname]
                if not hers and new:
                    drafted.append(dname)
                if drafted != (row.get("drafted") or []):
                    cols["drafted"] = drafted
            return cols
        return self.change("ideas", "id", ident, fn, "idea")

    def channels(self, ident, add=(), remove=(), hers=False):
        cfg = self.config()
        current = [c for c in cfg["opts"]["channel"] if c not in cfg["retired"]["channel"]]
        for c in add:
            if c not in current:
                raise ContentError(f"{c!r} is not a channel; channels: {', '.join(current)}")
        return self.edit_list(ident, "channels", add, remove, dname="channels",
                              order=cfg["opts"]["channel"], hers=hers)

    def speaks_to(self, ident, add=(), remove=(), hers=False):
        return self.edit_list(ident, "inner_world", add, remove, allowed=INNER_WORLD,
                              dname="innerWorld", order=INNER_WORLD, hers=hers)

    def link(self, ident, label, url):
        if not label.strip():
            raise ContentError("a link needs a label")
        if _bad_url(url):
            raise ContentError(f"{url!r} doesn't look like a link or a repo path")

        def fn(row):
            links = list(row.get("links") or [])
            if url in (row.get("brief"), row.get("source_url")):
                raise ContentError("that's already the brief or the source")
            for l in links:
                if l["url"] == url:
                    if l["label"] == label:
                        return {}
                    l["label"] = label
                    return {"links": links}
            return {"links": links + [{"label": label, "url": url}]}
        return self.change("ideas", "id", ident, fn, "idea")

    def unlink(self, ident, url):
        def fn(row):
            links = list(row.get("links") or [])
            keep = [l for l in links if l["url"] != url]
            if len(keep) == len(links):
                raise ContentError(f"no link {url!r} on {ident}")
            return {"links": keep}
        return self.change("ideas", "id", ident, fn, "idea")

    def note(self, ident, text, who=None):
        if not text.strip():
            raise ContentError("the note is empty")
        if who and who.strip().casefold() in HER_NAMES:
            raise ContentError("a note in her name has to come from her; leave out --who")
        entry = {"who": who or self.actor, "text": text}
        return self.change("ideas", "id", ident,
                           lambda row: {"notes": list(row.get("notes") or []) + [entry]}, "idea")

    def put(self, ident, field, value, force=False):
        if field not in PUT:
            raise ContentError(f"put takes: {', '.join(PUT)}")
        if field == "hooks":
            check_list(value, "hooks", ["spoken", "onscreen", "frame", "format", "payoff", "rec"],
                       required=["spoken"], bools=["rec"])
        elif field == "beats":
            check_list(value, "beats", ["t", "gap", "waitingOn"], required=["t"])
        elif field == "script":
            check_list(value, "script", ["t", "d", "g"], required=["t", "d"], bools=["d"])
        elif field == "prep":
            check_list(value, "prep")
        elif field in ("tips", "look"):
            keys = ["Dawn", "Alex", "Paloma"] if field == "tips" else LOOK
            if not isinstance(value, dict) or set(value) - set(keys) or \
                    not all(isinstance(v, str) for v in value.values()) or \
                    (field == "tips" and (set(value) != set(keys) or not all(v.strip() for v in value.values()))):
                raise ContentError(f"{field}: {PUT[field]}, all text"
                                   + (", all three filled in" if field == "tips" else ""))
            if field == "look":
                value = {k: value.get(k, "") for k in LOOK}

        def fn(row):
            if field == "hooks" and row.get("chosen") is not None and not force:
                raise ContentError(f"she has picked a hook on {ident}; pass --force to replace them")
            if row.get(field) == value:
                return {}
            cols = {field: value}
            if field == "hooks":
                defaults = {"onscreen": "", "frame": "", "format": "", "payoff": "", "rec": False}
                cols[field] = [{**defaults, **h} for h in value]
                cols["rehook"] = None
                if row.get("chosen") is not None:
                    cols["chosen"] = None
            if field == "prep" and row.get("prep_done"):
                cols["prep_done"] = []
            return cols
        return self.change("ideas", "id", ident, fn, "idea")

    def archive(self, table, key_col, key, what):
        def fn(row):
            return {} if row.get("archived") else {"archived": True}
        for _ in range(4):
            row = self._one(table, key_col, key, what, archived_ok=True)
            cols = fn(row)
            if not cols:
                return row, False
            out = self.api.update(table, key_col, key, row["updated_at"], cols)
            if out is not None:
                return out, True
        raise ContentError(f"{what} {key!r} kept changing; try again")

    def add_idea(self, title, ident=None, status="proposed", fields=None, channels=(),
                 links=(), hers=False, week=None, post_date=None, allow_duplicate=False):
        if not title.strip():
            raise ContentError("an idea needs a title")
        if status not in ("proposed", "backlog", "on deck", "in progress"):
            raise ContentError("a new idea starts as proposed, backlog, on deck or in progress")
        if not allow_duplicate:
            urls = {u.rstrip("/") for u in [(fields or {}).get("brief"), (fields or {}).get("source-url")]
                    + [u for _, u in links] if u}
            for r in self.api.rows("ideas", {"archived": False}):
                theirs = {(u or "").rstrip("/") for u in [r.get("brief"), r.get("source_url")]
                          + [l["url"] for l in r.get("links") or []]} - {""}
                same_title = " ".join(r["title"].casefold().split()) == " ".join(title.casefold().split())
                if same_title or urls & theirs:
                    raise ContentError(f"looks like {r['id']} ({r['title']!r}, {r['status']}) already; "
                                       f"pass --allow-duplicate if it really is a new idea")
        explicit = bool(ident)
        ident = ident or slug(title)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", ident):
            raise ContentError(f"{ident!r}: ids are lowercase letters, numbers and dashes")
        taken = {r["id"] for r in self.api.rows("ideas") if r["id"] == ident or r["id"].startswith(ident + "-")}
        if ident in taken and explicit:
            raise ContentError(f"there's already an idea {ident!r}; pick another --id")
        if ident in taken:
            n = 2
            while f"{ident}-{n}" in taken:
                n += 1
            ident = f"{ident}-{n}"
        row = {"id": ident, "title": title.strip(), "status": status, "origin": "agent",
               "channels": [], "links": [], "drafted": [], "notes": []}
        for field, raw in (fields or {}).items():
            col, dname, kind = FIELDS[field]
            if kind == "story":
                low = raw.strip().lower()
                if low in STORY_NA:
                    row["no_story"] = True
                elif low == "unset":
                    pass
                else:
                    self._one("stories", "key", raw, "story")
                    row["story_key"] = raw
            else:
                row[col] = self.parse_value(field, raw)
            filled = (raw.strip().lower() != "unset") if kind == "story" else row.get(col) not in (None, "")
            if dname and not hers and filled:
                row["drafted"].append(dname)
        if row.get("series") and row["series"] != NO_SERIES and row.get("ep") is None:
            row["ep"] = self._next_ep(row["series"], ident)
        if channels:
            cfg = self.config()
            current = [c for c in cfg["opts"]["channel"] if c not in cfg["retired"]["channel"]]
            for c in channels:
                if c not in current:
                    raise ContentError(f"{c!r} is not a channel; channels: {', '.join(current)}")
            row["channels"] = sorted(dict.fromkeys(channels), key=current.index)
            if not hers:
                row["drafted"].append("channels")
        for label, url in links:
            if _bad_url(url):
                raise ContentError(f"{url!r} doesn't look like a link or a repo path")
            row["links"].append({"label": label, "url": url})
        if row.get("origin") == "client" and "safety" not in (fields or {}):
            row["safety"] = "blinded"      # as the app does for a client source
            if not hers:
                row["drafted"].append("safety")
        if status in ("on deck", "in progress"):
            row["week"] = self.parse_value("week", week) if week else self.this_week().isoformat()
            if status == "on deck" and any(w["week"] == row["week"] and w.get("reviewed_on")
                                           for w in self.api.rows("weeks")):
                row["added_after_lock"] = True
        elif week:
            raise ContentError("--week goes with on deck or in progress")
        if post_date:
            row["post_date"] = _date(post_date).isoformat()
        return self.api.insert("ideas", row)

    # takes and stories
    def add_take(self, short, text, pillar="", on_camera="ready", allow_duplicate=False):
        if not short.strip() or not text.strip():
            raise ContentError("a take needs a short name and its text")
        if pillar:
            pillar = self.check_opt("pillars", pillar)
        rows = self.api.rows("takes")
        norm = lambda x: " ".join(x.casefold().split())
        for r in rows:
            if not allow_duplicate and not r.get("archived") and norm(r["text"]) == norm(text):
                raise ContentError(f"that's take #{r['n']} already; pass --allow-duplicate to add it again")
        n = max([r["n"] for r in rows] or [0]) + 1
        return self.api.insert("takes", {"n": n, "short": short, "text": text,
                                         "pillar": pillar, "on_camera": on_camera})

    def add_story(self, key, title, known="", missing="", pillar="", on_camera="blinded"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", key):
            raise ContentError(f"{key!r}: story keys are lowercase letters, numbers and dashes")
        if self.api.rows("stories", {"key": key}):
            raise ContentError(f"story {key!r} already exists")
        if pillar:
            pillar = self.check_opt("pillars", pillar)
        return self.api.insert("stories", {"key": key, "title": title, "known": known,
                                           "missing": missing, "pillar": pillar,
                                           "on_camera": on_camera})

    def set_bank(self, table, key, cols):
        """Change a take or a story: text fields, pillar(s), on camera."""
        key_col, what = ("n", "take") if table == "takes" else ("key", "story")
        cols = {k: v for k, v in cols.items() if v is not None}
        if not cols:
            raise ContentError("nothing to change")
        for k in ("short", "text", "title"):
            if k in cols and not cols[k].strip():
                raise ContentError(f"the {k} can't be empty")
        if cols.get("pillar"):
            cols["pillar"] = self.check_opt("pillars", cols["pillar"])
        return self.change(table, key_col, key,
                           lambda row: {k: v for k, v in cols.items() if row.get(k) != v}, what)

    # posts and signals
    def add_post(self, ident, channel, posted_on=None, url=None, hook_format=None):
        idea = self.idea(ident)
        cfg = self.config()
        if channel not in cfg["opts"]["channel"]:
            raise ContentError(f"{channel!r} is not a channel; channels: {', '.join(cfg['opts']['channel'])}")
        live = [p for p in self.api.rows("posts", {"idea_id": idea["id"], "archived": False})
                if p["channel"] == channel]
        if live:
            raise ContentError(f"{ident} already has a {channel} post ({live[0]['id'][:8]}); "
                               f"change it with `mh content post set`")
        if url and _bad_url(url):
            raise ContentError(f"{url!r} doesn't look like a link")
        row = {"idea_id": idea["id"], "channel": channel}
        if posted_on:
            row["posted_on"] = _date(posted_on).isoformat()
        if url:
            row["url"] = url
        if hook_format:
            row["hook_format"] = self.check_opt("hookFormat", hook_format)
        return self.api.insert("posts", row)

    def set_post(self, ident, posted_on=None, url=None, hook_format=None):
        post = self.post(ident)
        cols = {}
        if posted_on is not None:
            cols["posted_on"] = _date(posted_on).isoformat() if posted_on else None
        if url is not None:
            if url and _bad_url(url):
                raise ContentError(f"{url!r} doesn't look like a link")
            cols["url"] = url or None
        if hook_format is not None:
            cols["hook_format"] = self.check_opt("hookFormat", hook_format) if hook_format else ""
        if not cols:
            raise ContentError("nothing to change: pass --on, --url or --hook-format")
        return self.change("posts", "id", post["id"],
                           lambda row: {k: v for k, v in cols.items() if row.get(k) != v}, "post")

    def add_signal(self, ident, numbers, recorded_on=None, said=None, notice=None,
                   more=None, any_metric=False):
        post = self.post(ident)
        allowed = METRICS.get(post["channel"])
        out = {}
        for item in numbers:
            if "=" not in item:
                raise ContentError(f"{item!r}: numbers go as name=value")
            k, v = item.split("=", 1)
            k = k.strip()
            if not k:
                raise ContentError(f"{item!r}: the number needs a name")
            if allowed is not None and k not in allowed and not any_metric:
                raise ContentError(f"{k!r} isn't a {post['channel']} number; "
                                   f"use {', '.join(allowed)} (or --any-metric)")
            try:
                num = float(v.replace(",", "").rstrip("%"))
            except ValueError:
                raise ContentError(f"{item!r}: {v!r} is not a number") from None
            if not 0 <= num < 1e15:
                raise ContentError(f"{item!r}: numbers are counts, from 0 up")
            out[k] = int(num) if num.is_integer() else num
        if more is not None and more not in MORE:
            raise ContentError(f"more of this: {', '.join(MORE)}")
        if not out and not said and not notice and more is None:
            raise ContentError("a signal needs numbers, what people said, what you notice, or --more")
        # Her date, not the database's (UTC turns an evening check into tomorrow).
        row = {"post_id": post["id"], "numbers": out,
               "recorded_on": _date(recorded_on).isoformat() if recorded_on else self.today.isoformat()}
        if said:
            row["said"] = said
        if notice:
            row["notice"] = notice
        if more:
            row["more_of_this"] = more
        return self.api.insert("signals", row)


# ---------------------------------------------------------------- the CLI

def add_parser(sub, common):
    content = sub.add_parser(
        "content", parents=[common],
        help="the content-planning app's ideas, takes, stories, posts and signals",
        description="Reads and writes the content-planning app's database. "
                    "Pitch fields an agent sets are marked drafted until MQ confirms them.")
    cs = content.add_subparsers(dest="action", required=True)

    def p(parent, name, fn, help):
        x = parent.add_parser(name, help=help, parents=[common])
        x.set_defaults(content_fn=fn)
        return x

    x = p(cs, "ideas", cmd_ideas, "list ideas (not rejected or archived, unless asked)")
    x.add_argument("--status", action="append", choices=STATUSES)
    x.add_argument("--week", help="a Monday, or 'this'")
    x.add_argument("--channel")
    x.add_argument("--link", help="ideas whose brief, source or links include this path or URL")
    x.add_argument("--grep", help="ideas whose title or angle has all these words")
    x.add_argument("--all", action="store_true", help="include rejected and archived")

    idea = cs.add_parser("idea", help="one idea", parents=[common]).add_subparsers(
        dest="idea_action", required=True)
    x = p(idea, "show", cmd_idea_show, "everything on one idea")
    x.add_argument("id")
    x = p(idea, "add", cmd_idea_add, "add an idea; pitch fields given are marked drafted")
    x.add_argument("title")
    x.add_argument("--id", dest="new_id", help="default: made from the title")
    x.add_argument("--status", default="proposed", choices=["proposed", "backlog", "on deck", "in progress"])
    for f in ["summary", "pillar", "type", "feeling", "style", "on-camera", "voice",
              "harm-help", "cta", "series", "safety", "origin", "brief", "source-url",
              "source-label", "take", "story"]:
        x.add_argument("--" + f, dest="f_" + f.replace("-", "_"))
    x.add_argument("--channel", action="append", default=[])
    x.add_argument("--link", nargs=2, action="append", default=[], metavar=("LABEL", "URL"))
    x.add_argument("--week", help="with on deck; default the making week")
    x.add_argument("--hers", action="store_true", help="these are her words: don't mark drafted")
    x.add_argument("--allow-duplicate", action="store_true",
                   help="add it even though an idea with this title or link exists")
    x = p(idea, "set", cmd_idea_set, "change one field (story: a key, none, or unset)")
    x.add_argument("id")
    x.add_argument("field", choices=list(FIELDS) + ["look." + p for p in LOOK], metavar="field",
                   help=", ".join(list(FIELDS) + ["look.<part>"]))
    x.add_argument("value")
    x.add_argument("--hers", action="store_true", help="her words: clears drafted")
    x.epilog = "story: a story key; none (or n/a) for no story from the bank; unset for not picked."
    x = p(idea, "status", cmd_idea_status, "move an idea, as the app does")
    x.add_argument("id")
    x.add_argument("status", choices=STATUSES)
    x.add_argument("--week")
    x.add_argument("--post-date")
    x.add_argument("--note", help="why (rejected)")
    x.add_argument("--only-from", action="append", choices=STATUSES,
                   help="move it only if it is in this status now (repeatable); otherwise leave it")
    x = p(idea, "channels", cmd_idea_channels, "add or remove channels")
    x.add_argument("id")
    x.add_argument("--add", action="append", default=[])
    x.add_argument("--remove", action="append", default=[])
    x.add_argument("--hers", action="store_true", help="her pick: clears drafted")
    x = p(idea, "speaks-to", cmd_idea_speaks, "which part of the inner world it speaks to")
    x.add_argument("id")
    x.add_argument("--add", action="append", default=[], choices=INNER_WORLD)
    x.add_argument("--remove", action="append", default=[], choices=INNER_WORLD)
    x.add_argument("--hers", action="store_true", help="her pick: clears drafted")
    x = p(idea, "link", cmd_idea_link, "add a link (or relabel one)")
    x.add_argument("id")
    x.add_argument("label")
    x.add_argument("url")
    x = p(idea, "unlink", cmd_idea_unlink, "remove a link")
    x.add_argument("id")
    x.add_argument("url")
    x = p(idea, "note", cmd_idea_note, "add a note")
    x.add_argument("id")
    x.add_argument("text")
    x.add_argument("--who", help="default: --actor")
    x = p(idea, "put", cmd_idea_put, "replace hooks, beats, script, prep, tips or look from JSON")
    x.add_argument("id")
    x.add_argument("field", choices=list(PUT))
    x.add_argument("--file", required=True, help="a JSON file, or - for stdin")
    x.add_argument("--force", action="store_true", help="replace hooks she already picked from")
    x = p(idea, "archive", cmd_idea_archive, "hide an idea (nothing is deleted)")
    x.add_argument("id")

    x = p(cs, "takes", cmd_takes, "list takes")
    x.add_argument("--all", action="store_true")
    take = cs.add_parser("take", help="one take", parents=[common]).add_subparsers(
        dest="take_action", required=True)
    x = p(take, "show", cmd_take_show, "one take")
    x.add_argument("n", type=int)
    x = p(take, "add", cmd_take_add, "add a take (her words)")
    x.add_argument("short")
    x.add_argument("text")
    x.add_argument("--pillar", default="", help="one or more, comma separated")
    x.add_argument("--on-camera", default="ready", help="ready, or a note such as 'care: pair with take 1'")
    x.add_argument("--allow-duplicate", action="store_true")
    x = p(take, "set", cmd_take_set, "change a take: --short, --text, --pillar, --on-camera")
    x.add_argument("n", type=int)
    x.add_argument("--short")
    x.add_argument("--text")
    x.add_argument("--pillar")
    x.add_argument("--on-camera")
    x = p(take, "archive", cmd_take_archive, "hide a take")
    x.add_argument("n", type=int)

    x = p(cs, "stories", cmd_stories, "list stories")
    x.add_argument("--all", action="store_true")
    story = cs.add_parser("story", help="one story", parents=[common]).add_subparsers(
        dest="story_action", required=True)
    x = p(story, "show", cmd_story_show, "one story")
    x.add_argument("key")
    x = p(story, "add", cmd_story_add, "add a story (blinded unless --on-camera says otherwise)")
    x.add_argument("key")
    x.add_argument("title")
    x.add_argument("--known", default="")
    x.add_argument("--missing", default="")
    x.add_argument("--pillar", default="", help="one or more, comma separated")
    x.add_argument("--on-camera", default="blinded", help="blinded, ready, or a note")
    x = p(story, "set", cmd_story_set, "change a story: --title, --known, --missing, --pillar, --on-camera")
    x.add_argument("key")
    x.add_argument("--title")
    x.add_argument("--known")
    x.add_argument("--missing")
    x.add_argument("--pillar")
    x.add_argument("--on-camera")
    x = p(story, "archive", cmd_story_archive, "hide a story")
    x.add_argument("key")

    x = p(cs, "config", cmd_config, "the latest config version")

    x = p(cs, "posts", cmd_posts, "list posts")
    x.add_argument("id", nargs="?", help="only this idea's")
    post = cs.add_parser("post", help="one post", parents=[common]).add_subparsers(
        dest="post_action", required=True)
    x = p(post, "add", cmd_post_add, "record an idea going out on a channel")
    x.add_argument("id")
    x.add_argument("channel")
    x.add_argument("--on", help="the day it posted")
    x.add_argument("--url")
    x.add_argument("--hook-format")
    x = p(post, "set", cmd_post_set, "change a post's day, link or hook format ('' clears)")
    x.add_argument("post")
    x.add_argument("--on")
    x.add_argument("--url")
    x.add_argument("--hook-format")
    x = p(post, "archive", cmd_post_archive, "hide a post")
    x.add_argument("post")

    x = p(cs, "signals", cmd_signals, "a post's signals, newest first")
    x.add_argument("post")
    signal = cs.add_parser("signal", help="one signal", parents=[common]).add_subparsers(
        dest="signal_action", required=True)
    x = p(signal, "add", cmd_signal_add, "record a check on a post")
    x.add_argument("post")
    x.add_argument("--n", action="append", default=[], metavar="NAME=VALUE")
    x.add_argument("--on", help="the day you checked (default today)")
    x.add_argument("--said", help="what people said")
    x.add_argument("--notice", help="what you notice")
    x.add_argument("--more", choices=MORE, help="more of this?")
    x.add_argument("--any-metric", action="store_true")
    return content


def run(args, content=None, out=None):
    """Entry from mhcli.main: no task store needed."""
    out = out or sys.stdout
    try:
        if content is None:
            url, key = load_credentials()
            content = Content(Api(url, key, args.actor), args.actor)
        return args.content_fn(content, args, out) or 0
    except ContentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, UnicodeError) as exc:
        # http.client puts a bad header's value in its message; that could be the key.
        print(f"error: {type(exc).__name__} building the request (details withheld)", file=sys.stderr)
        return 1


def _emit(out, args, data, text):
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str), file=out)
    else:
        print(text, file=out)


def _done(out, args, row, changed, what):
    _emit(out, args, row, f"{what}." if changed else f"No change: {what.split(':')[0]} was already like that.")


def _idea_line(r):
    wk = r.get("week") or ""
    flag = " (archived)" if r.get("archived") else ""
    return f"{r['id']:<40} {r['status']:<11} {wk:<10} {r['title']}{flag}"


def cmd_ideas(c, args, out):
    statuses = args.status or (None if args.all else [s for s in STATUSES if s != "rejected"])
    week = c.this_week().isoformat() if args.week == "this" else args.week
    rows = c.ideas(statuses, week, args.channel, include_archived=args.all,
                   link=args.link, words=args.grep)
    _emit(out, args, rows, "\n".join(_idea_line(r) for r in rows) or "No ideas match.")


def cmd_idea_show(c, args, out):
    r = c.idea(args.id, archived_ok=True)
    d = set(r.get("drafted") or [])
    def tag(dname, val):
        return f"{val}{' (drafted)' if dname in d and val else ''}"
    lines = [f"{r['title']}  [{r['id']}]", f"status: {r['status']}"
             + (f", week of {r['week']}" if r.get("week") else "")
             + (f", posts {r['post_date']}" if r.get("post_date") else "")
             + (", archived" if r.get("archived") else "")]
    if r.get("summary"):
        lines.append(f"angle: {tag('summary', r['summary'])}")
    for f in ["pillar", "type", "feeling", "style", "on-camera", "voice", "harm-help", "cta", "series", "safety"]:
        col, dname, _ = FIELDS[f]
        if r.get(col):
            lines.append(f"{f}: {tag(dname, r[col])}" + (f" ep {r['ep']}" if f == "series" and r.get("ep") else ""))
    lines.append("take: " + (tag("take", f"#{r['take_n']}") if r.get("take_n") else "not picked"))
    lines.append("story: " + (tag("story", r["story_key"]) if r.get("story_key")
                              else "n/a (no story from the bank)" if r.get("no_story") else "not picked"))
    if r.get("channels"):
        lines.append("channels: " + tag("channels", ", ".join(r["channels"])))
    if r.get("inner_world"):
        lines.append("speaks to: " + ", ".join(r["inner_world"]))
    if r.get("brief"):
        lines.append(f"brief: {r['brief']}")
    if r.get("source_url"):
        lines.append(f"source: {r.get('source_label') or ''} {r['source_url']}".replace("  ", " "))
    for l in r.get("links") or []:
        lines.append(f"link: {l['label']}: {l['url']}")
    if r.get("hooks"):
        lines.append(f"hooks: {len(r['hooks'])}" + (f", she picked {r['chosen']}" if r.get("chosen") is not None else ", none picked"))
    if r.get("rehook"):
        lines.append(f"re-hook needed: {r['rehook']['field']} changed")
    if r.get("beats"):
        lines.append(f"beats: {len(r['beats'])}")
    if r.get("script"):
        lines.append(f"script: {len(r['script'])} lines")
    for n in r.get("notes") or []:
        lines.append(f"note ({n.get('who', '')}): {n.get('text', '')}")
    if r.get("reject_note") and r["status"] == "rejected":
        lines.append(f"rejected because: {r['reject_note']}")
    lines.append(f"updated {r['updated_at']} by {r.get('updated_by') or '?'}")
    _emit(out, args, r, "\n".join(lines))


def cmd_idea_add(c, args, out):
    fields = {}
    for f in ["summary", "pillar", "type", "feeling", "style", "on-camera", "voice",
              "harm-help", "cta", "series", "safety", "origin", "brief", "source-url",
              "source-label", "take", "story"]:
        v = getattr(args, "f_" + f.replace("-", "_"))
        if v is not None:
            fields[f] = v
    row = c.add_idea(args.title, args.new_id, args.status, fields, args.channel,
                     args.link, args.hers, args.week, allow_duplicate=args.allow_duplicate)
    _emit(out, args, row, f"Added {row['id']} ({row['status']}).")


def cmd_idea_set(c, args, out):
    row, changed = c.set_field(args.id, args.field, args.value, args.hers)
    extra = ". Re-hook needed" if changed and row.get("rehook") and args.field in ("feeling", "take", "summary", "style", "on-camera") else ""
    _done(out, args, row, changed, f"{args.id}: {args.field} set{extra}")


def cmd_idea_status(c, args, out):
    try:
        row, changed = c.set_status(args.id, args.status, args.week, args.post_date, args.note,
                                    args.only_from)
    except SkipMove as skip:
        # Exit 3, and left_alone in the JSON, so a script can tell this from a move.
        _emit(out, args, {**skip.row, "left_alone": True},
              f"Left alone: {args.id} is {skip.row['status']}.")
        return 3
    wk = f", week of {row['week']}" if row.get("week") else ""
    _done(out, args, row, changed, f"{args.id}: {row['status']}{wk}")


def cmd_idea_channels(c, args, out):
    row, changed = c.channels(args.id, args.add, args.remove, args.hers)
    _done(out, args, row, changed, f"{args.id}: channels {', '.join(row['channels']) or 'none'}")


def cmd_idea_speaks(c, args, out):
    row, changed = c.speaks_to(args.id, args.add, args.remove, args.hers)
    _done(out, args, row, changed, f"{args.id}: speaks to {', '.join(row['inner_world']) or 'nothing set'}")


def cmd_idea_link(c, args, out):
    row, changed = c.link(args.id, args.label, args.url)
    _done(out, args, row, changed, f"{args.id}: link {args.label}")


def cmd_idea_unlink(c, args, out):
    row, changed = c.unlink(args.id, args.url)
    _done(out, args, row, changed, f"{args.id}: link removed")


def cmd_idea_note(c, args, out):
    row, changed = c.note(args.id, args.text, args.who)
    _done(out, args, row, changed, f"{args.id}: note added")


def cmd_idea_put(c, args, out):
    try:
        text = sys.stdin.buffer.read().decode() if args.file == "-" else Path(args.file).read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise ContentError(f"can't read {args.file}: {exc.__class__.__name__}") from None
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ContentError(f"{args.file}: not JSON ({exc})") from None
    row, changed = c.put(args.id, args.field, value, args.force)
    _done(out, args, row, changed, f"{args.id}: {args.field} replaced")


def cmd_idea_archive(c, args, out):
    row, changed = c.archive("ideas", "id", args.id, "idea")
    _done(out, args, row, changed, f"{args.id}: archived")


def cmd_takes(c, args, out):
    rows = c.api.rows("takes", None if args.all else {"archived": False}, order="n.asc")
    _emit(out, args, rows, "\n".join(f"#{r['n']:<3} {r['short']}: {r['text']}"
                                     + (" (archived)" if r.get("archived") else "") for r in rows) or "No takes.")


def cmd_take_show(c, args, out):
    r = c._one("takes", "n", args.n, "take", archived_ok=True)
    _emit(out, args, r, f"#{r['n']} {r['short']}: {r['text']}\npillar: {r['pillar'] or 'not set'}; "
                        f"on camera: {r['on_camera']}" + ("; archived" if r.get("archived") else ""))


def cmd_take_set(c, args, out):
    row, changed = c.set_bank("takes", args.n, {"short": args.short, "text": args.text,
                                                "pillar": args.pillar, "on_camera": args.on_camera})
    _done(out, args, row, changed, f"take #{args.n}: updated")


def cmd_take_archive(c, args, out):
    row, changed = c.archive("takes", "n", args.n, "take")
    _done(out, args, row, changed, f"take #{args.n}: archived")


def cmd_story_set(c, args, out):
    row, changed = c.set_bank("stories", args.key, {"title": args.title, "known": args.known,
                                                    "missing": args.missing, "pillar": args.pillar,
                                                    "on_camera": args.on_camera})
    _done(out, args, row, changed, f"story {args.key}: updated")


def cmd_story_archive(c, args, out):
    row, changed = c.archive("stories", "key", args.key, "story")
    _done(out, args, row, changed, f"story {args.key}: archived")


def cmd_take_add(c, args, out):
    row = c.add_take(args.short, args.text, args.pillar, args.on_camera, args.allow_duplicate)
    _emit(out, args, row, f"Added take #{row['n']}.")


def cmd_stories(c, args, out):
    rows = c.api.rows("stories", None if args.all else {"archived": False}, order="key.asc")
    _emit(out, args, rows, "\n".join(f"{r['key']:<24} {r['on_camera']:<8} {r['title']}"
                                     + (" (archived)" if r.get("archived") else "") for r in rows) or "No stories.")


def cmd_story_show(c, args, out):
    r = c._one("stories", "key", args.key, "story", archived_ok=True)
    text = "\n".join(x for x in [f"{r['title']}  [{r['key']}], {r['on_camera']}",
                                 r.get("pillar") and f"pillar: {r['pillar']}",
                                 r.get("known") and f"known: {r['known']}",
                                 r.get("missing") and f"missing: {r['missing']}"] if x)
    _emit(out, args, r, text)


def cmd_story_add(c, args, out):
    row = c.add_story(args.key, args.title, args.known, args.missing, args.pillar,
                      args.on_camera)
    _emit(out, args, row, f"Added story {row['key']} ({row['on_camera']}).")


def cmd_config(c, args, out):
    cfg = c.config()
    lines = [f"Config version {cfg['n']}: {cfg['label']}"]
    for k, v in sorted(cfg.get("opts", {}).items()):
        retired = set(cfg.get("retired", {}).get(k, []))
        lines.append(f"{k}: " + ", ".join(x for x in v if x not in retired))
    lines.append("pillars: " + ", ".join(p["name"] for p in cfg.get("pillars", [])))
    iw = (cfg.get("config") or {}).get("innerWorld") or {}
    for k in INNER_WORLD:
        if iw.get(k):
            lines.append(f"inner world, {k}: {iw[k]}")
    _emit(out, args, cfg, "\n".join(lines))


def _post_line(p):
    return f"{p['id'][:8]}  {p['idea_id']:<36} {p['channel']:<20} {p.get('posted_on') or 'not posted':<10} {p.get('url') or ''}"


def cmd_posts(c, args, out):
    where = {"archived": False}
    if args.id:
        where["idea_id"] = c.idea(args.id, archived_ok=True)["id"]
    rows = c.api.rows("posts", where, order="posted_on.desc.nullslast")
    _emit(out, args, rows, "\n".join(_post_line(p) for p in rows) or "No posts.")


def cmd_post_add(c, args, out):
    row = c.add_post(args.id, args.channel, args.on, args.url, args.hook_format)
    _emit(out, args, row, f"Added post {row['id'][:8]}: {args.id} on {args.channel}.")


def cmd_post_set(c, args, out):
    row, changed = c.set_post(args.post, args.on, args.url, args.hook_format)
    _done(out, args, row, changed, f"post {row['id'][:8]}: updated")


def cmd_post_archive(c, args, out):
    post = c.post(args.post)
    row, changed = c.archive("posts", "id", post["id"], "post")
    _done(out, args, row, changed, f"post {row['id'][:8]}: archived")


def cmd_signals(c, args, out):
    post = c.post(args.post)
    rows = c.api.rows("signals", {"post_id": post["id"], "archived": False},
                      order="recorded_on.desc,created_at.desc")
    def line(s):
        nums = ", ".join(f"{k} {v}" for k, v in (s.get("numbers") or {}).items())
        bits = [s["recorded_on"], nums, s.get("said") and f"said: {s['said']}",
                s.get("notice") and f"notice: {s['notice']}",
                s.get("more_of_this") and f"more of this: {s['more_of_this']}"]
        return "  ".join(b for b in bits if b)
    _emit(out, args, rows, "\n".join(line(s) for s in rows) or "No signals yet.")


def cmd_signal_add(c, args, out):
    row = c.add_signal(args.post, args.n, args.on, args.said, args.notice, args.more,
                       args.any_metric)
    _emit(out, args, row, f"Recorded a signal for post {row['post_id'][:8]}.")
