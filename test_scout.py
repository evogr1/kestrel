#!/usr/bin/env python3
"""Offline tests. No network, no keys. Run: python3 test_scout.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import scout_growth
import scout_social
import urllib.error
import urllib.request
import datetime as dt
import re
import urllib.parse

os.environ["SCOUT_TZ"] = "Asia/Kolkata"   # the slot tests assume this zone
import scout_core as sc
import scout_server


def iso(days):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def vid(i, views, age, ch="UC" + "a" * 22, dur=600, title=None):
    return {"id": "v%010d" % i, "title": title or "t%d" % i, "views": views, "likes": 0, "comments": 0, "duration": dur,
            "is_short": 0 < dur <= 180, "published": iso(age), "age_days": float(age), "channel": "Chan", "channel_id": ch,
            "url": "u", "thumbnail": "t"}


class FakeYT:
    use_api = True

    def __init__(self, uploads, subs=50000):
        self.uploads, self.subs = uploads, subs

    def channel_info(self, ref):
        return {"id": "UC" + "a" * 22, "title": "Chan", "subscribers": self.subs, "uploads_playlist": "UU" + "a" * 22, "url": "x"}

    def channels_info(self, ids):
        return {i: {"id": i, "title": "Chan", "subscribers": self.subs, "uploads_playlist": "UU" + i[2:], "url": "x"} for i in ids}

    def recent(self, ch, kind="any", n=30):
        return [v for v in self.uploads if sc.YouTube._kind_ok(v, kind)][:n]

    def search(self, q, n=30, order="relevance", days=None, kind="any"):
        return {"videos": self.uploads[:5], "total_estimate": 12345}

    def autocomplete(self, s):
        return [s + " tips", s + " hack"]


def make_scout(tmp, yt):
    s = sc.Scout(sc.Config(db=os.path.join(tmp, "c.sqlite"), yt_key="", ig_pool=""))
    s.yt = yt
    return s


class Core(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_helpers(self):
        self.assertEqual(sc.parse_duration("PT1M30S"), 90)
        self.assertEqual(sc.parse_duration("PT1H2M3S"), 3723)
        self.assertEqual(sc.video_id("https://youtu.be/dQw4w9WgXcQ?t=1"), "dQw4w9WgXcQ")
        self.assertEqual(sc.video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(sc.parse_channel_ref("https://www.youtube.com/@MrBeast/videos"), ("handle", "@MrBeast"))
        self.assertEqual(sc.parse_channel_ref("demo"), ("handle", "@demo"))
        with self.assertRaises(sc.ScoutError):
            sc.video_id("nope")

    def test_outlier_math(self):
        ups = [vid(i, 1000, 20 + i) for i in range(1, 10)] + [vid(99, 12000, 10)]
        s = make_scout(self.tmp, FakeYT(ups))
        r = s.outliers(channels=["@x"], min_multiple=3)
        self.assertEqual(len(r["results"]), 1)
        top = r["results"][0]
        self.assertEqual(top["id"], vid(99, 0, 0)["id"])
        self.assertEqual(top["multiple"], 12.0)
        self.assertEqual(top["channel_median_views"], 1000)

    def test_outlier_on_topic_filter(self):
        ups = [vid(i, 1000, 20 + i) for i in range(1, 10)]
        ups += [vid(98, 12000, 10, title="Unclog Shower Drain Fast"), vid(99, 12000, 11, title="Modern Bathroom Tour")]
        s = make_scout(self.tmp, FakeYT(ups))
        r = s.outliers(query="fix shower drain", min_multiple=3)
        self.assertEqual([x["id"] for x in r["results"]], [vid(98, 0, 0)["id"]])
        self.assertTrue(any("dropped" in n for n in r["notes"]))
        r2 = s.outliers(query="fix shower drain", min_multiple=3, on_topic=False)
        self.assertEqual(len(r2["results"]), 2)

    def test_outlier_excludes_self_from_baseline_and_old(self):
        ups = [vid(i, 1000, 20 + i) for i in range(1, 10)] + [vid(99, 12000, 400)]
        s = make_scout(self.tmp, FakeYT(ups))
        self.assertEqual(s.outliers(channels=["@x"], max_age_days=90)["results"], [])

    def test_size_filter(self):
        ups = [vid(i, 1000, 20 + i) for i in range(1, 10)] + [vid(99, 12000, 10)]
        s = make_scout(self.tmp, FakeYT(ups, subs=500000))
        self.assertEqual(s.outliers(channels=["@x"], max_subs=100000)["results"], [])
        self.assertEqual(len(s.outliers(channels=["@x"], min_subs=100000)["results"]), 1)

    def test_thin_baseline_skipped(self):
        s = make_scout(self.tmp, FakeYT([vid(1, 9000, 10), vid(2, 10, 20)]))
        r = s.outliers(channels=["@x"])
        self.assertEqual(r["results"], [])
        self.assertTrue(any("baseline too thin" in n for n in r["notes"]))

    def test_shorts_kind(self):
        ups = [vid(i, 1000, 20 + i, dur=30) for i in range(1, 10)] + [vid(99, 9000, 10, dur=30), vid(98, 90000, 10, dur=900)]
        s = make_scout(self.tmp, FakeYT(ups))
        ids = [r["id"] for r in s.outliers(channels=["@x"], kind="shorts")["results"]]
        self.assertEqual(ids, [vid(99, 0, 0)["id"]])

    def test_channel_stats(self):
        ups = [vid(i, 1000, 7 * i) for i in range(1, 11)]
        s = make_scout(self.tmp, FakeYT(ups))
        r = s.channel_stats("@x")
        self.assertEqual(r["median_views"], 1000)
        self.assertAlmostEqual(r["uploads_per_week"], 1.0, 1)
        self.assertEqual(r["hit_rate_2x_pct"], 0.0)

    def test_keyword_demand(self):
        s = make_scout(self.tmp, FakeYT([vid(i, 5000, 10 + i) for i in range(1, 8)]))
        r = s.keyword_demand("drain", terms=3)
        self.assertEqual(len(r["terms"]), 3)
        self.assertIn("Estimates", r["notes"][0])
        self.assertTrue(all(t["supply_results_estimate"] == 12345 and t["demand_unit"] == "views_per_day" for t in r["terms"]))

    def test_json3(self):
        d = {"events": [{"tStartMs": 0, "segs": [{"utf8": "Hello"}, {"utf8": " there"}]}, {"tStartMs": 61000, "segs": [{"utf8": "bye"}]}, {"tStartMs": 100}]}
        r = sc.parse_json3(d)
        self.assertEqual(r["text"], "Hello there bye")
        self.assertEqual(r["hook_first_30s"], "Hello there")
        self.assertEqual(r["words"], 3)

    def test_cache_and_budget(self):
        c = sc.Cache(os.path.join(self.tmp, "b.sqlite"))
        n = []
        c.cached("k", 60, lambda: n.append(1) or {"a": 1})
        c.cached("k", 60, lambda: n.append(1) or {"a": 1})
        self.assertEqual(len(n), 1)
        y = sc.YouTube(sc.Config(db=":memory:", yt_key="K", yt_budget=150), c)
        c.add_usage("yt_units", 100)
        with self.assertRaises(sc.ScoutError) as cm:
            y.api("search", 100, q="x")
        self.assertEqual(cm.exception.status, 429)

    def test_pool_formats(self):
        d = tempfile.mkdtemp()
        cases = {"list": [{"token": "t1", "ig_user_id": "1"}, {"access_token": "t2", "id": 2, "label": "b"}],
                 "single": {"token": "t", "ig_user_id": "9"}, "dict": {"nicheone": {"token": "t", "ig_user_id": "3"}, "bad": {"x": 1}}}
        want = {"list": 2, "single": 1, "dict": 1}
        for k, v in cases.items():
            p = os.path.join(d, k + ".json")
            with open(p, "w") as f:
                json.dump(v, f)
            self.assertEqual(len(sc.load_pool([p])), want[k], k)
        self.assertEqual(sc.load_pool(["/nonexistent"]), [])
        self.assertEqual(sc.load_pool([os.path.join(d, "dict.json")])[0]["label"], "nicheone")


class IG(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = sc.Scout(sc.Config(db=os.path.join(self.tmp, "c.sqlite"), ig_pool=""))

    def reels(self, likes, base_age=5):
        return [{"media_product_type": "REELS", "like_count": l, "comments_count": 1, "permalink": "https://i/r%d" % i,
                 "timestamp": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=base_age + i)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                 "caption": "c"} for i, l in enumerate(likes)]

    def test_outliers(self):
        self.s.ig.profile = lambda u: {"username": u, "followers": 10000, "media": self.reels([100, 110, 90, 105, 95, 1000])}
        r = self.s.ig_outliers(users=["@Foo"], min_multiple=3)
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["multiple"], 10.0)
        self.assertIn("by views where Meta returns them", r["notes"][0])
        self.assertEqual(r["results"][0]["scored_by"], "likes")   # this fixture has no view_count

    def test_no_token_error(self):
        os.environ.pop("SCOUT_IG_POOL", None)
        self.s.ig.pool = lambda: []
        with self.assertRaises(sc.ScoutError) as cm:
            self.s.ig.profile("x")
        self.assertEqual(cm.exception.status, 501)

    def test_quota_stop_and_rotation(self):
        s = self.s
        s.ig.pool = lambda: [{"label": "a", "token": "TOKEN_A_SECRET", "ig_user_id": "1"}, {"label": "b", "token": "TOKEN_B_SECRET", "ig_user_id": "2"}]
        s.cache.put("igstate:a", {"pct": 95, "at": 9e12}, 3600)
        s.cache.put("igstate:b", {"pct": 10, "at": 9e12}, 3600)
        urls = []

        class Hdr(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)

        def fake(url, timeout=40):
            urls.append(url)
            return {"business_discovery": {"followers_count": 5, "media": {"data": []}}}, Hdr({"x-app-usage": '{"call_count": 12}'})
        orig = sc.http_json
        sc.http_json = fake
        try:
            p = s.ig._fetch("someone")
        finally:
            sc.http_json = orig
        self.assertEqual(p["via"], "b")
        self.assertIn("/2?", urls[0])
        self.assertEqual(s.ig.state("b")["pct"], 12)
        self.assertNotIn("TOKEN", json.dumps(s.ig.status()))


class Server(unittest.TestCase):
    KEY = "k" * 32

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        ups = [vid(i, 1000, 20 + i) for i in range(1, 10)] + [vid(99, 12000, 10)]
        cls.scout = make_scout(cls.tmp, FakeYT(ups))
        cls.srv = scout_server.make_server(cls.scout, cls.KEY, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path, key=KEY, hdr="X-Scout-Key"):
        h = {hdr: key if hdr != "Authorization" else "Bearer " + key} if key else {}
        try:
            r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), headers=h))
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health_open_rest_locked(self):
        self.assertEqual(self.get("/health", key=None)[0], 200)
        self.assertEqual(self.get("/v1/usage", key=None)[0], 401)
        self.assertEqual(self.get("/v1/usage", key="wrong")[0], 401)

    def test_both_auth_styles(self):
        self.assertEqual(self.get("/v1/usage")[0], 200)
        self.assertEqual(self.get("/v1/usage", hdr="Authorization")[0], 200)

    def test_outliers_route(self):
        code, d = self.get("/v1/outliers?channels=@x,@y&min_multiple=3&limit=5")
        self.assertEqual(code, 200)
        self.assertEqual(d["results"][0]["multiple"], 12.0)

    def test_errors(self):
        self.assertEqual(self.get("/v1/outliers")[0], 400)
        self.assertEqual(self.get("/v1/outliers?bogus=1&query=x")[0], 400)
        self.assertEqual(self.get("/v1/outliers?query=x&limit=abc")[0], 400)
        self.assertEqual(self.get("/v1/nope")[0], 404)
        self.assertEqual(self.get("/v1/channel")[0], 400)


class MCP(unittest.TestCase):
    def test_protocol(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "scout_usage", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}}]
        p = subprocess.run([sys.executable, "scout_mcp.py"], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                           capture_output=True, text=True, timeout=30, env={**os.environ, "SCOUT_URL": "http://127.0.0.1:9"})
        out = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual([o["id"] for o in out], [1, 2, 3, 4])
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "kestrel")
        names = [t["name"] for t in out[1]["result"]["tools"]]
        import scout_mcp
        self.assertEqual(len(names), len(scout_mcp.TOOLS) + len(scout_mcp.GATEWAY))
        for _, route, _, _ in scout_mcp.TOOLS:
            self.assertIn(route, scout_server.ROUTES)
            self.assertEqual(route in scout_mcp.POST, scout_server.ROUTES[route][1] == "POST")
        self.assertTrue(out[2]["result"]["isError"])
        self.assertIn("cannot reach Scout", out[2]["result"]["content"][0]["text"])
        self.assertTrue(out[3]["result"]["isError"])
        self.assertIn("kestrel_tools", out[3]["result"]["content"][0]["text"])


# ---------------------------------------------------------------- v2 (scout_plus)

import scout_plus as sp


class LocalModeTests(unittest.TestCase):
    def test_stdio_runs_in_process_without_a_server(self):
        tmp = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SCOUT_", "YOUTUBE_"))}
        env.update({"SCOUT_DB": os.path.join(tmp, "k.db"), "HOME": tmp, "YOUTUBE_API_KEY": "x"})
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "scout_usage", "arguments": {}}}]
        p = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scout_mcp.py")],
                           input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True, env=env, timeout=60)
        out = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        self.assertEqual([o["id"] for o in out], [1, 2, 3], p.stderr[-2000:])
        tools = {x["name"]: x for x in out[1]["result"]["tools"]}
        self.assertIn("_meta", tools["scout_creator_breakdown"])
        self.assertFalse(out[2]["result"]["isError"], out[2])

    def test_instagram_token_from_env(self):
        old = {k: os.environ.get(k) for k in ("SCOUT_IG_TOKEN", "SCOUT_IG_USER_ID")}
        os.environ.update({"SCOUT_IG_TOKEN": "tok", "SCOUT_IG_USER_ID": "178"})
        try:
            pool = sc.Instagram(sc.Config(ig_pool="/nope"), None).pool()
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertEqual(pool, [{"label": "default", "token": "tok", "ig_user_id": "178"}])


class FakeYT2(FakeYT):
    def videos(self, ids):
        return [v for v in self.uploads if v["id"] in ids]

    def transcript(self, vid, lang="en"):
        return {"hook_first_30s": "hook for " + vid}


def make_plus(tmp, yt):
    s = sp.ScoutPlus(sc.Config(db=os.path.join(tmp, "c.sqlite"), yt_key="", ig_pool=""))
    s.yt = yt
    return s


def drain_uploads():
    ups = [vid(i, 1000, 10 + i, title="Drain cleaning routine part %s" % chr(96 + i)) for i in range(1, 13)]
    ups += [vid(50 + i, 9000 + i, 5 + i, title="Fix a slow DRAIN in %d minutes?" % (i + 2)) for i in range(4)]
    return ups


class Plus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_niches(self):
        s = make_plus(self.tmp, FakeYT2(drain_uploads()))
        s.niche_add("Demo", "drain fix", kind="any")
        self.assertEqual(s.niches()["niches"][0]["name"], "demo")
        with self.assertRaises(sc.ScoutError):
            s.niche_add("bad/name", "q")
        with self.assertRaises(sc.ScoutError):
            s.niche_add("ok", "q", kind="reels")
        self.assertTrue(s.niche_remove("demo")["removed"])
        self.assertEqual(s.niches()["niches"], [])

    def test_feed_new_then_seen_and_alerts(self):
        ups = drain_uploads()
        s = make_plus(self.tmp, FakeYT2(ups, subs=5000))
        self.assertEqual(s.feed()["items"], [])
        s.niche_add("demo", "drain", min_multiple=3, max_age_days=60, breakout_max_subs=10000)
        s.track_add("@rival")
        s._today = lambda back=0: (dt.date(2026, 11, 10) - dt.timedelta(days=back)).isoformat()
        r1 = s.run_daily()
        self.assertEqual(r1["niches"][0]["new"], 4)
        self.assertEqual(r1["tracked"][0]["new_outliers"], 0)  # existing outliers are the baseline
        f = s.feed()
        self.assertEqual(f["day"], "2026-11-10")
        self.assertTrue(all(i["new"] for i in f["items"]))
        self.assertEqual(len(f["breakouts"]), 1)
        ups.append(vid(200, 50000, 4, title="Drain trick nobody uses"))
        s._today = lambda back=0: (dt.date(2026, 11, 11) - dt.timedelta(days=back)).isoformat()
        r2 = s.run_daily()
        self.assertEqual(r2["niches"][0]["new"], 1)
        self.assertEqual(r2["tracked"][0]["new_outliers"], 1)
        f2 = s.feed(only_new=True)
        self.assertEqual([i["id"] for i in f2["items"]], [vid(200, 0, 0)["id"]])
        comp = s.competitors()["tracked"][0]
        self.assertEqual(len(comp["new_outliers"]), 1)
        self.assertTrue(s.track_remove("@rival")["removed"])

    def test_niche_from_lists(self):
        ups = drain_uploads()
        for v in ups:
            v["title"] = v["title"].replace("Drain", "Pipe").replace("DRAIN", "PIPE")  # off the query on purpose
        s = make_plus(self.tmp, FakeYT2(ups))
        with self.assertRaises(sc.ScoutError):
            s.niche_add("empty")
        saved = s.niche_add("demo", channels=["demochannel", "https://www.youtube.com/@other", "UC" + "b" * 22], ig_users=["@pageone", "pagetwo"])["saved"]
        self.assertEqual(saved["channels"][0], "@demochannel")
        self.assertEqual(saved["ig_users"], ["pageone", "pagetwo"])
        self.assertIsNone(saved["query"])
        out = s.run_daily()
        self.assertEqual(out["niches"][0]["outliers"], 4)  # curated list, no topic filter
        self.assertTrue(any("instagram" in str(e) for e in out["errors"]))  # no IG token in this test
        r = s.ideas(niche="demo", n=2)
        self.assertEqual(len(r["seeds"]), 2)
        cal = s.calendar(niche="demo", days=7, per_week=2)
        self.assertTrue(cal["slots"])

    def test_breakouts_filters_size(self):
        s = make_plus(self.tmp, FakeYT2(drain_uploads(), subs=2000))
        r = s.breakouts("drain", max_subs=10000, min_median_views=500)
        self.assertEqual(len(r["results"]), 1)
        self.assertGreater(r["results"][0]["views_per_subscriber"], 0.4)
        self.assertEqual(s.breakouts("drain", max_subs=1000)["results"], [])

    def test_wins_finds_patterns(self):
        s = make_plus(self.tmp, FakeYT2(drain_uploads()))
        w = s.wins("drain", win_multiple=3)
        self.assertEqual(w["winners"], 4)
        pats = {p["pattern"]: p for p in w["title_patterns"]}
        self.assertEqual(pats["question"]["winners_pct"], 100)
        self.assertGreater(pats["question"]["lift"], 5)
        self.assertIn("slow", [x["word"] for x in w["winning_words"]])
        self.assertNotIn("drain", [x["word"] for x in w["winning_words"]])

    def test_optimize_flags_fixes(self):
        ups = drain_uploads()
        for v in ups[12:]:
            v["tags"] = ["drain hack", "plumbing"]
        target = vid(300, 1500, 20, title="my drain video")
        target["tags"], target["description"] = [], "short"
        ups.append(target)
        s = make_plus(self.tmp, FakeYT2(ups))
        o = s.optimize(target["id"], query="drain")
        c = {x["check"]: x for x in o["checks"]}
        self.assertEqual(c["title patterns"]["status"], "fix")
        self.assertEqual(c["tags"]["status"], "fix")
        self.assertIn("drain hack", c["tags"]["fix"])
        self.assertEqual(c["description"]["status"], "fix")
        self.assertEqual(c["keyword in title"]["status"], "pass")
        self.assertLess(o["score"], 70)

    def test_timing_and_calendar(self):
        ups = drain_uploads()
        s = make_plus(self.tmp, FakeYT2(ups))
        t = s.timing(channels=["@x"])
        self.assertEqual(t["uploads_read"], 16)
        self.assertTrue(t["by_hour"])
        cal = s.calendar(channels=["@x"], days=14, per_week=2)
        self.assertEqual(len(cal["slots"]), 4)
        self.assertEqual(len({x["weekday"] for x in cal["slots"]}), 2)

    def test_ideas_from_niche_feed(self):
        s = make_plus(self.tmp, FakeYT2(drain_uploads()))
        s.niche_add("demo", "drain", min_multiple=3, max_age_days=60)
        s.run_daily()
        r = s.ideas(niche="demo", n=3, with_hooks=True)
        self.assertEqual(len(r["seeds"]), 3)
        self.assertTrue(r["seeds"][0]["hook_first_30s"].startswith("hook for"))
        self.assertIn("A_reframe_trait", r["seeds"][0]["angles"])
        self.assertIn("three ways", r["instructions"])
        self.assertNotIn("\u2014", r["instructions"])

    @unittest.skipIf(sp.Image is None, "Pillow not installed")
    def test_thumbnails(self):
        from PIL import Image as Img
        import io as _io

        def png(vid_id):
            n = int(vid_id[1:])
            im = Img.new("RGB", (480, 360), (0, 0, 0))
            col = (240, 40, 40) if n >= 50 else (30, 60, 200)
            im.paste(Img.new("RGB", (240, 270), col), (0 if n >= 50 else 240, 45))
            b = _io.BytesIO()
            im.save(b, "PNG")
            return b.getvalue()

        orig = sp.http_bytes
        sp.http_bytes = lambda url, timeout=20: png(url.split("/vi/")[1].split("/")[0])
        try:
            s = make_plus(self.tmp, FakeYT2(drain_uploads()))
            r = s.similar_thumbnails(vid(50, 0, 0)["id"], query="drain")
            self.assertGreaterEqual(r["results"][0]["similarity"], 0.99)
            self.assertGreaterEqual(r["results"][0]["id"], "v0000000051")
            self.assertLess(r["results"][-1]["similarity"], 0.6)
            w = s.wins("drain", with_thumbs=True)
            self.assertTrue(w["thumbnails"]["winner_colors"])
            self.assertIn("saturation", w["thumbnails"])
        finally:
            sp.http_bytes = orig


class ServerV2(unittest.TestCase):
    KEY = "z" * 32

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.scout = make_plus(cls.tmp, FakeYT2(drain_uploads()))
        cls.srv = scout_server.make_server(cls.scout, cls.KEY, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, path, body=None, method=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                   headers={"X-Scout-Key": self.KEY, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_legal_pages_open(self):
        for path in ("/privacy", "/terms", "/data-deletion"):
            r = urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path))
            body = r.read().decode()
            self.assertEqual(r.status, 200)
            self.assertIn("Kestrel", body)
            self.assertNotIn("\u2014", body)

    def test_post_flow(self):
        self.assertEqual(self.req("/v1/niches/add", {"name": "demo", "query": "drain", "min_multiple": 3})[0], 200)
        self.assertEqual(self.req("/v1/niches")[1]["niches"][0]["query"], "drain")
        self.assertEqual(self.req("/v1/niches/add")[0], 405)
        self.assertEqual(self.req("/v1/niches/add", raw=b"{nope", method="POST")[0], 400)
        code, d = self.req("/v1/jobs/daily", {})
        self.assertEqual(code, 200)
        self.assertIn("demo", [n["niche"] for n in d["niches"]])
        self.assertEqual(self.req("/v1/feed?niche=demo&only_new=true")[0], 200)
        self.assertEqual(self.req("/v1/wins?query=drain")[0], 200)

    def test_mcp_post_tool(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "scout_niche_add", "arguments": {"name": "mcpniche", "query": "drain"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "scout_niches", "arguments": {}}}]
        p = subprocess.run([sys.executable, "scout_mcp.py"], input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True,
                           text=True, timeout=30, env={**os.environ, "SCOUT_URL": "http://127.0.0.1:%d" % self.port, "SCOUT_API_KEY": self.KEY})
        out = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertFalse(out[0]["result"]["isError"])
        self.assertIn("mcpniche", out[1]["result"]["content"][0]["text"])


# ---------------------------------------------------------------- own channels (scout_mine)

import scout_mine as sm


class FakeGoogle:
    """Answers the OAuth, Data API, Analytics and Graph calls scout_mine makes."""

    def __init__(self):
        self.subs = 900
        self.calls = []
        self.fail_dims = set()

    def __call__(self, url, data=None, headers=None, timeout=40):
        self.calls.append(url)
        if "oauth2.googleapis.com/token" in url:
            assert b"REFRESH-SECRET" in data
            return {"access_token": "ACCESS-SECRET", "expires_in": 3600}
        if "youtube/v3/channels" in url:
            assert headers["Authorization"] == "Bearer ACCESS-SECRET"
            return {"items": [{"id": "UCme", "snippet": {"title": "Demo Tries It", "publishedAt": "2025-01-01T00:00:00Z"},
                               "statistics": {"subscriberCount": str(self.subs), "viewCount": "1500000", "videoCount": "40"},
                               "contentDetails": {"relatedPlaylists": {"uploads": "UUme"}}}]}
        if "youtube/v3/playlistItems" in url:
            now = dt.datetime.now(dt.timezone.utc)
            return {"items": [{"contentDetails": {"videoId": "vid%08d" % i,
                                                  "videoPublishedAt": (now - dt.timedelta(days=3 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")}} for i in range(20)]}
        if "youtube/v3/videos" in url:
            ids = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["id"][0].split(",")
            return {"items": [{"id": i, "snippet": {"title": "Title " + i}, "contentDetails": {}} for i in ids]}
        if "youtubeanalytics" in url:
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
            dim, mets = q.get("dimensions"), q["metrics"].split(",")
            if dim in self.fail_dims:
                raise sc.ScoutError("500 An internal error has occurred.", 500)
            days = (dt.date.fromisoformat(q["endDate"]) - dt.date.fromisoformat(q["startDate"])).days + 1
            def row(scale=1.0):
                base = {"views": 1000 * days, "estimatedMinutesWatched": 600 * days, "averageViewDuration": 95, "averageViewPercentage": 42.0,
                        "likes": 30 * days, "comments": 3 * days, "shares": 2 * days, "subscribersGained": 4 * days, "subscribersLost": 1 * days,
                        "viewerPercentage": 25.0}
                return [base[m] * scale for m in mets]
            if dim == "creatorContentType":
                return {"columnHeaders": [{"name": "creatorContentType"}] + [{"name": m} for m in mets],
                        "rows": [["shorts"] + row(2.0), ["videoOnDemand"] + row(0.5)]}
            if dim == "ageGroup,gender":
                return {"columnHeaders": [{"name": "ageGroup"}, {"name": "gender"}, {"name": "viewerPercentage"}],
                        "rows": [["age18-24", "male", 30.0], ["age25-34", "male", 40.0], ["age25-34", "female", 30.0]]}
            if dim == "video":
                return {"columnHeaders": [{"name": "video"}] + [{"name": m} for m in mets],
                        "rows": [["vid%08d" % i] + [100 * (5 - i)] * len(mets) for i in range(int(q.get("maxResults", 5)))]}
            if dim:
                return {"columnHeaders": [{"name": dim}] + [{"name": m} for m in mets], "rows": [["X"] + row()]}
            scale = 1.25 if q["startDate"] > str(dt.date.today() - dt.timedelta(days=29)) else 1.0
            return {"columnHeaders": [{"name": m} for m in mets], "rows": [row(scale)]}
        if "graph.facebook.com" in url:
            assert "access_token=IGTOKEN-SECRET" in url
            path = urllib.parse.urlparse(url).path
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
            if path.endswith("/17841") and "fields" in q:
                return {"username": "demo.channel", "followers_count": 5200, "media_count": 80}
            if path.endswith("/insights") and "breakdown" in q:
                return {"data": [{"total_value": {"breakdowns": [{"results": [{"dimension_values": ["IN"], "value": 300},
                                                                              {"dimension_values": ["US"], "value": 900}]}]}}]}
            if path.endswith("/insights") and q.get("metric") == "profile_links_taps":
                raise sc.ScoutError("400 metric not available", 401)
            if path.endswith("/17841/insights"):
                return {"data": [{"name": q["metric"], "total_value": {"value": 1234}}]}
            if path.endswith("/media"):
                now = dt.datetime.now(dt.timezone.utc)
                return {"data": [{"id": "m%d" % i, "media_product_type": "REELS", "permalink": "https://ig/r%d" % i, "caption": "reel %d" % i,
                                  "like_count": 10 * i, "comments_count": i,
                                  "timestamp": (now - dt.timedelta(days=2 * i + 1)).strftime("%Y-%m-%dT%H:%M:%S+0000")} for i in range(12)]}
            if "/m" in path and path.endswith("/insights"):
                i = int(path.split("/m")[-1].split("/")[0])
                vals = {"views": 1000 + 500 * (12 - i), "reach": 800, "saved": 20, "shares": 10 + i, "total_interactions": 60,
                        "ig_reels_avg_watch_time": 6400}
                return {"data": [{"name": m, "values": [{"value": vals[m]}]} for m in q["metric"].split(",")]}
        raise AssertionError("unexpected url " + url)


class MineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "oauth")
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "youtube_client.json"), "w") as f:
            json.dump({"installed": {"client_id": "cid", "client_secret": "csecret"}}, f)
        with open(os.path.join(self.dir, "youtube_token_demo.json"), "w") as f:
            json.dump({"refresh_token": "REFRESH-SECRET"}, f)
        pool = os.path.join(self.tmp, "pool.json")
        with open(pool, "w") as f:
            json.dump([{"label": "demo", "token": "IGTOKEN-SECRET", "ig_user_id": "17841"}], f)
        self.s = sp.ScoutPlus(sc.Config(db=os.path.join(self.tmp, "c.sqlite"), yt_key="", ig_pool=pool))
        self.s.mine.dir = self.dir
        self.fake = FakeGoogle()
        self.orig, sm._http = sm._http, self.fake

    def tearDown(self):
        sm._http = self.orig

    def clean(self, obj):
        txt = json.dumps(obj)
        for secret in ("REFRESH-SECRET", "ACCESS-SECRET", "IGTOKEN-SECRET", "csecret"):
            self.assertNotIn(secret, txt)

    def test_channels_and_race(self):
        ch = self.s.me_channels()
        self.assertEqual(ch["channels"][0]["title"], "Demo Tries It")
        self.assertEqual(ch["accounts"][0]["username"], "demo.channel")
        r = self.s.me_race("demo")
        self.clean(r)
        long_path, shorts_path = r["paths"][0], r["paths"][1]
        self.assertEqual(long_path["watch_hours_365d"]["have"], round(600 * 365 * 0.5 / 60, 1))
        self.assertEqual(shorts_path["shorts_views_90d"]["have"], 1000 * 90 * 2.0)
        self.assertEqual(long_path["subscribers"]["need"], 1000)
        self.assertGreater(long_path["eta_days"], 0)
        self.assertTrue(r["paths"][2]["met"] is False or r["paths"][2]["uploads_90d"]["have"] >= 3)
        self.assertIsNotNone(r["fastest_path"])
        self.assertEqual(sum(1 for u in self.fake.calls if "oauth2" in u), 1)  # access token reused

    def test_scorecard(self):
        sc_ = self.s.me_scorecard("demo")
        self.clean(sc_)
        m = {x["metric"]: x for x in sc_["metrics"]}
        self.assertEqual(m["views"]["change_pct"], 25.0)
        self.assertTrue(0 <= sc_["score"] <= 100)
        self.assertIn(sc_["grade"], "ABCDE")
        self.assertEqual(sc_["top_videos_28d"][0]["title"], "Title vid00000000")

    def test_achievements_new_on_second_check(self):
        a1 = self.s.me_achievements("demo")
        self.assertTrue(all(not x["new_since_last_check"] for x in a1["achievements"]))
        subs = next(x for x in a1["achievements"] if x["track"] == "subscribers")
        self.assertEqual(subs["next"], 1000)
        self.fake.subs = 1200
        a2 = self.s.me_achievements("demo")
        subs2 = next(x for x in a2["achievements"] if x["track"] == "subscribers")
        self.assertEqual(subs2["new_since_last_check"], [1000])

    def test_audience(self):
        a = self.s.me_audience("demo")
        self.assertEqual(a["age_pct"]["age25-34"], 70.0)
        self.assertEqual(a["gender_pct"]["male"], 70.0)
        self.assertEqual(a["videos_that_bring_subscribers"][0]["subs_per_1k_views"], 1000.0)

    def test_audience_survives_partial_youtube_errors(self):
        self.fake.fail_dims = {"ageGroup,gender", "insightTrafficSourceDetail"}
        a = self.s.me_audience("demo")
        self.assertEqual(a["age_pct"], {})
        self.assertIn("age_gender", a["unavailable"])
        self.assertEqual(a["traffic_sources"][0]["share_pct"], 100.0)
        self.assertTrue(a["videos_that_bring_subscribers"])

    def test_audience_with_no_data_at_all(self):
        self.fake.fail_dims = {"ageGroup,gender", "country", "subscribedStatus", "deviceType", "insightTrafficSourceType",
                               "insightTrafficSourceDetail", "video"}
        with self.assertRaises(sc.ScoutError) as cm:
            self.s.me_audience("demo")
        self.assertEqual(cm.exception.status, 404)

    def test_live_stats(self):
        k = make_k(self.tmp)
        k.mine.dir = self.dir
        now = dt.datetime.now(dt.timezone.utc)
        views = {"v": 100}
        k.mine._me = lambda ch: {"uploads": "UU1"}
        k.mine._uploads = lambda ch, me, n=50: [{"id": "newvideo000"}]
        k.mine._get = lambda ch, url: {"items": [{"id": "newvideo000", "status": {"privacyStatus": "public"},
            "snippet": {"title": "Chanthree", "publishedAt": (now - dt.timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "statistics": {"viewCount": str(views["v"]), "likeCount": "3", "commentCount": "1"}, "contentDetails": {"duration": "PT8M"}}]}
        r = k.me_live("chantwo")
        v = r["videos"][0]
        self.assertEqual((v["views"], v["hours_since_upload"]), (100, 10.0))
        self.assertEqual(v["views_per_hour_lifetime"], 10.0)
        k._w("update own_snap set ts=ts-3600")    # pretend the last snapshot was an hour ago
        views["v"] = 160
        v = k.me_live("chantwo")["videos"][0]
        self.assertEqual(v["views_per_hour_now"], 60.0)
        self.assertEqual(v["since_last_check"]["new_views"], 60)

    def test_reach_impressions_and_ctr(self):
        k = make_k(self.tmp)
        k.mine.dir = self.dir
        jobs, posted = [], []
        def rpt(ch, path, body=None):
            if path == "jobs" and body is None:
                return {"jobs": jobs}
            if path == "jobs":
                posted.append(body["reportTypeId"])
                jobs.append({"id": "J" + body["reportTypeId"][-8:], "reportTypeId": body["reportTypeId"]})
                return jobs[-1]
            if path.endswith("/reports"):
                kind = "basic" if "basic" in path else "comb"
                return {"reports": [{"id": "R" + kind, "downloadUrl": "https://x/" + kind, "startTime": "2026-11-13T08:00:00Z"}]}
        today = dt.date.today()
        d1, d2 = (today - dt.timedelta(days=2)).strftime("%Y%m%d"), (today - dt.timedelta(days=1)).strftime("%Y%m%d")
        csvs = {"https://x/basic": "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
                                   "%s,UC1,vidA,1000,0.05\n%s,UC1,vidA,3000,0.03\n%s,UC1,vidB,500,0.10\n" % (d1, d2, d2),
                "https://x/comb": "date,channel_id,video_id,traffic_source_type,traffic_source_detail,operating_system,device_type,"
                                  "video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
                                  "%s,UC1,vidA,BROWSE,,IOS,MOBILE,2500,0.04\n%s,UC1,vidA,YT_SEARCH,,IOS,MOBILE,500,0.10\n" % (d2, d2)}
        k._rpt = rpt
        k._rpt_csv = lambda ch, url: csvs[url]
        k.mine._titles = lambda ch, ids: {"vidA": "Drain video", "vidB": "Toilet video"}
        r = k.me_reach("demo")
        self.assertEqual(sorted(posted), ["channel_reach_basic_a1", "channel_reach_combined_a1"])
        a = r["videos"][0]
        self.assertEqual((a["video"], a["impressions"], a["ctr_pct"]), ("vidA", 4000, 3.5))   # (50 + 90) / 4000
        self.assertEqual(r["total_impressions"], 4500)
        self.assertEqual(r["by_traffic_source"][0], {"source": "BROWSE", "impressions": 2500, "ctr_pct": 4.0})
        self.assertEqual(r["by_device"][0]["device"], "MOBILE")
        k.me_reach("demo")                                   # same reports are not stored twice
        self.assertEqual(k._q("select count(*) from reach_rows")[0][0], 5)


    def test_creator_breakdown(self):
        k = make_k(self.tmp)
        k.mine.dir = self.dir
        now = dt.datetime.now(dt.timezone.utc)
        ts = lambda d, h: (now - dt.timedelta(days=d)).replace(hour=h, minute=0).strftime("%Y-%m-%dT%H:%M:%S+0000")
        media = []
        for i in range(12):
            q = i % 3 == 0
            media.append({"media_product_type": "REELS", "like_count": 9000 if q else 1000, "comments_count": 40,
                          "permalink": "https://www.instagram.com/reel/R%d/" % i, "timestamp": ts(i * 2, 14 if q else 3),
                          "caption": ("Why does nobody talk about the Jio pricing war?" if q else "Morning routine. Coffee first.") + " More."})
        k.ig.profile = lambda u: {"username": u, "followers": 200000, "media_count": 400, "media": media}
        k._transcribe_reel = lambda url, seconds=60: (None, "Instagram refused the download (needs a logged-in cookies file)")
        mine = [{"views": 1000, "caption": "Here is my story.", "posted": ts(d, 3), "age_days": float(d)} for d in (1, 5, 9, 13, 17)]
        k.mine.ig_insights = lambda a, days=28, reels=25: {"account": "myaccount", "reels": mine}
        r = k.creator_breakdown("Rajkumari", me="myaccount")
        did = [s["did"] for s in r["steps"]]
        self.assertTrue(did[0].startswith("Opened @rajkumari"))
        self.assertIn("Tried to transcribe it", did)
        self.assertIn("Picked the one reel worth stealing from", did)
        self.assertEqual(did.count("Tried to compare with your account"), 0)
        sb = r["script_brief"]
        self.assertEqual(sb["vs_their_usual"], 9.0)
        self.assertEqual(sb["source"], "caption")
        self.assertTrue(sb["hook"].startswith("Why does nobody"))
        self.assertIn("how_to_write", sb)
        self.assertIn("Compared with your own account @myaccount", did)
        self.assertEqual(r["viral_reels"][0]["likes"], 9000)
        self.assertEqual(r["what_works_for_them"]["best_hook"], "question")
        plan = " ".join(r["what_you_should_do"])
        self.assertIn("question hook", plan)
        self.assertIn("Post more often", plan)

    def test_breakdown_view_renders_for_every_platform(self):
        import scout_showcase
        for plat, site in (("instagram", "Instagram"), ("youtube", "YouTube"), ("x", "X")):
            d = {"creator": "@a</script><b>", "platform": plat, "site": site, "steps": [], "reels_read": 3, "distribution": [1, 2, 9]}
            page = scout_showcase.render(d, cta="someone")
            self.assertNotIn("@@", page)
            self.assertNotIn("</script><b>", page)
            self.assertIn("Follow @someone", page)
            self.assertIn("<title>@a&lt;/script&gt;&lt;b&gt; on %s</title>" % site, page)
        app = scout_showcase.app_page()
        self.assertIn('id="img"', app)            # share image button
        self.assertIn("function shareImage", app)
        self.assertNotIn("@@", app)
        self.assertIn("const D0 = null", app)

    def test_watchlist_whats_new(self):
        k = make_k(self.tmp)
        now = dt.datetime.now(dt.timezone.utc)
        ts = lambda h: (now - dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        # old posts set the usual (~1000 views); two new ones: a real hit and a paid boost
        old = [{"media_product_type": "REELS", "view_count": 1000 + i * 10, "like_count": 50, "permalink": "old%d" % i,
                "timestamp": ts(24 * 9 + i), "caption": "Old one %d" % i} for i in range(8)]
        new = [{"media_product_type": "REELS", "view_count": 9000, "like_count": 450, "permalink": "hit", "timestamp": ts(5), "caption": "A real hit"},
               {"media_product_type": "REELS", "view_count": 20000, "like_count": 20, "permalink": "ad", "timestamp": ts(3), "caption": "Buy my course"},
               {"media_product_type": "REELS", "view_count": 1100, "like_count": 55, "permalink": "meh", "timestamp": ts(2), "caption": "Meh"}]
        k.ig.profile = lambda u: {"username": u, "followers": 10000, "media": old + new}
        with self.assertRaises(sc.ScoutError):
            k.whats_new()
        self.assertEqual(k.watchlist_add("@SomeOne")["count"], 1)
        k.watchlist_add("someone")                              # no duplicates
        self.assertEqual(k.watchlist()["count"], 1)
        r = k.whats_new()
        self.assertEqual([h["url"] for h in r["new_hits"]], ["hit", "ad"])   # boost listed last
        self.assertTrue(r["new_hits"][1]["boosted"])
        self.assertEqual(r["new_hits"][0]["metric"], "views")
        self.assertEqual(k.whats_new()["new_hits"], [])           # nothing new since the last check
        k.watchlist_remove("someone")
        self.assertEqual(k.watchlist()["count"], 0)

    def test_my_reel_check(self):
        k = make_k(self.tmp)
        now = dt.datetime.now(dt.timezone.utc)
        ts = lambda d, h=13: (now - dt.timedelta(days=d)).replace(hour=h, minute=0).strftime("%Y-%m-%dT%H:%M:%S+0000")   # 13 UTC = evening IST
        mine = [{"url": "https://www.instagram.com/reel/U%d/" % i, "caption": "5 things I learned building this", "posted": ts(3 + i * 3),
                 "views": 10000, "shares": 100, "saved": 50, "comments": 20, "avg_watch_seconds": 9.0, "age_days": 3 + i * 3} for i in range(6)]
        mine.append({"url": "https://www.instagram.com/reel/LATEST/", "caption": "Why does nobody talk about this?", "posted": ts(1.5, 3),
                     "views": 4000, "shares": 10, "saved": 20, "comments": 8, "avg_watch_seconds": 4.0, "age_days": 1.5})
        k.mine.ig_insights = lambda a, days=28, reels=25: {"account": a, "reels": mine}
        r = k.my_reel_check(me="me")
        self.assertEqual(r["reel"], "https://www.instagram.com/reel/LATEST/")
        self.assertEqual(r["verdict"], "flopped")
        self.assertEqual(r["vs_usual"], 0.4)
        sig = {s["what"]: s for s in r["signals"]}
        self.assertEqual(sig["average watch time"]["verdict"], "worse")
        self.assertEqual(sig["shares per view"]["verdict"], "worse")
        self.assertEqual(r["your_best_hook"], "number")
        self.assertTrue(r["what_to_do"][0].startswith("People left early"))
        self.assertTrue(any("evening 17-22" in f for f in r["what_to_do"]))
        self.assertIsNotNone(r["note"])                             # under 2 days old
        # comments collapse while everything else is normal: the fix must be about comments
        mine2 = [dict(m, caption="Comment GUIDE and I'll send you the list", comments=500) for m in mine[:-1]]
        mine2.append(dict(mine[-1], caption="I built my own tool", shares=40, avg_watch_seconds=8.5, saved=20, comments=20))
        k.cache.put("myinsfresh:me", {"account": "me", "reels": mine2}, 600)
        r3 = k.my_reel_check(me="me")
        self.assertIn("Most of your reels ask people to comment", r3["what_to_do"][0])
        k.cache.put("myinsfresh:me", {"account": "me", "reels": mine}, 600)
        r2 = k.my_reel_check(reel="https://www.instagram.com/reel/U0/", me="me")
        self.assertEqual(r2["verdict"], "did about normal")
        mine[-1]["views"] = 30000
        k.cache.put("myinsfresh:me", {"account": "me", "reels": mine}, 600)
        self.assertEqual(k.my_reel_check(me="me")["verdict"], "popped")

    def test_setup_status_guides_new_users(self):
        k = make_k(self.tmp)
        k.mine.dir = self.dir
        k.cfg.yt_keys = []
        k.ig.pool = lambda: []
        s = k.setup_status()
        st = {i["name"]: i for i in s["items"]}
        self.assertFalse(s["ready"])
        self.assertEqual(s["next"], "YouTube research")
        self.assertEqual(st["YouTube research"]["status"], "missing")
        self.assertTrue(any("console.cloud.google.com" in x for x in st["YouTube research"]["how_to_fix"]))
        self.assertEqual(st["Instagram research"]["status"], "missing")
        self.assertIn("Free", s["cost"])
        self.assertNotIn("credit", " ".join(" ".join(i["how_to_fix"]) for i in s["items"]).lower())
        # now give it working keys: YouTube answers, Instagram answers
        k.cfg.yt_keys = ["K"]
        k.yt.api = lambda endpoint, units, **p: {"items": [{"id": "x"}]}
        k.ig.pool = lambda: [{"label": "a", "token": "t", "ig_user_id": "1"}]
        k.ig.profile = lambda u: {"username": u, "followers": 600000000, "media": []}
        k.my_profile_set(instagram="me", niche="finance", audience="students")
        s = k.setup_status()
        st = {i["name"]: i for i in s["items"]}
        self.assertTrue(s["ready"])
        self.assertEqual(st["YouTube research"]["status"], "working")
        self.assertIn("600,000,000", st["Instagram research"]["detail"])
        self.assertEqual(st["About you"]["status"], "working")
        # a rejected key is reported as broken with a fix
        def bad(endpoint, units, **p):
            raise sc.ScoutError("API key not valid", 400)
        k.yt.api = bad
        st = {i["name"]: i for i in k.setup_status()["items"]}
        self.assertEqual(st["YouTube research"]["status"], "broken")
        self.assertTrue(st["YouTube research"]["how_to_fix"])

    def test_profile_setup_and_views_ranking(self):
        k = make_k(self.tmp)
        k.mine.dir = self.dir
        self.assertFalse(k.my_profile()["set_up"])
        self.assertEqual(sorted(k.my_profile()["missing"]), ["audience", "instagram", "niche"])
        p = k.my_profile_set(instagram="@mypage", niche="personal finance for students", audience="college students in India")
        self.assertTrue(p["set_up"])
        self.assertEqual(p["profile"]["instagram"], "mypage")
        k.my_profile_set(goal="followers")
        self.assertEqual(k.my_profile()["profile"]["niche"], "personal finance for students")   # partial update keeps the rest
        now = dt.datetime.now(dt.timezone.utc)
        ts = lambda d: (now - dt.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        caps = ["Dance trend with my cat."] + ["How students can save money on rent and finance."] + ["Morning coffee."] * 8
        likes = [20000, 15000] + [500] * 8
        views = [200000, 900000] + [30000] * 8      # by likes the cat reel wins, by real views the finance one does
        media = [{"media_product_type": "REELS", "like_count": l, "view_count": v, "comments_count": 5, "permalink": "R%d" % i,
                  "timestamp": ts(i * 2), "caption": c} for i, (l, v, c) in enumerate(zip(likes, views, caps))]
        k.ig.profile = lambda u: {"username": u, "followers": 50000, "media_count": 90, "media": media}
        k._transcribe_reel = lambda url, seconds=60: (None, "off")
        seen = {}
        k.mine.ig_insights = lambda a, days=28, reels=25: seen.setdefault("me", a) and {"account": a, "reels": []}
        r = k.creator_breakdown("someone")
        self.assertEqual(seen["me"], "mypage")                     # compared with the saved page
        self.assertEqual(r["metric"], "views")
        self.assertEqual(r["viral_reels"][0]["likes"], 900000)
        self.assertEqual(r["script_brief"]["reel"], "R1")
        self.assertIn("students", r["script_brief"]["shared_topics"])
        self.assertEqual(r["script_brief"]["for_me"]["audience"], "college students in India")
        self.assertIsNone(r["setup_needed"])

    def test_boosted_reels_are_flagged_and_not_copied(self):
        k = make_k(self.tmp)
        now = dt.datetime.now(dt.timezone.utc)
        ts = lambda d: (now - dt.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        # an ad: 1.7M views, 111 likes. An organic hit: 700K views, 9K likes. The rest: ~180K views, ~1.5K likes.
        rows = [(1700000, 111, "Join my free bootcamp."), (700000, 9000, "What happens after you clear the exam?")] + \
               [(180000 + i * 1000, 1500, "Normal reel %d." % i) for i in range(8)]
        media = [{"media_product_type": "REELS", "view_count": v, "like_count": l, "comments_count": 5, "permalink": "R%d" % i,
                  "timestamp": ts(i * 2), "caption": c} for i, (v, l, c) in enumerate(rows)]
        k.ig.profile = lambda u: {"username": u, "followers": 4000000, "media_count": 90, "media": media}
        k._transcribe_reel = lambda url, seconds=60: (None, "off")
        k.mine.ig_insights = lambda a, days=28, reels=25: {"account": a, "reels": []}
        r = k.creator_breakdown("someone", me="me")
        self.assertTrue(r["viral_reels"][0]["boosted"])
        self.assertEqual(r["viral_reels"][0]["real_likes"], 111)
        self.assertFalse(r["viral_reels"][1]["boosted"])
        self.assertEqual(r["script_brief"]["reel"], "R1")          # the organic hit is the one to copy
        self.assertIn("Spotted 1 likely paid boost", [s["did"] for s in r["steps"]])

    def test_youtube_breakdown_uses_views_and_captions(self):
        k = make_k(self.tmp)
        k.mine.dir = self.dir
        now = dt.datetime.now(dt.timezone.utc)
        iso = lambda d: (now - dt.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
        views = [900000, 40000, 50000, 45000, 30000, 60000, 52000, 41000]
        ups = [{"id": "abcdefghij%d" % i, "views": v, "comments": 10, "title": "Title number %d about money" % i, "published": iso(i * 3),
                "url": "https://www.youtube.com/watch?v=abcdefghij%d" % i, "is_short": True} for i, v in enumerate(views)]
        k.yt.channel_info = lambda ref: {"id": "UCx", "title": "Demo Channel", "handle": "@demochan", "subscribers": 120000, "video_count": 300}
        k.yt.recent = lambda info, kind, n: ups
        k.yt.transcript = lambda vid, lang="en": {"segments": [{"start": 0, "text": "Nobody tells you this about money"},
                                                              {"start": 3, "text": "here is the trick"}, {"start": 200, "text": "late part"}]}
        r = k.creator_breakdown("@demochan", platform="yt")
        self.assertEqual((r["platform"], r["site"], r["metric"]), ("youtube", "YouTube", "views"))
        self.assertEqual(r["creator"], "@demochan")
        self.assertEqual(r["viral_reels"][0]["likes"], 900000)
        sb = r["script_brief"]
        self.assertEqual(sb["source"], "transcript")
        self.assertEqual(sb["hook"], "Nobody tells you this about money.")
        self.assertNotIn("late part", sb["text"])
        self.assertIn("Opened @demochan on YouTube", [s["did"] for s in r["steps"]])

    def test_x_embed_parse_and_breakdown(self):
        tw = lambda i, likes, text, user="someone": {"type": "tweet", "content": {"tweet": {
            "id_str": str(100 + i), "full_text": text + " https://t.co/abc", "favorite_count": likes, "retweet_count": 3, "reply_count": 2,
            "created_at": "Wed Oct 1%d 20:19:24 +0000 2026" % (i % 10), "user": {"screen_name": user, "followers_count": 5000, "statuses_count": 900}}}}
        entries = [tw(i, l, "Post %d about startups." % i) for i, l in enumerate([50, 60, 2000, 40, 55, 70])]
        entries.append(tw(9, 99999, "RT @other: not theirs"))
        entries.append(tw(10, 99999, "someone else", user="other"))
        html = '<html><script id="__NEXT_DATA__" type="application/json">%s</script></html>' % json.dumps(
            {"props": {"pageProps": {"timeline": {"entries": entries}}}})
        d = scout_social.parse_x_embed(html, "someone")
        self.assertEqual(len(d["posts"]), 6)
        self.assertEqual(d["followers"], 5000)
        self.assertEqual(d["posts"][2]["text"], "Post 2 about startups.")
        self.assertTrue(d["posts"][0]["created"].startswith("2026-10-10"))
        self.assertIsNone(scout_social.parse_x_embed("<html>login wall</html>", "someone"))
        k = make_k(self.tmp)
        k.x.user_posts = lambda u, n=50: d
        r = k.creator_breakdown("someone", platform="twitter")
        self.assertEqual(r["platform"], "x")
        self.assertEqual(r["script_brief"]["source"], "post")
        self.assertIn("ONE post", r["script_brief"]["how_to_write"])
        self.assertEqual(r["viral_reels"][0]["url"], "https://x.com/someone/status/102")

    def test_reel_transcripts_are_cached_and_split_into_lines(self):
        k = make_k(self.tmp)
        calls = []
        old = (os.environ.get("SCOUT_TRANSCRIBE"), scout_growth._WMODEL[:])
        os.environ["SCOUT_TRANSCRIBE"] = "1"

        class Seg:
            def __init__(self, t):
                self.text = t

        class Model:
            def transcribe(self, path, **kw):
                calls.append(kw)
                return [Seg(" These are three places"), Seg("you must visit"), Seg("Trust me!")], None
        scout_growth._WMODEL[:] = [("faster", Model())]
        try:
            import shutil, subprocess

            def fake_run(cmd, **kw):
                out = cmd[cmd.index("-o") + 1].replace("%(ext)s", "m4a")
                open(out, "w").close()
                return subprocess.CompletedProcess(cmd, 0, "", "")
            with mock.patch("shutil.which", return_value="/usr/bin/yt-dlp"), mock.patch("subprocess.run", side_effect=fake_run):
                a = k._transcribe_reel("https://www.instagram.com/reel/ABC/?igsh=x")
                b = k._transcribe_reel("https://www.instagram.com/reel/ABC/")
        finally:
            scout_growth._WMODEL[:] = old[1]
            os.environ.pop("SCOUT_TRANSCRIBE") if old[0] is None else os.environ.__setitem__("SCOUT_TRANSCRIBE", old[0])
        self.assertEqual(a, ("These are three places. you must visit. Trust me!", None))
        self.assertEqual(b, a)
        self.assertEqual(len(calls), 1)

    def test_breakdown_transcribes_the_viral_reel_closest_to_me(self):
        k = make_k(self.tmp)
        k.mine.dir = self.dir
        now = dt.datetime.now(dt.timezone.utc)
        ts = lambda d: (now - dt.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        caps = ["Celebrity gossip you missed this week."] * 1 + ["Why most founders fail at raising money from investors."] + ["Morning coffee."] * 8
        likes = [10000, 8000] + [1000] * 8
        media = [{"media_product_type": "REELS", "like_count": l, "comments_count": 5, "permalink": "R%d" % i,
                  "timestamp": ts(i * 2), "caption": c} for i, (l, c) in enumerate(zip(likes, caps))]
        k.ig.profile = lambda u: {"username": u, "followers": 50000, "media_count": 90, "media": media}
        calls = []
        k._transcribe_reel = lambda url, seconds=60: calls.append((url, seconds)) or (
            "Most founders raise too early. Here is why. Investors want proof. So get ten paying customers first. Follow for more.", None)
        mine = [{"views": 900, "caption": c, "posted": ts(d), "age_days": float(d)}
                for d, c in ((1, "How founders pitch investors"), (4, "Raising money as a student founder"), (8, "My founders story"))]
        k.mine.ig_insights = lambda a, days=28, reels=25: {"account": "myaccount", "reels": mine}
        r = k.creator_breakdown("someone", me="myaccount")
        self.assertEqual(calls, [("R1", 90)])
        sb = r["script_brief"]
        self.assertEqual(sb["reel"], "R1")
        self.assertEqual(sb["source"], "transcript")
        self.assertIn("founders", sb["shared_topics"])
        self.assertEqual(sb["hook"], "Most founders raise too early.")
        self.assertEqual(sb["payoff"], "Follow for more.")
        self.assertEqual(len(sb["beats"]), 3)

    def test_ig_insights(self):
        r = self.s.me_ig("demo.channel")
        self.clean(r)
        self.assertEqual(r["followers"], 5200)
        self.assertEqual(r["account_insights_28d"]["reach"], 1234)
        self.assertEqual(r["follower_demographics"]["country"][0]["key"], "US")
        self.assertEqual(r["top_reels"][0]["views"], 7000)
        self.assertEqual(r["reels_summary"]["median_avg_watch_seconds"], 6.4)
        self.assertTrue(any("profile_links_taps" in e for e in r["errors"]))

    def test_unknown_channel_and_missing_dir(self):
        with self.assertRaises(sc.ScoutError):
            self.s.me_race("nobody")
        self.s.mine.dir = os.path.join(self.tmp, "nope")
        self.assertEqual(self.s.me_channels()["channels"], [])

    def test_error_redacts_tokens(self):
        e = sc.ScoutError("x")
        msg = re.sub(r"(access_token|refresh_token)=[^&\s]+", r"\1=***", "bad url ?access_token=ABC&x=1")
        self.assertNotIn("ABC", msg)

    def test_daily_job_checks_milestones(self):
        self.s.me_achievements("demo")
        self.fake.subs = 1500
        out = self.s.run_daily()
        self.assertEqual(out["my_channels"][0]["new_milestones"][0]["milestones"], [1000])


# ---------------------------------------------------------------- X and Facebook (scout_social)

import scout_social as ss


class FakeSocial:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers=None, timeout=30):
        self.calls.append(url)
        now = dt.datetime.now(dt.timezone.utc)
        iso = lambda d: (now - dt.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if "syndication" in url:
            pid = url.split("id=")[1]
            if pid == "404404":
                return {"__typename": "TweetTombstone"}
            return {"__typename": "Tweet", "id_str": pid, "text": "hello", "favorite_count": 120, "conversation_count": 9,
                    "created_at": iso(2), "user": {"screen_name": "demo", "name": "Demo"}, "video": {"viewCount": "5400", "poster": "p.jpg"}}
        if "api.x.com" in url:
            assert headers["Authorization"] == "Bearer XBEARER"
            if "users/by/username/" in url:
                return {"data": {"id": "42", "username": "demo", "name": "Demo", "public_metrics": {"followers_count": 1000, "tweet_count": 500}}}
            if "users/42/tweets" in url:
                data = [{"id": str(i), "text": "post %d" % i, "created_at": iso(2 + i), "public_metrics": {"impression_count": 1000, "like_count": 10}} for i in range(10)]
                data.append({"id": "99", "text": "big one", "created_at": iso(3), "public_metrics": {"impression_count": 9000, "like_count": 300}})
                return {"data": data}
            if "search/recent" in url:
                return {"data": [{"id": "7", "text": "q", "author_id": "1", "created_at": iso(1), "public_metrics": {"impression_count": 50000}},
                                 {"id": "8", "text": "q2", "author_id": "2", "created_at": iso(1), "public_metrics": {"impression_count": 900}}],
                        "includes": {"users": [{"id": "1", "username": "small", "public_metrics": {"followers_count": 500}},
                                               {"id": "2", "username": "big", "public_metrics": {"followers_count": 90000}}]}}
        if "graph.facebook.com" in url:
            assert "access_token=" in url
            path = urllib.parse.urlparse(url).path
            if path.endswith("/me/accounts"):
                return {"data": [{"id": "555", "name": "Nichethree", "access_token": "PAGETOKEN-SECRET", "followers_count": 40000, "fan_count": 38000}]}
            assert "PAGETOKEN-SECRET" in url
            if path.endswith("/555/insights"):
                if "page_daily_follows_unique" in url:
                    raise sc.ScoutError("400 metric deprecated", 401)
                return {"data": [{"values": [{"value": 10}, {"value": 20}]}]}
            if path.endswith("/555/posts"):
                return {"data": [{"permalink_url": "https://fb/p%d" % i, "message": "p", "created_time": iso(i + 1).replace(".000Z", "+0000"),
                                  "reactions": {"summary": {"total_count": 100 if i else 900}}, "comments": {"summary": {"total_count": 0}},
                                  "shares": {"count": 0}} for i in range(8)]}
            if path.endswith("/555/video_reels"):
                return {"data": [{"id": "r%d" % i, "permalink_url": "/reel/%d" % i, "description": "r", "created_time": iso(i + 1).replace(".000Z", "+0000"),
                                  "length": 30} for i in range(6)]}
            if "/r" in path and path.endswith("/video_insights"):
                i = int(path.split("/r")[-1].split("/")[0])
                return {"data": [{"name": "blue_reels_play_count", "values": [{"value": 20000 if i == 0 else 3000}]},
                                 {"name": "post_impressions_unique", "values": [{"value": 1500}]},
                                 {"name": "post_video_avg_time_watched", "values": [{"value": 7200}]}]}
        raise AssertionError("unexpected url " + url)


class SocialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        pool = os.path.join(self.tmp, "pool.json")
        with open(pool, "w") as f:
            json.dump([{"label": "a", "token": "SYS-SECRET", "ig_user_id": "1"}], f)
        self.s = sp.ScoutPlus(sc.Config(db=os.path.join(self.tmp, "c.sqlite"), yt_key="", ig_pool=pool))
        self.fake = FakeSocial()
        self.orig, ss._http = ss._http, self.fake

    def tearDown(self):
        ss._http = self.orig

    def test_x_post_free(self):
        p = self.s.x_post("https://x.com/demo/status/123456789")
        self.assertEqual(p["likes"], 120)
        self.assertEqual(p["video_views"], 5400)
        self.assertEqual(p["author"], "@demo")
        r = self.s.x_posts(["123456789", "404404", "not a url"])
        self.assertEqual(len(r["posts"]), 1)
        self.assertEqual(len(r["errors"]), 2)

    def test_x_api_needs_key(self):
        with self.assertRaises(sc.ScoutError) as e:
            self.s.x_outliers(["demo"])
        self.assertEqual(e.exception.status, 501)

    def test_x_outliers_and_cap(self):
        self.s.x.bearer = "XBEARER"
        r = self.s.x_outliers(["@demo"], min_multiple=3)
        self.assertEqual(r["results"][0]["id"], "99")
        self.assertEqual(r["results"][0]["multiple"], 9.0)
        self.assertEqual(r["results"][0]["scored_on"], "views")
        u = self.s.usage()
        self.assertEqual(u["x_posts_read_today"], 11)
        self.assertGreater(u["x_estimated_cost_today_usd"], 0)
        self.s.x.daily_posts = 20
        with self.assertRaises(sc.ScoutError) as e:
            self.s.x._api("users/42/tweets", posts_expected=50)
        self.assertEqual(e.exception.status, 429)

    def test_x_search_ranks_by_reach(self):
        self.s.x.bearer = "XBEARER"
        r = self.s.x_search("drain hack")
        self.assertEqual(r["results"][0]["author"], "@small")
        self.assertEqual(r["results"][0]["views_per_follower"], 100.0)
        self.assertTrue(any("is%3Aretweet" in c for c in self.fake.calls if "search" in c))

    def test_fb_page(self):
        self.assertEqual(self.s.me_fb_pages()["pages"][0]["name"], "Nichethree")
        r = self.s.me_fb("nichethree")
        txt = json.dumps(r)
        self.assertNotIn("PAGETOKEN-SECRET", txt)
        self.assertNotIn("SYS-SECRET", txt)
        self.assertEqual(r["insights_28d"]["page_media_view"], 30)
        self.assertTrue(any("page_daily_follows_unique" in e for e in r["errors"]))
        self.assertEqual(r["post_outliers"][0]["multiple"], 9.0)
        self.assertEqual(r["reel_outliers"][0]["plays"], 20000)
        self.assertEqual(r["top_reels"][0]["avg_watch_seconds"], 7.2)
        self.assertTrue(r["top_reels"][0]["url"].startswith("https://www.facebook.com/reel/"))
        with self.assertRaises(sc.ScoutError):
            self.s.me_fb("nope")



class RemoteMcpTests(ServerV2):
    SECRET = "m" * 30

    @classmethod
    def setUpClass(cls):
        cls.old_allow_shared = os.environ.get("SCOUT_ALLOW_SHARED_MCP")
        os.environ["SCOUT_ALLOW_SHARED_MCP"] = "1"  # explicit opt-in for legacy compatibility tests only
        cls.tmp = tempfile.mkdtemp()
        cls.scout = make_plus(cls.tmp, FakeYT2(drain_uploads()))
        cls.srv = scout_server.make_server(cls.scout, cls.KEY, "127.0.0.1", 0, cls.SECRET)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        if cls.old_allow_shared is None:
            os.environ.pop("SCOUT_ALLOW_SHARED_MCP", None)
        else:
            os.environ["SCOUT_ALLOW_SHARED_MCP"] = cls.old_allow_shared

    def rpc(self, msg, secret=None):
        r = urllib.request.Request("http://127.0.0.1:%d/mcp/%s" % (self.port, secret or self.SECRET),
                                   data=json.dumps(msg).encode(), method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                b = resp.read()
                return resp.status, (json.loads(b) if b else None)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_stable_gateway_tools(self):
        c, r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertTrue(r["result"]["capabilities"]["tools"]["listChanged"])
        names = [t["name"] for t in self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]]
        self.assertIn("kestrel_tools", names)
        self.assertIn("kestrel_call", names)
        c, r = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kestrel_tools", "arguments": {"search": "live"}}})
        found = [t["name"] for t in json.loads(r["result"]["content"][0]["text"])["tools"]]
        self.assertIn("scout_my_live", found)
        c, r = self.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "kestrel_call", "arguments": {"tool": "scout_usage", "args": {}}}})
        self.assertFalse(r["result"]["isError"])
        self.assertIn("youtube_units_today", r["result"]["content"][0]["text"])
        c, r = self.rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                         "params": {"name": "kestrel_call", "arguments": {"tool": "nope"}}})
        self.assertTrue(r["result"]["isError"])

    def test_breakdown_renders_as_mcp_app(self):
        c, r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertIn("resources", r["result"]["capabilities"])
        tools = {t["name"]: t for t in self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]}
        uri = tools["scout_creator_breakdown"]["_meta"]["ui"]["resourceUri"]
        self.assertEqual(tools["scout_creator_breakdown"]["_meta"]["openai/outputTemplate"], uri)   # ChatGPT alias
        self.assertTrue(uri.startswith("ui://"))
        self.assertNotIn("_meta", tools["scout_usage"])
        res = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})[1]["result"]["resources"]
        self.assertEqual(res[0]["uri"], uri)
        page = self.rpc({"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": uri}})[1]["result"]["contents"][0]
        self.assertEqual(page["mimeType"], "text/html;profile=mcp-app")
        self.assertIn("ui/notifications/tool-result", page["text"])
        self.assertIn("const D0 = null", page["text"])
        self.assertIn("ui/notifications/size-changed", page["text"])
        self.assertNotIn("googleapis", page["text"])  # no external requests: renders instantly inside the chat
        self.assertEqual(page["_meta"]["ui"]["csp"]["resourceDomains"], [])
        seen = {}
        self.scout.creator_breakdown = lambda user, me=None, **k: seen.update(user=user, me=me) or {"creator": "@" + user, "steps": []}
        old_me = os.environ.get("SCOUT_SHOWCASE_ME")
        os.environ["SCOUT_SHOWCASE_ME"] = "myaccount"
        try:
            r = self.rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                          "params": {"name": "scout_creator_breakdown", "arguments": {"user": "@rajshamani"}}})[1]["result"]
        finally:
            del self.scout.creator_breakdown
            os.environ.pop("SCOUT_SHOWCASE_ME") if old_me is None else os.environ.__setitem__("SCOUT_SHOWCASE_ME", old_me)
        self.assertEqual(seen, {"user": "rajshamani", "me": None})   # the breakdown itself falls back to the saved profile
        self.assertEqual(r["structuredContent"]["creator"], "@rajshamani")
        self.assertEqual(r["_meta"]["ui"]["resourceUri"], uri)
        self.assertFalse(r["isError"])

    def test_event_stream_says_list_changed(self):
        old = scout_server.SSE_TICKS
        scout_server.SSE_TICKS = 0
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/mcp/%s" % (self.port, self.SECRET), headers={"Accept": "text/event-stream"})
            body = urllib.request.urlopen(req, timeout=5).read().decode()
        finally:
            scout_server.SSE_TICKS = old
        self.assertIn("notifications/tools/list_changed", body)

    def test_tools_endpoint(self):
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen("http://127.0.0.1:%d/v1/tools" % self.port)
        req = urllib.request.Request("http://127.0.0.1:%d/v1/tools" % self.port, headers={"X-Scout-Key": self.KEY})
        tools = json.loads(urllib.request.urlopen(req).read())["tools"]
        live = next(t for t in tools if t["name"] == "scout_my_live")
        self.assertEqual((live["route"], live["method"]), ("/v1/me/live", "GET"))

    def test_stdio_wrapper_serves_the_servers_list(self):
        import subprocess
        env = dict(os.environ, SCOUT_URL="http://127.0.0.1:%d" % self.port, SCOUT_API_KEY=self.KEY)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "kestrel_call",
                                                                               "arguments": {"tool": "scout_usage", "args": {}}}}]
        p = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scout_mcp.py")],
                           input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True, env=env, timeout=60)
        out = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        self.assertTrue(out[0]["result"]["capabilities"]["tools"]["listChanged"])
        self.assertIn("kestrel_call", [t["name"] for t in out[1]["result"]["tools"]])
        self.assertIn("youtube_units_today", out[2]["result"]["content"][0]["text"])

    def test_remote_mcp(self):
        c, r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual((c, r["result"]["protocolVersion"]), (200, "2025-06-18"))
        c, r = self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((c, r), (202, None))
        c, r = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertGreaterEqual(len(r["result"]["tools"]), 50)
        c, r = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "scout_usage", "arguments": {}}})
        self.assertFalse(r["result"]["isError"])
        c, r = self.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "scout_video", "arguments": {}}})
        self.assertTrue(r["result"]["isError"])
        c, _ = self.rpc({"jsonrpc": "2.0", "id": 5, "method": "ping"}, secret="wrong" * 6)
        self.assertEqual(c, 404)

    def test_breakdown_page_survives_placeholder_text_in_captions(self):
        import scout_showcase
        evil = {"platform": "instagram", "name": "x", "viral_reels": [{"hook": "nice @@CTA_JS@@ @@SITE@@ reel", "caption": "@@CREDIT@@"}]}
        page = scout_showcase.render(evil, cta="me")
        self.assertIn("nice @@CTA_JS@@ @@SITE@@ reel", page)

    def test_access_log_hides_mcp_secret(self):
        import io, contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.rpc({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertNotIn(self.SECRET, err.getvalue())
        self.assertIn("<secret>", err.getvalue())


import scout_extras as sx


def make_k(tmp, yt=None):
    k = sx.Kestrel(sc.Config(db=os.path.join(tmp, "c.sqlite"), yt_key="", ig_pool=""))
    k.yt = yt or FakeYT2(drain_uploads())
    return k


def ig_item(url, acct, likes, median, followers, cap="A caption here. More text", age=1.0):
    return {"url": url, "account": acct, "followers": followers, "caption": cap, "likes": likes, "comments": 3,
            "account_median_likes": median, "multiple": round(likes / median, 1), "age_days": age, "posted": "2026-11-10T10:00:00+0000"}


class ExtrasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.k = make_k(self.tmp)
        day = self.k._today()
        self.k.niche_add("nichefive", ig_users=["a1"])
        self.k.niche_add("nichetwo", ig_users=["a1"])
        rows = [("nichefive", ig_item("u1", "@small", 40000, 50, 1000000, "Leaders create certainty. Rest")),
                ("nichetwo", ig_item("u2", "@tiny", 5000, 50, 5000, "Why nobody talks about this?")),
                ("nichefive", ig_item("u2", "@tiny", 5000, 50, 5000, "Why nobody talks about this?"))]
        for n, r in rows:
            self.k._w("insert into feed values(?,?,?,?,?,?,?)", (day, n, r["url"], "instagram", r["multiple"], 1, json.dumps(r)))

    def test_ranked_dedupes_and_floors(self):
        r = self.k.ranked()
        self.assertEqual(r["duplicates_merged"], 1)
        top, small = r["items"]
        self.assertEqual((top["url"], top["adj_multiple"]), ("u1", 40.0))      # 40000 / max(50, 0.1% of 1M) = 40x
        self.assertEqual((small["url"], small["adj_multiple"]), ("u2", 16.7))  # 5000 / 300 floor, not / 50
        self.assertEqual(sorted(small["niches"]), ["nichefive", "nichetwo"])

    def test_cap_orders_by_likes(self):
        self.assertEqual(sx.adjusted({"account_median_likes": 77, "likes": 428686, "followers": 7356}), 50.0)
        rows = [{"adj_multiple": 50.0, "likes": 10}, {"adj_multiple": 50.0, "likes": 900}]
        self.assertEqual(sorted(rows, key=sx.rank_key)[0]["likes"], 900)

    def test_brief_and_trello_push(self):
        b = self.k.brief(per_page=5)
        urls = [c["url"] for p in b["pages"] for c in p["cards"]]
        self.assertEqual(sorted(urls), ["u1", "u2"])   # u2 sits in two niches but becomes one card
        self.assertTrue(all(c["name"].startswith("[%s]" % p["page"]) for p in b["pages"] for c in p["cards"]))
        sent = []
        self.k._post = lambda url, payload: sent.append(url) or {"id": "c1"}
        os.environ.update(SCOUT_TRELLO_KEY="K", SCOUT_TRELLO_TOKEN="T", SCOUT_TRELLO_LISTS=json.dumps({"nichefive": "L1", "nichetwo": "L1"}))
        try:
            out = self.k.brief(push=True)
            self.assertEqual(out["pushed"], 2)
            self.assertTrue(all("idList=L1" in u for u in sent))
            self.assertEqual(self.k.brief(push=True)["pushed"], 0)  # never sends the same card twice
        finally:
            for v in ("SCOUT_TRELLO_KEY", "SCOUT_TRELLO_TOKEN", "SCOUT_TRELLO_LISTS"):
                os.environ.pop(v, None)

    def test_hooks(self):
        self.assertEqual(sx.hook_type("Why nobody talks about this?"), "question")
        self.assertEqual(sx.hook_type("🚨 BREAKING NEWS FROM DELHI TODAY"), "breaking")
        self.assertEqual(sx.hook_type("In 2009 a teenager from the Nilgiris"), "story")
        self.assertEqual(self.k.extract_hooks()["extracted"], 2)
        h = self.k.hooks()
        self.assertEqual(h["total"], 2)
        self.assertIn("Why nobody talks about this?", [x["hook"] for x in h["hooks"]])

    def test_watch_flags_early_reel(self):
        now = dt.datetime.now(dt.timezone.utc)
        ts = lambda hrs: (now - dt.timedelta(hours=hrs)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        media = [{"media_product_type": "REELS", "like_count": 100, "permalink": "old%d" % i, "timestamp": ts(100 + i)} for i in range(8)]
        media.append({"media_product_type": "REELS", "like_count": 5000, "permalink": "fresh", "timestamp": ts(5), "caption": "Big one."})
        self.k.ig._fetch = lambda u: {"username": u, "followers": 10000, "media": media}
        got = []
        self.k._post = lambda url, payload: got.append(payload)
        os.environ["SCOUT_ALERT_WEBHOOK"] = "http://hook"
        try:
            r = self.k.watch()
        finally:
            os.environ.pop("SCOUT_ALERT_WEBHOOK")
        self.assertEqual([a["url"] for a in r["new_alerts"]], ["fresh"])
        self.assertTrue(r["notified"])
        self.assertEqual(self.k.watch()["new_alerts"], [])  # alerted once only
        self.assertEqual(len(self.k.early_alerts()["alerts"]), 1)

    def test_stories_cluster_across_platforms(self):
        posts = [{"url": "https://x.com/a/status/1", "author": "a", "text": "Jensen Huang resigns from Nvidia board after Rubin chip delay", "likes": 9000},
                 {"url": "https://x.com/b/status/2", "author": "b", "text": "Wow Jensen Huang leaving Nvidia board, Rubin delay blamed", "likes": 500}]
        self.k.bots.ingest("x", posts)
        self.k.bots.ingest("reddit", [{"url": "https://reddit.com/r/nvidia/1", "author": "c", "community": "nvidia", "text": "Jensen Huang steps down from Nvidia board", "likes": 70}])
        self.k.bots.ingest("linkedin", [{"url": "https://linkedin.com/posts/1", "author": "d", "text": "My thoughts on hiring interns this summer", "likes": 40}])
        st = self.k.stories(hours=24)["stories"]
        self.assertEqual(len(st), 1)
        self.assertEqual(st[0]["platforms"], ["reddit", "x"])
        self.assertEqual(st[0]["posts"], 3)

    def test_postmortem(self):
        now = dt.datetime.now(dt.timezone.utc)
        rows = []
        for i in range(8):
            v = 10000 if i else 50000
            rows.append({"url": "r%d" % i, "caption": "Why this works?" if i == 0 else "Plain caption.", "views": v,
                         "shares": 900 if i == 0 else 50, "saved": 40, "avg_watch_seconds": 12.0,
                         "posted": (now - dt.timedelta(days=3 + i)).strftime("%Y-%m-%dT%H:%M:%S+0000"), "age_days": 3.0 + i})
        self.k.mine.ig_insights = lambda account, days=28, reels=25: {"account": "nichethree", "label": "a", "reels": rows}
        pm = self.k.postmortem("nichethree", days=14)
        self.assertEqual(pm["median_views"], 10000)
        self.assertEqual(pm["reels"][0]["verdict"], "hit")
        self.assertTrue(any("shared a lot" in w for w in pm["reels"][0]["why"]))

    def test_comments(self):
        self.k.yt.use_api = True
        def api(endpoint, units, **p):
            self.assertEqual(endpoint, "commentThreads")
            c = lambda t, l: {"snippet": {"topLevelComment": {"snippet": {"textDisplay": t, "likeCount": l}}}}
            return {"items": [c("How long does the drain fix last?", 40), c("Please make a part 2 on sinks", 12), c("great video", 3)]}
        self.k.yt.api = api
        r = self.k.comments(ref="https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(r["top_questions"][0]["likes"], 40)
        self.assertEqual(len(r["top_requests"]), 1)

    def test_besttime(self):
        now = dt.datetime.now(dt.timezone.utc)
        rows = [{"views": 1000 * (3 if i % 2 else 1), "posted": (now.replace(hour=13 if i % 2 else 4)).strftime("%Y-%m-%dT%H:%M:%S+0000")}
                for i in range(8)]
        self.k.mine._ig_pick = lambda a: [{"label": "a", "ig_user_id": "1", "token": "T"}]
        self.k.mine._ig_get = lambda t, path, **q: {"data": [{"values": [{"end_time": "2026-11-10T08:00:00+0000",
                                                                         "value": {str(h): (500 if h == 6 else 10) for h in range(24)}}]}]}
        self.k.mine.ig_insights = lambda a, days=28, reels=25: {"reels": rows}
        r = self.k.besttime("nichethree")
        self.assertEqual(len(r["best_hours_ist"]), 3)
        self.assertEqual(r["errors"], [])

    def test_seed_outliers(self):
        p = os.path.join(self.tmp, "seeds.json")
        with open(p, "w") as f:
            json.dump({"Nichethree": {"seeds": ["@foo"]}, "nicheone": ["bar"]}, f)
        os.environ["SCOUT_AUTOPILOT_SEEDS"] = p
        now = dt.datetime.now(dt.timezone.utc)
        media = [{"media_product_type": "REELS", "like_count": 100, "permalink": "o%d" % i, "caption": "c",
                  "timestamp": (now - dt.timedelta(days=2 + i)).strftime("%Y-%m-%dT%H:%M:%S+0000")} for i in range(6)]
        media[0]["like_count"] = 2000
        self.k.ig.profile = lambda u: {"username": u, "followers": 50000, "media": media}
        try:
            r = self.k.ig_seed_outliers(group="nichethree")
        finally:
            os.environ.pop("SCOUT_AUTOPILOT_SEEDS")
        self.assertEqual(r["groups"], ["nichethree"])
        self.assertEqual(r["results"][0]["adj_multiple"], 6.7)   # 2000 / 300 floor

    def test_seed_outliers_dedupe_across_groups(self):
        p = os.path.join(self.tmp, "seeds2.json")
        with open(p, "w") as f:
            json.dump({"nichefive": ["foo"], "nichetwo": ["foo"]}, f)
        os.environ["SCOUT_AUTOPILOT_SEEDS"] = p
        now = dt.datetime.now(dt.timezone.utc)
        media = [{"media_product_type": "REELS", "like_count": 100, "permalink": "o%d" % i, "caption": "c",
                  "timestamp": (now - dt.timedelta(days=2 + i)).strftime("%Y-%m-%dT%H:%M:%S+0000")} for i in range(6)]
        media[0]["like_count"] = 5000
        self.k.ig.profile = lambda u: {"username": u, "followers": 50000, "media": media}
        try:
            r = self.k.ig_seed_outliers()
        finally:
            os.environ.pop("SCOUT_AUTOPILOT_SEEDS")
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["groups"], ["nichefive", "nichetwo"])

    def test_brief_page_rules(self):
        p = os.path.join(self.tmp, "rules.json")
        with open(p, "w") as f:
            json.dump({"nichefive": {"exclude_any": ["leaders"]}, "nichetwo": {"exclude_any": ["nobody"]}}, f)
        os.environ["SCOUT_PAGE_RULES"] = p
        try:
            b = self.k.brief()
        finally:
            os.environ.pop("SCOUT_PAGE_RULES")
        cards = {x["page"]: [c["url"] for c in x["cards"]] for x in b["pages"]}
        self.assertEqual(cards, {"nichefive": ["u2"], "nichetwo": []})   # u1 breaks nichefive's rule, u2 breaks nichetwo's but fits nichefive
        self.assertEqual(b["rules_loaded"], ["nichefive", "nichetwo"])
        self.assertTrue(sx.passes({"source": "instagram", "adj_multiple": 9}, {"sources": ["instagram"], "min_multiple": 5}))
        self.assertFalse(sx.passes({"source": "x", "adj_multiple": 9}, {"sources": ["instagram"]}))

    def test_dashboard_route(self):
        srv = scout_server.make_server(self.k, "k" * 32, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen("http://127.0.0.1:%d/dashboard" % port)
            req = urllib.request.Request("http://127.0.0.1:%d/dashboard?niche=nichefive" % port, headers={"X-Scout-Key": "k" * 32})
            body = urllib.request.urlopen(req).read().decode()
            self.assertIn("kestrel", body)
            self.assertIn("Why nobody talks about this?", body)
            os.environ["SCOUT_DASHBOARD_TRUST_CF"] = "1"
            req = urllib.request.Request("http://127.0.0.1:%d/dashboard" % port, headers={"Cf-Access-Authenticated-User-Email": "a@b.c"})
            self.assertEqual(urllib.request.urlopen(req).status, 200)
        finally:
            os.environ.pop("SCOUT_DASHBOARD_TRUST_CF", None)
            srv.shutdown()


class Bulletproof(unittest.TestCase):
    """Server hardening from 14 Nov: key rotation, per-caller share, yt-dlp fallback, stale Instagram answers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = sc.Config(db=os.path.join(self.tmp, "c.sqlite"), yt_keys=["KEY1", "KEY2"], yt_budget=100, yt_caller_pct=60, ig_pool="")
        self.cache = sc.Cache(self.cfg.db)
        self.yt = sc.YouTube(self.cfg, self.cache)
        self.used = []
        self._orig = sc.http_json
        sc.CALLER.name = "a"

    def tearDown(self):
        sc.http_json = self._orig
        sc.CALLER.name = None

    def fake(self, quota_on=()):
        def f(url, timeout=40):
            key = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["key"][0]
            self.used.append(key)
            if key in quota_on:
                raise sc.HttpErr(403, '{"error":{"errors":[{"reason":"quotaExceeded"}]}}')
            return {"items": []}, {}
        sc.http_json = f

    def test_rotates_when_key_one_is_full(self):
        self.fake()
        for _ in range(3):
            self.yt.api("videos", 40)   # 40 + 40 fill key 1 to 80, the third call doesn't fit, goes to key 2
        self.assertEqual(self.used, ["KEY1", "KEY1", "KEY2"])

    def test_quota_exceeded_marks_key_spent(self):
        self.fake(quota_on={"KEY1"})
        self.yt.api("videos", 1)
        self.assertEqual(self.used, ["KEY1", "KEY2"])
        self.used.clear()
        self.yt.api("videos", 1)
        self.assertEqual(self.used, ["KEY2"])   # key 1 stays skipped for the rest of the day

    def test_caller_share(self):
        self.fake()
        for _ in range(3):
            self.yt.api("videos", 40)            # caller a: 120 of its 120-unit share (60% of 200)
        self.assertFalse(self.yt.use_api)
        with self.assertRaises(sc.ScoutError):
            self.yt.api("videos", 1)
        sc.CALLER.name = "b"
        self.assertTrue(self.yt.use_api)          # caller b still gets the API
        self.yt.api("videos", 1)

    def test_falls_back_to_ytdlp_when_spent(self):
        self.fake(quota_on={"KEY1", "KEY2"})
        with self.assertRaises(sc.ScoutError):
            self.yt.api("videos", 1)
        self.assertFalse(self.yt.use_api)
        calls = []
        self.yt.dlp = lambda args, timeout=150: calls.append(args) or [{"id": "abcdefghijk", "title": "t", "view_count": 5}]
        out = self.yt.search("drain fix", 5)
        self.assertTrue(calls)
        vids = out["videos"] if isinstance(out, dict) else out
        self.assertEqual(vids[0]["id"], "abcdefghijk")

    def test_instagram_hourly_budget_per_app(self):
        s = sc.Scout(sc.Config(db=os.path.join(self.tmp, "e.sqlite"), ig_pool=""))
        s.ig.pool = lambda: [{"label": "a", "token": "T1", "ig_user_id": "1", "app": "scout"},
                             {"label": "b", "token": "T2", "ig_user_id": "2", "app": "poster"}]
        used = []
        def f(url, timeout=40):
            used.append(url.split("/")[4].split("?")[0])
            return {"business_discovery": {"followers_count": 5, "media": {"data": []}}}, {}
        sc.http_json = f
        os.environ["SCOUT_IG_PER_HOUR"] = "1"
        try:
            s.ig.profile("one")
            s.ig.profile("two")              # first app is at its cap, so the second app's token is used
            with self.assertRaises(sc.ScoutError) as cm:
                s.ig.profile("three")        # both apps capped, nothing saved
            self.assertEqual(cm.exception.status, 429)
            s.cache.put("igp_last:four", {"username": "four", "followers": 1, "media": []}, 3600)
            self.assertTrue(s.ig.profile("four")["stale"])
        finally:
            os.environ.pop("SCOUT_IG_PER_HOUR")
        self.assertEqual(sorted(used), ["1", "2"])

    def test_instagram_asks_for_views_and_falls_back_without_them(self):
        s = sc.Scout(sc.Config(db=os.path.join(self.tmp, "v.sqlite"), ig_pool=""))
        s.ig.pool = lambda: [{"label": "a", "token": "T1", "ig_user_id": "1", "app": "scout"}]
        asked = []

        def f(url, timeout=40):
            fields = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["fields"][0]
            asked.append("view_count" in fields)
            if "view_count" in fields:
                raise sc.HttpErr(400, '{"error":{"code":100,"message":"(#100) Tried accessing nonexisting field (view_count)"}}')
            return {"business_discovery": {"followers_count": 5, "media": {"data": []}}}, {}
        sc.http_json = f
        self.assertEqual(s.ig.profile("one")["followers"], 5)
        s.ig.profile("two")
        self.assertEqual(asked, [True, False, False])   # asked once, remembered for the day

    def test_instagram_per_app_cap(self):
        s = sc.Scout(sc.Config(db=os.path.join(self.tmp, "f.sqlite"), ig_pool=""))
        s.ig.pool = lambda: [{"label": "a", "token": "T1", "ig_user_id": "1", "app": "scout"},
                             {"label": "p", "token": "T2", "ig_user_id": "2", "app": "poster"}]
        used = []
        def f(url, timeout=40):
            used.append(url.split("/")[4].split("?")[0])
            return {"business_discovery": {"followers_count": 5, "media": {"data": []}}}, {}
        sc.http_json = f
        os.environ.update(SCOUT_IG_PER_HOUR="3", SCOUT_IG_PER_HOUR_POSTER="1")
        try:
            for u in ("a", "b", "c", "d"):
                s.ig.profile(u)
            with self.assertRaises(sc.ScoutError):
                s.ig.profile("e")
        finally:
            os.environ.pop("SCOUT_IG_PER_HOUR"); os.environ.pop("SCOUT_IG_PER_HOUR_POSTER")
        self.assertEqual(used.count("2"), 1)   # poster stops at its own lower cap
        self.assertEqual(used.count("1"), 3)

    def test_unnamed_caller_is_traced_to_its_script(self):
        self.fake()
        sc.CALLER.name = None
        os.environ["SCOUT_YT_ANON_PCT"] = "100"
        try:
            self.yt.api("search", 100)
        finally:
            os.environ.pop("SCOUT_YT_ANON_PCT")
        who = self.yt._caller()
        self.assertTrue(who.startswith("anon:test_scout.py:"), who)
        u = sc.Scout(self.cfg)
        u.cache = self.cache
        rows = u.usage()["youtube_unnamed_callers_today"]
        self.assertEqual(rows[0]["endpoint"], "search")
        self.assertEqual(rows[0]["units"], 100)
        self.assertTrue(rows[0]["caller"].startswith("anon:test_scout.py"))

    def test_unnamed_callers_share_a_small_budget(self):
        self.fake()
        sc.CALLER.name = None
        os.environ["SCOUT_YT_ANON_PCT"] = "20"      # 20% of 200 = 40 units for all unnamed callers together
        try:
            self.yt.api("videos", 40)
            self.assertFalse(self.yt.use_api)
            sc.CALLER.name = "research"
            self.assertTrue(self.yt.use_api)       # named callers are unaffected
        finally:
            os.environ.pop("SCOUT_YT_ANON_PCT")

    def test_instagram_stale_answer(self):
        s = sc.Scout(sc.Config(db=os.path.join(self.tmp, "d.sqlite"), ig_pool=""))
        s.ig.pool = lambda: [{"label": "a", "token": "T", "ig_user_id": "1"}]
        s.cache.put("igstate:a", {"pause_until": time.time() + 3600}, 3600)
        with self.assertRaises(sc.ScoutError) as cm:
            s.ig.profile("nosaved")
        self.assertEqual(cm.exception.status, 429)
        s.cache.put("igp_last:foo", {"username": "foo", "followers": 10, "media": []}, 86400)
        p = s.ig.profile("foo")
        self.assertTrue(p["stale"])
        self.assertEqual(p["followers"], 10)


class GrowthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.k = make_k(self.tmp)

    def test_clips(self):
        heat = [{"start_time": i * 10.0, "end_time": i * 10.0 + 10, "value": 0.1} for i in range(60)]
        heat[0]["value"] = 1.0       # intro spike, must be skipped
        heat[20]["value"] = 0.9      # 3:20
        heat[21]["value"] = 0.8      # too close to 3:20
        heat[45]["value"] = 0.7      # 7:30
        self.k.yt.dlp = lambda args, timeout=150: [{"title": "Podcast", "duration": 600, "channel": "MU", "heatmap": heat, "chapters": []}]
        self.k.yt.transcript = lambda vid, lang="en": {"segments": [{"start": 190.0, "text": "the line that matters"}]}
        r = self.k.clips("https://www.youtube.com/watch?v=abcdefghijk", n=2)
        self.assertEqual([c["peak"] for c in r["clips"]], ["3:25", "7:35"])
        self.assertEqual(r["clips"][0]["transcript"], "the line that matters")
        self.assertIn("&t=", r["clips"][0]["link"])

    def test_clips_without_heatmap(self):
        self.k.yt.dlp = lambda args, timeout=150: [{"title": "New", "duration": 100, "heatmap": None}]
        with self.assertRaises(sc.ScoutError) as cm:
            self.k.clips("abcdefghijk")
        self.assertEqual(cm.exception.status, 404)

    def test_retention(self):
        m = self.k.mine
        m._me = lambda ch: {"uploads": "UU1"}
        m._uploads = lambda ch, me, n=50: [{"id": "abcdefghijk"}]
        m._get = lambda ch, url: {"items": [{"id": "abcdefghijk", "snippet": {"title": "Fix it", "publishedAt": "2026-11-01T00:00:00Z"},
                                             "contentDetails": {"duration": "PT10M"}}]}
        curve = [1.0, 0.5] + [0.45] * 8 + [0.35]   # big early drop, then a smaller one at the end
        m._rep = lambda ch, **q: [{"elapsedVideoTimeRatio": i / 10.0, "audienceWatchRatio": w, "relativeRetentionPerformance": 0.4}
                                  for i, w in enumerate(curve)]
        r = self.k.me_retention("demo")["videos"][0]
        self.assertEqual(r["biggest_drops"][0]["at"], "1:00")
        self.assertEqual(r["still_watching_at_30s_pct"], 50.0)
        self.assertTrue(any("0:30" in t for t in r["tips"]))

    def test_gaps(self):
        now = dt.datetime.now(dt.timezone.utc)
        def search(t, n=30, order="relevance", days=None, kind="any"):
            age = 400 if "old" in t else 10
            return {"videos": [dict(vid(i, 100000, age, ch="UC%d" % i), title=t) for i in range(10)]}
        self.k.yt.search = search
        self.k.yt.autocomplete = lambda seed: ["drain fix old", "drain fix new"]
        r = self.k.gaps(seed="drain fix", terms=3)
        self.assertEqual(r["gaps"][0]["term"], "drain fix old")
        self.assertEqual(r["gaps"][0]["fresh_videos_in_top20"], 0)

    def test_title_change(self):
        self.k._w("insert into tracked values(?,?,?,?)", ("UC" + "a" * 22, "Chan", "x", 0))
        ups = [dict(vid(1, 1000, 5), title="Old title")]
        self.k.yt.recent = lambda info, kind="any", n=30: ups
        self.k.scan_changes(thumbs=False)
        ups[0] = dict(vid(1, 5000, 6), title="New title")
        self.assertEqual(self.k.scan_changes(thumbs=False)["changes_found"], 1)
        c = self.k.changes()["changes"][0]
        self.assertEqual((c["old_title"], c["new_title"], c["views_at_change"]), ("Old title", "New title", 1000))

    def test_rising(self):
        now = time.time()
        posts = [{"url": "https://x.com/a/status/%d" % i, "author": "a%d" % i, "text": "Agniveer pay revised again", "likes": 10,
                  "created": dt.datetime.fromtimestamp(now - 3600, dt.timezone.utc).isoformat()} for i in range(6)]
        self.k.bots.ingest("x", posts)
        r = self.k.rising(min_count=5)
        row = next(x for x in r["rising"] if x["term"] == "agniveer")
        self.assertEqual((row["this_period"], row["last_period"]), (6, 0))

    def test_clips_fallback_chapters_and_transcript(self):
        segs = [{"start": float(t), "text": "small talk here"} for t in range(0, 600, 5)]
        segs[60] = {"start": 300.0, "text": "I failed three times and lost 2 crore, nobody tells you this"}
        self.k.yt.transcript = lambda vid, lang="en": {"segments": segs}
        self.k.yt.dlp = lambda args, timeout=150: [{"title": "MU talk", "duration": 600, "heatmap": None,
                                                    "chapters": [{"start_time": 0, "end_time": 120, "title": "Intro"},
                                                                 {"start_time": 290, "end_time": 400, "title": "The failure"}]}]
        r = self.k.clips("abcdefghijk", n=1)
        self.assertEqual((r["method"], r["clips"][0]["chapter"]), ("chapters", "The failure"))
        self.k.cache.put("heat:abcdefghij2", {"title": "t", "duration": 600, "channel": "c", "heatmap": [], "chapters": []}, 3600)
        r = self.k.clips("abcdefghij2", n=1)
        self.assertEqual(r["method"], "transcript")
        self.assertIn("crore", r["clips"][0]["transcript"])

    def test_gaps_needs_real_seeds(self):
        self.k.niche_add("demo", channels=["@x"])
        with self.assertRaises(sc.ScoutError) as cm:
            self.k.gaps(niche="demo")
        self.assertIn("gap_seeds", str(cm.exception))
        p = os.path.join(self.tmp, "rules.json")
        with open(p, "w") as f:
            json.dump({"demo": {"gap_seeds": ["home repair hacks"]}}, f)
        os.environ["SCOUT_PAGE_RULES"] = p
        searched = []
        self.k.yt.autocomplete = lambda seed: ["home repair hacks cheap", "people using phone"]
        self.k.yt.search = lambda t, n=30, order="relevance", days=None, kind="any": searched.append(t) or {
            "videos": [dict(vid(i, 1000, 30, ch="UC%d" % i), title=t) for i in range(6)]}
        try:
            r = self.k.gaps(niche="demo")
        finally:
            os.environ.pop("SCOUT_PAGE_RULES")
        self.assertEqual(searched, ["home repair hacks", "home repair hacks cheap"])   # the drifting suggestion is dropped
        self.assertEqual(r["seed"], "home repair hacks")

    def test_rising_without_history(self):
        now = time.time()
        posts = [{"url": "https://x.com/a/status/%d" % i, "author": "a%d" % i, "text": "People say Agniveer pay revised", "likes": 1,
                  "created": dt.datetime.fromtimestamp(now - 3600, dt.timezone.utc).isoformat()} for i in range(6)]
        self.k.bots.ingest("x", posts)
        r = self.k.rising(min_count=5)
        self.assertFalse(r["has_growth"])
        self.assertNotIn("people", [x["term"] for x in r["rising"]])
        self.assertTrue(all("growth" not in x for x in r["rising"]))
        self.assertIn("not growth", r["notes"][0])

    def test_sponsors(self):
        day = self.k._today()
        for i, d in enumerate(["Thanks to NordVPN for sponsoring. https://nordvpn.com/demo use code DEMO10",
                               "Sponsored by NordVPN https://www.nordvpn.com/x and https://instagram.com/me"]):
            row = dict(vid(i, 1000, 2), channel="Chan%d" % i, description=d)
            self.k._w("insert into feed values(?,?,?,?,?,?,?)", (day, "demo", row["url"] + str(i), "youtube", 3, 1, json.dumps(dict(row, url=row["url"] + str(i)))))
        r = self.k.sponsors("demo")
        self.assertEqual(r["linked_domains"][0], {"name": "nordvpn.com", "channels": ["Chan0", "Chan1"], "channel_count": 2})
        self.assertEqual(r["named_sponsors"][0]["name"], "NordVPN")
        self.assertEqual(r["promo_codes"][0]["name"], "DEMO10")
        self.assertNotIn("instagram.com", [d["name"] for d in r["linked_domains"]])


class FitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.k = make_k(self.tmp)
        self.k.niche_add("nichethree", ig_users=["a"])
        self.k.niche_add("nichetwo", ig_users=["a"])
        day = self.k._today()
        rows = [("nichethree", ig_item("b1", "@leila", 21843, 53, 1634130, "Comment WIN if you want my 5 free scaling frameworks", 3)),
                ("nichethree", ig_item("b2", "@biz", 9000, 100, 50000, "How a café in Indiana made $2M from one menu change.", 2)),
                ("nichetwo", ig_item("p1", "@news", 4000, 100, 40000, "Bodies of two missing SPOs recovered in Kathua", 1)),
                ("nichetwo", ig_item("p2", "@infra", 8000, 100, 40000, "India opens its longest sea bridge to traffic", 1))]
        for n, r in rows:
            self.k._w("insert into feed values(?,?,?,?,?,?,?)", (day, n, r["url"], "instagram", r["multiple"], 1, json.dumps(r)))
        self.rules = os.path.join(self.tmp, "rules.json")
        with open(self.rules, "w") as f:
            json.dump({"nichethree": {"exclude_any": ["india"]}, "nichetwo": {"about": "proud India growth stories, named leaders, no tragedies"}}, f)
        os.environ["SCOUT_PAGE_RULES"] = self.rules

    def tearDown(self):
        os.environ.pop("SCOUT_PAGE_RULES", None)
        os.environ.pop("SCOUT_JUDGE_URL", None)

    def test_whole_words(self):
        self.assertIsNone(sx.check({"caption": "A café in Indiana"}, {"exclude_any": ["india"]}))
        self.assertEqual(sx.check({"caption": "A café in Indiana"}, {"exclude_any": ["india*"]}), "exclude word 'india*'")
        self.assertIsNotNone(sx.check({"caption": "Made in India, again"}, {"exclude_any": ["india"]}))

    def test_bait(self):
        self.assertTrue(sx.is_bait({"caption": "Comment WIN if you want my 5 free scaling frameworks"}))
        self.assertTrue(sx.is_bait({"caption": "Comment “SEND” and I'll DM you"}) or sx.is_bait({"caption": "link in bio for the guide"}))
        self.assertFalse(sx.is_bait({"caption": "How a café in Indiana made $2M", "likes": 900, "comments": 20}))
        self.assertEqual(sx.check({"caption": "Comment WIN for the free PDF"}, {}), "engagement bait")
        self.assertIsNone(sx.check({"caption": "Comment WIN for the free PDF"}, {"allow_bait": True}))

    def test_more_bait_and_language(self):
        self.assertTrue(sx.is_bait({"caption": "Best Indian Wholesale Marketplaces Comment \u201cWHOLESALE\u201d & I\u2019ll send you the list"}))
        self.assertTrue(sx.is_bait({"caption": "If you want my 5 free scaling frameworks, comment \u201cTEAM\u201d and I'll send them"}))
        self.assertEqual(sx.check({"caption": "\u0b89\u0b99\u0bcd\u0b95 \u0bb7\u0bbe\u0baa\u0bcd\u0bb2 \u0b92\u0bb0\u0bc1\u0bae\u0bc1\u0bb1\u0bc8"}, {"english_only": True, "allow_bait": True}), "not in English")
        self.assertIsNone(sx.check({"caption": "Jio changed India's future \U0001f1ee\U0001f1f3"}, {"english_only": True}))
        self.assertEqual(sx.check({"source": "reddit", "adj_multiple": 0, "text": "Modi sets 2047 deadline"}, {"skip_unscored": True}), "no engagement numbers")

    def test_brief_lists_every_cut_with_reason(self):
        b = self.k.brief()
        cards = {p["page"]: [c["url"] for c in p["cards"]] for p in b["pages"]}
        self.assertEqual(cards["nichethree"], ["b2"])            # Indiana survives the 'india' rule now
        reasons = {c["url"]: c["reason"] for c in b["cut"]}
        self.assertEqual(reasons["b1"], "engagement bait")
        self.assertTrue(any(x["reason"] == "engagement bait" for x in b["cut_summary"]))

    def test_min_multiple_defaults_to_2_and_page_can_raise_it(self):
        with open(self.rules, "w") as f:
            json.dump({"nichetwo": {"min_multiple": 30}}, f)
        b = self.k.brief()
        cards = {p["page"]: [c["url"] for c in p["cards"]] for p in b["pages"]}
        self.assertEqual(cards["nichetwo"], [])
        self.assertTrue(all(c["reason"].startswith("adj_multiple") for c in b["cut"] if c["page"] == "nichetwo"))

    def test_judge(self):
        os.environ["SCOUT_JUDGE_URL"] = "http://judge"
        seen = []
        def post(url, payload):
            seen.append(payload)
            fit = {"p1": (0.1, "tragedy, no named leader"), "p2": (0.9, "growth story"), "b2": (0.8, "ok")}
            return {"scores": [{"id": i["id"], "fit": fit[i["id"]][0], "reason": fit[i["id"]][1]} for i in payload["items"]]}
        self.k._post = post
        b = self.k.brief()
        cards = {p["page"]: [c["url"] for c in p["cards"]] for p in b["pages"]}
        self.assertEqual(cards["nichetwo"], ["p2"])
        self.assertIn("judge: tragedy", {c["url"]: c["reason"] for c in b["cut"]}["p1"])
        self.assertEqual(next(p for p in seen if p["page"] == "nichetwo")["about"], "proud India growth stories, named leaders, no tragedies")

    def test_judge_down_passes_items(self):
        os.environ["SCOUT_JUDGE_URL"] = "http://judge"
        def post(url, payload):
            raise OSError("down")
        self.k._post = post
        b = self.k.brief()
        self.assertEqual(len([c for p in b["pages"] if p["page"] == "nichetwo" for c in p["cards"]]), 2)
        self.assertIn("unavailable", b["judge"]["nichetwo"]["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
