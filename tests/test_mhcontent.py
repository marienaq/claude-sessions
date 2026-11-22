#!/usr/bin/env python3
"""
Tests for `mh content`, against an in-memory stand-in for the app's database.
The real PostgREST path is exercised separately (see mhcontent's docstring);
these pin the rules: guarded writes, drafted marks, re-hook, option checks,
status moves, posts and signals.
"""

import copy
import io
import json
import sys
import unittest
import uuid
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mhcli
import mhcontent
from mhcontent import Content, ContentError, making_week

KEYS = {"ideas": "id", "takes": "n", "stories": "key", "posts": "id",
        "signals": "id", "config_versions": "n", "weeks": "week"}


class FakeApi:
    """Same methods as mhcontent.Api. `between` runs once between a read and
    the next guarded write, to stand in for MQ editing in the app."""

    def __init__(self):
        self.t = {k: [] for k in KEYS}
        self.clock = 0
        self.between = None
        self.writes = 0

    def stamp(self):
        self.clock += 1
        return f"2026-11-22T00:00:{self.clock:02d}.000001+00:00"

    def seed(self, table, **row):
        row.setdefault("archived", False)
        row["updated_at"] = self.stamp()
        self.t[table].append(row)
        return row

    def rows(self, table, where=None, order=None, limit=None):
        out = []
        for r in self.t[table]:
            ok = True
            for col, val in (where or {}).items():
                have = r.get(col)
                if isinstance(val, (list, tuple)):
                    ok = ok and have in val
                else:
                    ok = ok and have == val
            if ok:
                out.append(copy.deepcopy(r))
        if order:
            col, _, rest = order.partition(".")
            out.sort(key=lambda r: (r.get(col) is None, r.get(col) or 0),
                     reverse=rest.startswith("desc"))
        return out[:limit] if limit else out

    def insert(self, table, row):
        row = copy.deepcopy(row)
        if table in ("posts", "signals"):
            row["id"] = str(uuid.uuid4())
        if table == "signals":
            row.setdefault("recorded_on", "2026-11-22")
        key = KEYS[table]
        if any(r[key] == row[key] for r in self.t[table]):
            raise ContentError("duplicate key [23505]")
        row.setdefault("archived", False)
        row["updated_at"] = self.stamp()
        self.t[table].append(row)
        self.writes += 1
        return copy.deepcopy(row)

    def update(self, table, key_col, key, stamp, cols):
        if self.between:
            fn, self.between = self.between, None
            fn(self)
        for r in self.t[table]:
            if r[key_col] == key:
                if r["updated_at"] != stamp:
                    return None
                r.update(copy.deepcopy(cols))
                r["updated_at"] = self.stamp()
                self.writes += 1
                return copy.deepcopy(r)
        return None


def config_snapshot():
    return {
        "opts": {"feeling": ["cheeky", "seen", "curious"], "style": ["listicle", "hot take"],
                 "type": ["authority", "storyline"], "onCamera": ["talking head", "skit"],
                 "cta": ["none", "follow"], "series": ["AI-ITA", "Workplace advice"],
                 "safety": ["blinded", "clear"], "hookFormat": ["question", "stakes"],
                 "harmHelp": ["harm", "help", "both"], "voice": ["Mariena"],
                 "channel": ["TikTok", "LinkedIn", "YouTube", "Mellonmail"]},
        "retired": {"feeling": ["curious"], "channel": []},
        "pillars": [{"name": "Messy"}, {"name": "Value"}],
        "config": {"filmWeekday": 3},
    }


def idea_row(ident, **kw):
    row = {"id": ident, "title": ident.title(), "status": "proposed", "week": None,
           "post_date": None, "summary": "", "pillar": "", "type": "", "style": "",
           "on_camera": "", "voice": "", "feeling": "", "harm_help": "", "cta": "",
           "take_n": None, "story_key": None, "no_story": False, "series": "", "ep": None,
           "channels": [], "links": [], "inner_world": [], "hooks": [], "chosen": None,
           "rehook": None, "beats": [], "notes": [], "drafted": [], "script": [], "prep": [],
           "prep_done": [], "look": {k: "" for k in mhcontent.LOOK}, "brief": "",
           "source_url": None, "source_label": None, "reject_note": None,
           "added_after_lock": False}
    row.update(kw)
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.api.seed("config_versions", n=12, label="v12", snapshot=config_snapshot())
        self.api.seed("takes", n=3, short="tools", text="Tools don't fix process")
        self.api.seed("stories", key="grant", title="The grant email")
        self.api.seed("ideas", **idea_row("dyslexia"))
        self.api.seed("ideas", **idea_row("hooked", hooks=[{"spoken": "Hi", "rec": True}],
                                          feeling="seen", drafted=["feeling"]))
        # Thursday 11/19: the making week is Monday 11/16.
        self.c = Content(self.api, "orca", today=date(2026, 11, 19))

    def get(self, ident):
        return self.api.rows("ideas", {"id": ident})[0]


class SetFieldTests(Base):
    def test_agent_value_is_drafted_and_hers_clears_it(self):
        self.c.set_field("dyslexia", "pillar", "Messy")
        self.assertIn("pillar", self.get("dyslexia")["drafted"])
        self.c.set_field("dyslexia", "pillar", "Messy", hers=True)
        self.assertNotIn("pillar", self.get("dyslexia")["drafted"])

    def test_options_come_from_config(self):
        with self.assertRaisesRegex(ContentError, "not a feeling option"):
            self.c.set_field("dyslexia", "feeling", "sad")
        with self.assertRaisesRegex(ContentError, "retired"):
            self.c.set_field("dyslexia", "feeling", "curious")
        with self.assertRaisesRegex(ContentError, "not a pillar"):
            self.c.set_field("dyslexia", "pillar", "Fluff")
        with self.assertRaisesRegex(ContentError, "unknown field"):
            self.c.set_field("dyslexia", "gut", "yes")

    def test_rehook_when_a_hooked_idea_changes_feeling(self):
        self.c.set_field("hooked", "feeling", "cheeky")
        r = self.get("hooked")
        self.assertEqual(r["rehook"], {"field": "feeling", "from": "seen", "to": "cheeky"})
        self.c.set_field("hooked", "pillar", "Value")   # not a re-hook field
        self.assertEqual(self.get("hooked")["rehook"]["field"], "feeling")

    def test_no_rehook_without_hooks_or_change(self):
        self.c.set_field("dyslexia", "feeling", "cheeky")
        self.assertIsNone(self.get("dyslexia")["rehook"])
        row, changed = self.c.set_field("hooked", "feeling", "seen", hers=True)
        self.assertIsNone(row["rehook"])
        self.assertNotIn("feeling", row["drafted"])     # confirmed

    def test_story_na_and_story_are_written_together(self):
        self.api.seed("ideas", **idea_row("withstory", story_key="grant"))
        self.c.set_field("withstory", "story", "n/a")
        r = self.get("withstory")
        self.assertEqual((r["story_key"], r["no_story"]), (None, True))
        self.assertIn("story", r["drafted"])
        self.c.set_field("withstory", "story", "grant", hers=True)
        r = self.get("withstory")
        self.assertEqual((r["story_key"], r["no_story"]), ("grant", False))
        self.assertNotIn("story", r["drafted"])
        with self.assertRaisesRegex(ContentError, "no story 'nope'"):
            self.c.set_field("withstory", "story", "nope")

    def test_take_must_exist_and_rehooks(self):
        with self.assertRaisesRegex(ContentError, "no take 9"):
            self.c.set_field("dyslexia", "take", "9")
        self.c.set_field("hooked", "take", "3")
        self.assertEqual(self.get("hooked")["rehook"], {"field": "take", "from": "", "to": "#3"})

    def test_series_numbers_the_episode(self):
        self.api.seed("ideas", **idea_row("ep1", series="AI-ITA", ep=4))
        self.c.set_field("dyslexia", "series", "AI-ITA")
        self.assertEqual(self.get("dyslexia")["ep"], 5)
        self.c.set_field("dyslexia", "series", "none")
        self.assertEqual((self.get("dyslexia")["series"], self.get("dyslexia")["ep"]), ("none", None))

    def test_look_part_merges(self):
        self.c.set_field("dyslexia", "look.outfit", "green sweater")
        self.c.set_field("dyslexia", "look.setup", "desk")
        look = self.get("dyslexia")["look"]
        self.assertEqual((look["outfit"], look["setup"]), ("green sweater", "desk"))

    def test_week_must_be_a_monday_and_dates_checked(self):
        with self.assertRaisesRegex(ContentError, "Monday"):
            self.c.set_field("dyslexia", "week", "2026-11-18")
        with self.assertRaisesRegex(ContentError, "not a date"):
            self.c.set_field("dyslexia", "post-date", "Dec 2")

    def test_no_change_writes_nothing(self):
        self.c.set_field("dyslexia", "summary", "x", hers=True)
        before = self.api.writes
        _, changed = self.c.set_field("dyslexia", "summary", "x", hers=True)
        self.assertFalse(changed)
        self.assertEqual(self.api.writes, before)


class GuardTests(Base):
    def test_a_clash_rereads_and_keeps_her_edit(self):
        def her_edit(api):
            r = next(r for r in api.t["ideas"] if r["id"] == "dyslexia")
            r["notes"] = r["notes"] + [{"who": "You", "text": "hers"}]
            r["title"] = "Her title"
            r["updated_at"] = api.stamp()
        self.api.between = her_edit
        self.c.note("dyslexia", "from orca")
        r = self.get("dyslexia")
        self.assertEqual([n["text"] for n in r["notes"]], ["hers", "from orca"])
        self.assertEqual(r["title"], "Her title")
        self.assertEqual(r["notes"][1]["who"], "orca")

    def test_gives_up_if_it_never_settles(self):
        api = self.api
        def always(*a, **k):
            return None
        api.update = always
        with self.assertRaisesRegex(ContentError, "kept changing"):
            self.c.set_field("dyslexia", "summary", "x")

    def test_archived_rows_refuse_writes(self):
        self.c.archive("ideas", "id", "dyslexia", "idea")
        with self.assertRaisesRegex(ContentError, "archived"):
            self.c.note("dyslexia", "hi")
        _, changed = self.c.archive("ideas", "id", "dyslexia", "idea")
        self.assertFalse(changed)


class StatusTests(Base):
    def test_on_deck_takes_the_making_week(self):
        self.c.set_status("dyslexia", "on deck")
        self.assertEqual(self.get("dyslexia")["week"], "2026-11-16")

    def test_backlog_clears_week_and_lock_flag(self):
        self.c.set_status("dyslexia", "on deck")
        self.c.set_status("dyslexia", "backlog")
        r = self.get("dyslexia")
        self.assertEqual((r["week"], r["added_after_lock"]), (None, False))

    def test_on_deck_in_a_reviewed_week_is_added_after_lock(self):
        self.api.seed("weeks", week="2026-11-16", reviewed_on="2026-11-17")
        self.c.set_status("dyslexia", "on deck")
        self.assertTrue(self.get("dyslexia")["added_after_lock"])

    def test_scheduled_needs_a_post_date(self):
        with self.assertRaisesRegex(ContentError, "post-date"):
            self.c.set_status("dyslexia", "scheduled")
        self.c.set_status("dyslexia", "scheduled", post_date="2026-12-02")
        self.assertEqual(self.get("dyslexia")["post_date"], "2026-12-02")

    def test_reject_keeps_the_note(self):
        self.c.set_status("dyslexia", "rejected", note="too close to last week")
        self.assertEqual(self.get("dyslexia")["reject_note"], "too close to last week")


class ListTests(Base):
    def test_channels_checked_ordered_and_drafted(self):
        self.c.channels("dyslexia", add=["YouTube", "TikTok"])
        r = self.get("dyslexia")
        self.assertEqual(r["channels"], ["TikTok", "YouTube"])
        self.assertIn("channels", r["drafted"])
        self.c.channels("dyslexia", remove=["YouTube"], hers=True)
        r = self.get("dyslexia")
        self.assertEqual(r["channels"], ["TikTok"])
        self.assertNotIn("channels", r["drafted"])
        with self.assertRaisesRegex(ContentError, "not a channel"):
            self.c.channels("dyslexia", add=["Myspace"])

    def test_speaks_to(self):
        self.c.speaks_to("dyslexia", add=["wants", "hurting"])
        self.assertEqual(self.get("dyslexia")["inner_world"], ["hurting", "wants"])
        with self.assertRaises(ContentError):
            self.c.speaks_to("dyslexia", add=["vibes"])

    def test_links(self):
        self.api.seed("ideas", **idea_row("briefed", brief="a/brief.md"))
        self.c.link("briefed", "Storyboard", "marketing/tiktok/sb.md")
        self.c.link("briefed", "Story board", "marketing/tiktok/sb.md")   # relabel
        self.assertEqual(self.get("briefed")["links"], [{"label": "Story board", "url": "marketing/tiktok/sb.md"}])
        with self.assertRaisesRegex(ContentError, "already the brief"):
            self.c.link("briefed", "Brief", "a/brief.md")
        with self.assertRaisesRegex(ContentError, "doesn't look like"):
            self.c.link("briefed", "Bad", "javascript:alert(1)")
        self.c.unlink("briefed", "marketing/tiktok/sb.md")
        self.assertEqual(self.get("briefed")["links"], [])


class PutTests(Base):
    def test_hooks_fill_defaults_and_clear_rehook(self):
        self.api.seed("ideas", **idea_row("rh", rehook={"field": "feeling", "from": "a", "to": "b"}))
        self.c.put("rh", "hooks", [{"spoken": "One"}, {"spoken": "Two", "rec": True}])
        r = self.get("rh")
        self.assertEqual(r["hooks"][0]["onscreen"], "")
        self.assertTrue(r["hooks"][1]["rec"])
        self.assertIsNone(r["rehook"])

    def test_hooks_she_picked_from_need_force(self):
        self.api.seed("ideas", **idea_row("picked", hooks=[{"spoken": "A"}], chosen="0"))
        with self.assertRaisesRegex(ContentError, "--force"):
            self.c.put("picked", "hooks", [{"spoken": "B"}])
        self.c.put("picked", "hooks", [{"spoken": "B"}], force=True)
        self.assertIsNone(self.get("picked")["chosen"])

    def test_shapes_are_checked(self):
        with self.assertRaisesRegex(ContentError, "missing 't'"):
            self.c.put("dyslexia", "beats", [{"gap": "x"}])
        with self.assertRaisesRegex(ContentError, "'d' must be true or false"):
            self.c.put("dyslexia", "script", [{"t": "line", "d": "yes"}])
        with self.assertRaisesRegex(ContentError, "unknown keys"):
            self.c.put("dyslexia", "hooks", [{"spoken": "x", "why": "y"}])
        with self.assertRaises(ContentError):
            self.c.put("dyslexia", "tips", {"Dawn": "x", "Bob": "y"})

    def test_new_prep_clears_ticks(self):
        self.api.seed("ideas", **idea_row("prepped", prep=["a"], prep_done=[0]))
        self.c.put("prepped", "prep", ["a", "b"])
        self.assertEqual(self.get("prepped")["prep_done"], [])


class AddTests(Base):
    def test_add_idea_drafts_what_the_agent_filled(self):
        row = self.c.add_idea("Chatbot told me to quit", fields={"pillar": "Messy", "story": "n/a",
                                                                  "series": "AI-ITA"},
                              channels=["YouTube", "TikTok"], links=[("Card", "cards/x.md")])
        self.assertEqual(row["id"], "chatbot-told-me-to-quit")
        self.assertEqual(sorted(row["drafted"]), ["channels", "pillar", "series", "story"])
        self.assertTrue(row["no_story"])
        self.assertEqual(row["channels"], ["TikTok", "YouTube"])
        self.assertEqual(row["ep"], 1)
        self.assertEqual(row["origin"], "agent")

    def test_ids_never_collide(self):
        with self.assertRaisesRegex(ContentError, "looks like dyslexia"):
            self.c.add_idea("Dyslexia")
        a = self.c.add_idea("Dyslexia", allow_duplicate=True)
        b = self.c.add_idea("Dyslexia", allow_duplicate=True)
        self.assertEqual((a["id"], b["id"]), ("dyslexia-2", "dyslexia-3"))

    def test_on_deck_gets_a_week_and_others_refuse_one(self):
        row = self.c.add_idea("New one", status="on deck")
        self.assertEqual(row["week"], "2026-11-16")
        with self.assertRaisesRegex(ContentError, "--week goes with on deck"):
            self.c.add_idea("Other", week="2026-11-23")
        with self.assertRaisesRegex(ContentError, "starts as"):
            self.c.add_idea("Done", status="published")

    def test_hers_drafts_nothing(self):
        row = self.c.add_idea("Mine", fields={"feeling": "cheeky"}, hers=True)
        self.assertEqual(row["drafted"], [])

    def test_take_and_story(self):
        self.assertEqual(self.c.add_take("short", "text")["n"], 4)
        s = self.c.add_story("new-story", "A story")
        self.assertEqual(s["on_camera"], "blinded")
        with self.assertRaisesRegex(ContentError, "already exists"):
            self.c.add_story("grant", "Again")


class PostSignalTests(Base):
    def test_one_live_post_per_channel(self):
        p = self.c.add_post("dyslexia", "TikTok", posted_on="2026-12-02", hook_format="question")
        with self.assertRaisesRegex(ContentError, "already has a TikTok post"):
            self.c.add_post("dyslexia", "TikTok")
        with self.assertRaisesRegex(ContentError, "not a channel"):
            self.c.add_post("dyslexia", "Vine")
        with self.assertRaisesRegex(ContentError, "not a hookFormat"):
            self.c.add_post("dyslexia", "YouTube", hook_format="vibes")
        self.c.archive("posts", "id", p["id"], "post")
        self.c.add_post("dyslexia", "TikTok")      # allowed once the old one is archived

    def test_signal_numbers_follow_the_channel(self):
        p = self.c.add_post("dyslexia", "TikTok")
        s = self.c.add_signal(p["id"][:8], ["views=1,200", "avg_watch_seconds=7.5"], more="yes")
        self.assertEqual(s["numbers"], {"views": 1200, "avg_watch_seconds": 7.5})
        with self.assertRaisesRegex(ContentError, "isn't a TikTok number"):
            self.c.add_signal(p["id"], ["impressions=3"])
        self.c.add_signal(p["id"], ["impressions=3"], any_metric=True)
        with self.assertRaisesRegex(ContentError, "not a number"):
            self.c.add_signal(p["id"], ["views=lots"])
        with self.assertRaisesRegex(ContentError, "needs numbers"):
            self.c.add_signal(p["id"], [])

    def test_post_prefix_must_be_unique(self):
        p = self.c.add_post("dyslexia", "TikTok")
        q = self.c.add_post("dyslexia", "YouTube")
        self.api.t["posts"][1]["id"] = p["id"][:6] + q["id"][6:]
        with self.assertRaisesRegex(ContentError, "matches 2"):
            self.c.post(p["id"][:6])
        with self.assertRaisesRegex(ContentError, "not a post id"):
            self.c.post("zz")

    def test_set_post(self):
        p = self.c.add_post("dyslexia", "TikTok")
        row, _ = self.c.set_post(p["id"], url="https://tiktok.com/@x/1", posted_on="2026-12-03")
        self.assertEqual((row["url"], row["posted_on"]), ("https://tiktok.com/@x/1", "2026-12-03"))
        row, _ = self.c.set_post(p["id"], url="")
        self.assertIsNone(row["url"])


class ReviewFixTests(Base):
    def test_resetting_a_confirmed_value_keeps_it_confirmed(self):
        self.c.set_field("dyslexia", "take", "3", hers=True)
        before = self.api.writes
        _, changed = self.c.set_field("dyslexia", "take", "3")
        self.assertFalse(changed)
        self.assertNotIn("take", self.get("dyslexia")["drafted"])
        self.assertEqual(self.api.writes, before)

    def test_explicit_id_collision_refuses(self):
        with self.assertRaisesRegex(ContentError, "already an idea"):
            self.c.add_idea("Anything", ident="dyslexia")

    def test_client_origin_is_blinded(self):
        row = self.c.add_idea("A client thing", fields={"origin": "client"})
        self.assertEqual(row["safety"], "blinded")
        row = self.c.add_idea("Cleared", fields={"origin": "client", "safety": "clear"})
        self.assertEqual(row["safety"], "clear")

    def test_new_on_deck_idea_in_reviewed_week_is_flagged(self):
        self.api.seed("weeks", week="2026-11-16", reviewed_on="2026-11-17")
        self.assertTrue(self.c.add_idea("Late", status="on deck")["added_after_lock"])

    def test_story_words(self):
        self.api.seed("ideas", **idea_row("s", story_key="grant"))
        self.c.set_field("s", "story", "none")
        self.assertEqual((self.get("s")["story_key"], self.get("s")["no_story"]), (None, True))
        self.c.set_field("s", "story", "unset")
        self.assertEqual((self.get("s")["no_story"], "story" in self.get("s")["drafted"]), (False, False))
        self.assertTrue(self.c.add_idea("No story", fields={"story": "none"})["no_story"])

    def test_archived_ideas_dont_count_for_episodes(self):
        self.api.seed("ideas", **idea_row("old", series="AI-ITA", ep=9, archived=True))
        self.c.set_field("dyslexia", "series", "AI-ITA")
        self.assertEqual(self.get("dyslexia")["ep"], 1)

    def test_tips_need_all_three(self):
        with self.assertRaisesRegex(ContentError, "all three"):
            self.c.put("dyslexia", "tips", {"Dawn": "x"})
        self.c.put("dyslexia", "tips", {"Dawn": "x", "Alex": "y", "Paloma": "z"})

    def test_negative_numbers_refused(self):
        p = self.c.add_post("dyslexia", "TikTok")
        with self.assertRaisesRegex(ContentError, "from 0 up"):
            self.c.add_signal(p["id"], ["views=-3"])


class CredentialTests(unittest.TestCase):
    def test_url_and_key_never_mixed(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("SUPABASE_URL=https://real.supabase.co\nSUPABASE_SECRET_KEY=filekey\n")
        self.assertEqual(mhcontent.load_credentials(env={}, path=f.name),
                         ("https://real.supabase.co", "filekey"))
        # Half a setting never falls back to the file (that would be the live database).
        for half in ({"SUPABASE_URL": "http://127.0.0.1:54321"}, {"SUPABASE_SECRET_KEY": "k"}):
            with self.assertRaisesRegex(ContentError, "only one of"):
                mhcontent.load_credentials(env=half, path=f.name)
        with self.assertRaisesRegex(ContentError, "https"):
            mhcontent.load_credentials(env={"SUPABASE_URL": "http://evil.example", "SUPABASE_SECRET_KEY": "k"})
        with self.assertRaises(ContentError) as cm:
            mhcontent.load_credentials(env={"SUPABASE_URL": "https://x.co", "SUPABASE_SECRET_KEY": "sb_secret_X\r\nInjected: 1"})
        self.assertNotIn("sb_secret_X", str(cm.exception))
        with self.assertRaisesRegex(ContentError, "actor"):
            mhcontent.Api("https://x.co", "k", "Ørca")

    def test_timeout_on_a_write_says_it_may_have_landed(self):
        api = mhcontent.Api("https://x.supabase.co", "k", "orca")
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
            with self.assertRaisesRegex(ContentError, "may or may not"):
                api.insert("ideas", {"id": "x"})


class WrapperTests(unittest.TestCase):
    def run_mh(self, *argv):
        import os, subprocess, tempfile
        env = {k: v for k, v in os.environ.items() if k != "MELLONHEAD_ROOT"}
        env["MH_CODE"] = str(Path(__file__).resolve().parent.parent)
        env["SUPABASE_URL"], env["SUPABASE_SECRET_KEY"] = "https://127.0.0.1:1", "test"
        with tempfile.TemporaryDirectory() as d:
            return subprocess.run([str(Path(__file__).resolve().parent.parent / "mh"), *argv],
                                  cwd=d, env=env, capture_output=True, text=True, timeout=20)

    def test_only_the_command_word_skips_the_store_check(self):
        r = self.run_mh("task", "add", "proj", "Draft content calendar")
        self.assertIn("no task store found", r.stderr)
        r = self.run_mh("--actor", "content", "task", "next")
        self.assertIn("no task store found", r.stderr)
        r = self.run_mh("--actor")                      # used to spin forever
        self.assertIn("no task store found", r.stderr)
        r = self.run_mh("--actor", "orca", "content", "ideas")
        self.assertNotIn("no task store found", r.stderr)
        self.assertIn("database", r.stderr)


class TesterFixTests(Base):
    """From Devi's and Sam's test runs, 11/21."""

    def test_find_by_link_and_words(self):
        self.api.seed("ideas", **idea_row("carded", title="Boss trusts the chatbot", brief="cards/a.md",
                                          links=[{"label": "Storyboard", "url": "sb/b.md"}]))
        self.assertEqual([r["id"] for r in self.c.ideas(link="sb/b.md")], ["carded"])
        self.assertEqual([r["id"] for r in self.c.ideas(link="cards/a.md")], ["carded"])
        self.assertEqual([r["id"] for r in self.c.ideas(words="CHATBOT boss")], ["carded"])
        self.assertEqual(self.c.ideas(words="chatbot quit"), [])

    def test_channel_filter_ignores_case_and_is_checked(self):
        self.c.channels("dyslexia", add=["TikTok"])
        self.assertEqual([r["id"] for r in self.c.ideas(channel="tiktok")], ["dyslexia"])
        with self.assertRaisesRegex(ContentError, "not a channel"):
            self.c.ideas(channel="tiktak")
        with self.assertRaisesRegex(ContentError, "not a week"):
            self.c.ideas(week="garbage")

    def test_duplicate_by_link(self):
        self.c.add_idea("One", links=[("Card", "cards/x.md")])
        with self.assertRaisesRegex(ContentError, "looks like one"):
            self.c.add_idea("  ONE ")
        with self.assertRaisesRegex(ContentError, "looks like one"):
            self.c.add_idea("Different title", links=[("Card", "cards/x.md")])

    def test_only_from_leaves_other_statuses_alone(self):
        self.c.set_status("dyslexia", "rejected", note="no")
        with self.assertRaises(mhcontent.SkipMove):
            self.c.set_status("dyslexia", "backlog", only_from=["proposed"])
        self.assertEqual(self.get("dyslexia")["status"], "rejected")
        self.c.set_status("dyslexia", "backlog", only_from=["rejected"])
        self.assertEqual(self.get("dyslexia")["status"], "backlog")

    def test_week_rules(self):
        with self.assertRaisesRegex(ContentError, "has no week"):
            self.c.set_field("dyslexia", "week", "2026-11-23")
        with self.assertRaisesRegex(ContentError, "drop --week"):
            self.c.set_status("dyslexia", "backlog", week="2026-11-23")
        self.c.set_status("dyslexia", "scheduled", post_date="2026-12-02")
        self.c.set_status("dyslexia", "backlog")
        self.c.set_status("dyslexia", "scheduled")      # keeps its post date

    def test_title_cannot_be_blanked_and_her_name_is_hers(self):
        with self.assertRaisesRegex(ContentError, "needs a title"):
            self.c.set_field("dyslexia", "title", "  ")
        with self.assertRaisesRegex(ContentError, "her name"):
            self.c.note("dyslexia", "x", who="You")

    def test_takes_and_stories_editable(self):
        with self.assertRaisesRegex(ContentError, "take #3 already"):
            self.c.add_take("again", "Tools  don't fix process")
        t = self.c.add_take("care", "A care take", pillar="Messy, Value", on_camera="care: pair with take 1")
        self.assertEqual((t["pillar"], t["on_camera"]), ("Messy, Value", "care: pair with take 1"))
        with self.assertRaisesRegex(ContentError, "not a pillar"):
            self.c.add_take("x", "y", pillar="Messy, Fluff")
        self.c.set_bank("takes", 3, {"on_camera": "ready, client blinded"})
        self.assertEqual(self.api.rows("takes", {"n": 3})[0]["on_camera"], "ready, client blinded")
        self.c.set_bank("stories", "grant", {"missing": "the reply", "title": None})
        self.assertEqual(self.api.rows("stories", {"key": "grant"})[0]["missing"], "the reply")
        self.assertEqual(self.c.add_story("st", "S")["on_camera"], "blinded")
        with self.assertRaisesRegex(ContentError, "can't be empty"):
            self.c.set_bank("takes", 3, {"text": ""})

    def test_signal_takes_her_date_and_named_numbers(self):
        p = self.c.add_post("dyslexia", "TikTok")
        self.assertEqual(self.c.add_signal(p["id"], ["views=1"])["recorded_on"], "2026-11-19")
        with self.assertRaisesRegex(ContentError, "needs a name"):
            self.c.add_signal(p["id"], ["=5"], any_metric=True)

    def test_clearing_hook_format(self):
        p = self.c.add_post("dyslexia", "TikTok", hook_format="question")
        row, _ = self.c.set_post(p["id"], hook_format="")
        self.assertEqual(row["hook_format"], "")

    def test_unicode_title_slug(self):
        self.assertEqual(mhcontent.slug("Café naïve"), "cafe-naive")


class CheckerTests(unittest.TestCase):
    def test_content_lines_parse(self):
        import check_mh_usage as cmu
        parser = mhcli.build_parser()
        for line in [
            'mh content idea add <title> --link "Your #tiktok post, M/D" <url> --actor orca',
            "mh content idea status <id> <status> --only-from proposed",
            "mh content ideas --status <status>",
            "mh content idea put <id> <field> --file <path>",
            "mh content idea speaks-to <id> --add <part>",
            "mh content idea note <id> <text>",
            "mh task status <key#id> <status>",
        ]:
            cmd = cmu.cut_prose(line)
            self.assertIsNone(cmu.check(cmd, parser), line)
        self.assertIsNotNone(cmu.check("mh content idea set <id> bucket personal", parser))
        found = [c for _, c in cmu.extract('Run `mh content idea note <id> "Pitched 11/23, card (ts 1.2)"`, then rest.\n'
                                           'Or `mh content idea put <id> hooks --file - < hooks.json`.')]
        self.assertEqual(found, ['mh content idea note <id> "Pitched 11/23, card (ts 1.2)"',
                                 'mh content idea put <id> hooks --file -'])
        for c in found:
            self.assertIsNone(cmu.check(c, parser), c)
        self.assertEqual(cmu.cut_prose('mh content idea note x "a, b" then, more'),
                         'mh content idea note x "a, b" then')


class WeekTests(unittest.TestCase):
    def test_making_week(self):
        self.assertEqual(making_week(date(2026, 11, 19)), date(2026, 11, 16))   # Thu
        self.assertEqual(making_week(date(2026, 11, 20)), date(2026, 11, 23))   # Fri rolls
        self.assertEqual(making_week(date(2026, 11, 22)), date(2026, 11, 23))   # Sun
        self.assertEqual(making_week(date(2026, 11, 16)), date(2026, 11, 16))   # Mon
        self.assertEqual(making_week(date(2026, 11, 20), 5), date(2026, 11, 16))  # Fri film day holds
        self.assertEqual(making_week(date(2026, 11, 21), 5), date(2026, 11, 23))


class CliTests(Base):
    def run_cli(self, *argv):
        args = mhcli.build_parser().parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stderr", err):
            code = mhcontent.run(args, content=self.c, out=out)
        return code, out.getvalue(), err.getvalue()

    def test_commands_parse_and_run(self):
        code, out, _ = self.run_cli("content", "idea", "set", "dyslexia", "feeling", "cheeky")
        self.assertEqual((code, out.strip()), (0, "dyslexia: feeling set."))
        code, out, _ = self.run_cli("content", "ideas", "--json")
        self.assertEqual(code, 0)
        self.assertEqual({r["id"] for r in json.loads(out)}, {"dyslexia", "hooked"})
        code, _, err = self.run_cli("content", "idea", "set", "dyslexia", "feeling", "sad")
        self.assertEqual(code, 1)
        self.assertIn("not a feeling option", err)
        code, out, _ = self.run_cli("content", "idea", "show", "hooked")
        self.assertIn("feeling: seen (drafted)", out)

    def test_actor_flag_either_side(self):
        args = mhcli.build_parser().parse_args(["content", "idea", "note", "x", "hi", "--actor", "cody"])
        self.assertEqual(args.actor, "cody")
        args = mhcli.build_parser().parse_args(["--actor", "cody", "content", "ideas"])
        self.assertEqual(args.actor, "cody")

    def test_main_needs_no_task_store(self):
        with mock.patch.object(mhcontent, "load_credentials",
                               side_effect=ContentError("no database settings")), \
             mock.patch("sys.stderr", io.StringIO()) as err:
            code = mhcli.main(["--repo", "/nonexistent", "content", "ideas"])
        self.assertEqual(code, 1)
        self.assertIn("no database settings", err.getvalue())


class SecretTests(unittest.TestCase):
    def test_credentials_from_file_and_never_in_errors(self):
        import tempfile, urllib.error
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write('SUPABASE_URL="https://x.supabase.co"\nSUPABASE_SECRET_KEY=sb_secret_TOPSECRET\n')
        url, key = mhcontent.load_credentials(env={}, path=f.name)
        self.assertEqual((url, key), ("https://x.supabase.co", "sb_secret_TOPSECRET"))
        with self.assertRaises(ContentError) as cm:
            mhcontent.load_credentials(env={}, path="/nonexistent")
        self.assertNotIn("TOPSECRET", str(cm.exception))

        api = mhcontent.Api(url, key, "orca")
        err = urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(
            b'{"message":"Invalid API key","code":"401"}'))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ContentError) as cm:
                api.rows("ideas")
        self.assertIn("Invalid API key", str(cm.exception))
        self.assertNotIn("TOPSECRET", str(cm.exception))

    def test_request_shape(self):
        seen = {}
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def fake(req, timeout):
            seen["url"], seen["method"] = req.full_url, req.get_method()
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            seen["body"] = req.data
            return Resp(b"[]")
        api = mhcontent.Api("https://x.supabase.co", "k", "orca")
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertIsNone(api.update("ideas", "id", "a b", "2026-11-22T00:00:01.5+00:00", {"title": "t"}))
        self.assertEqual(seen["method"], "PATCH")
        self.assertIn("updated_at=eq.2026-11-22T00:00:01.5%2B00:00", seen["url"])
        self.assertIn("id=eq.a+b", seen["url"])
        self.assertEqual(seen["headers"]["x-mh-actor"], "orca")
        self.assertEqual(seen["headers"]["content-profile"], "content")
        self.assertEqual(json.loads(seen["body"]), {"title": "t"})


if __name__ == "__main__":
    unittest.main()
