#!/usr/bin/env python3
"""Scout Plus: the vidIQ-style layer on top of scout_core.

  Daily feed for your niches, breakout channels, competitor tracking with new-outlier alerts,
  title and thumbnail wins, similar thumbnails, video optimize, best time to post, a posting
  calendar, and idea seeds for your agent to write from.

Stdlib only. Pillow is optional and turns on the thumbnail features (pip install pillow).
Everything is stored in the same sqlite file as the cache.
"""
import collections
import datetime as dt
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from scout_core import Scout, ScoutError, med, video_id, age_days, UA
from scout_mine import Mine
from scout_social import X, Facebook, BotFeed

try:
    from PIL import Image, ImageStat
except ImportError:  # thumbnail features switch off cleanly
    Image = ImageStat = None

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

STOP = set("""a an and are as at be but by can do does for from get got has have how i if in into is it its just
make me more my new not now of on or our out so than that the their them then there these they this to too up us
was we what when where which who why will with you your shorts short video videos youtube vs""".split())

FEATURES = [
    ("number", r"\d", 0),
    ("starts_with_number", r"^\W*\d", 0),
    ("question", r"\?", 0),
    ("exclamation", r"!", 0),
    ("all_caps_word", r"\b[A-Z]{3,}\b", 0),
    ("brackets", r"[\(\[]", 0),
    ("colon_or_pipe", r"[:|]", 0),
    ("first_person", r"\b(i|i'm|im|i've|my|me|we|our)\b", re.I),
    ("you_your", r"\b(you|your|you're)\b", re.I),
    ("how_to", r"\bhow to\b", re.I),
    ("versus", r"\b(vs\.?|versus)\b", re.I),
    ("negative", r"\b(never|stop|don'?t|mistakes?|worst|wrong|avoid|fail(ed|s)?|bad|ruin(ed|s)?)\b", re.I),
    ("money", r"[$₹€£]|\b\d+k\b|\b(free|cheap|money|price|cost)\b", re.I),
    ("time_frame", r"\b\d+\s?(sec(ond)?s?|min(ute)?s?|hours?|days?|weeks?)\b", re.I),
    ("superlative", r"\b(best|most|ultimate|only|secret|insane|crazy|genius)\b", re.I),
    ("emoji", "[\U0001F300-\U0001FAFF☀-➿]", 0),
    ("hashtag", r"#\w", 0),
]
_FEAT = [(n, re.compile(p, f)) for n, p, f in FEATURES]

IDEA_BRIEF = (
    "For each seed, never copy the title, thumbnail or script. Keep the proven idea and change it three ways: "
    "A) same subject, different trait. B) same subject and trait, new format (a ranking, a list, a compilation). "
    "C) stack a second proven subject into one concept. Write all three, then pick the best and say why. "
    "Package it: one dominant subject, one readable emotion, a curiosity gap (the title asks, the thumbnail half answers), "
    "contrast or conflict where it fits. Shorts titles stay under 70 characters with the hook first and no hashtags. "
    "Plain English, no em-dashes. Give options with a recommendation. The human makes the final call. "
    "Use niche_patterns as evidence of what wins in this niche."
)


# ---------------------------------------------------------------- text helpers

def title_features(t):
    return {n: bool(rx.search(t or "")) for n, rx in _FEAT}


def words(t):
    return [w for w in re.findall(r"[a-z0-9']+", (t or "").lower()) if len(w) >= 3 and w not in STOP]


def stems(q):
    return {w[:5] for w in words(q)}


def title_compare(win, rest):
    fw = [title_features(v.get("title")) for v in win]
    fr = [title_features(v.get("title")) for v in rest]
    out = []
    for name, _ in _FEAT:
        pw = sum(f[name] for f in fw) / max(len(fw), 1)
        pr = sum(f[name] for f in fr) / max(len(fr), 1)
        if pw < 0.15 and pr < 0.15:
            continue
        out.append({"pattern": name, "winners_pct": round(100 * pw), "others_pct": round(100 * pr),
                    "lift": round((pw + 0.02) / (pr + 0.02), 2)})
    out.sort(key=lambda p: -p["lift"])
    return out


def word_lift(win, rest, exclude=()):
    cw, cr = collections.Counter(), collections.Counter()
    for v in win:
        cw.update(set(words(v.get("title"))))
    for v in rest:
        cr.update(set(words(v.get("title"))))
    rows = []
    for w, c in cw.items():
        if c < 2 or w[:5] in exclude:
            continue
        pw, pr = c / len(win), cr[w] / max(len(rest), 1)
        rows.append({"word": w, "winners": c, "others": cr[w], "lift": round((pw + 0.02) / (pr + 0.02), 2)})
    rows.sort(key=lambda r: (-r["lift"], -r["winners"]))
    return rows[:12]


def split_winners(vids, win_multiple):
    win = [v for v in vids if v["multiple"] >= win_multiple]
    rule = "multiple >= %g" % win_multiple
    if len(win) < 3:
        k = max(3, len(vids) // 4)
        win = sorted(vids, key=lambda v: -v["multiple"])[:k]
        rule = "top quarter by multiple (fewer than 3 videos passed %gx)" % win_multiple
    ids = {v["id"] for v in win}
    return win, [v for v in vids if v["id"] not in ids], rule


def mmss(s):
    return "%d:%02d" % (s // 60, s % 60) if s is not None else None


def brief(v):
    keep = ("id", "title", "url", "thumbnail", "channel", "views", "multiple", "age_days", "is_short", "caption", "account", "likes")
    return {k: v.get(k) for k in keep if v.get(k) is not None}


# ---------------------------------------------------------------- thumbnail helpers

def http_bytes(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError) as e:
        raise ScoutError("could not fetch thumbnail: " + str(getattr(e, "reason", e)), 502)


def image_features(data):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = im.size
    th = int(w * 9 / 16)
    if th < h:  # hqdefault is 4:3 with black bars, keep the 16:9 picture
        top = (h - th) // 2
        im = im.crop((0, top, w, top + th))
    small = im.resize((64, 36))
    hsv = ImageStat.Stat(small.convert("HSV"))
    lum = ImageStat.Stat(small.convert("L"))
    g = im.convert("L").resize((9, 8)).tobytes()
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (1 if g[r * 9 + c] > g[r * 9 + c + 1] else 0)
    raw = small.tobytes()
    hist = [0] * 64
    for i in range(0, len(raw), 3):
        hist[(raw[i] // 64) * 16 + (raw[i + 1] // 64) * 4 + raw[i + 2] // 64] += 1
    n = float(sum(hist)) or 1.0
    q = small.quantize(colors=4)
    pal = q.getpalette() or []
    colors = [{"hex": "#%02x%02x%02x" % tuple(pal[i * 3:i * 3 + 3]), "share": round(c / n, 2)}
              for c, i in sorted(q.getcolors() or [], reverse=True) if len(pal) >= i * 3 + 3]
    return {"dhash": "%016x" % bits, "hist": [round(x / n, 4) for x in hist],
            "brightness": round(hsv.mean[2] / 255, 3), "saturation": round(hsv.mean[1] / 255, 3),
            "contrast": round(lum.stddev[0] / 128, 3), "colors": colors}


def thumb_similarity(a, b):
    ham = bin(int(a["dhash"], 16) ^ int(b["dhash"], 16)).count("1")
    inter = sum(min(x, y) for x, y in zip(a["hist"], b["hist"]))
    return round(1 - (0.5 * ham / 64 + 0.5 * (1 - inter)), 3)


def public_thumb(f):
    return {k: f[k] for k in ("brightness", "saturation", "contrast", "colors")}


# ---------------------------------------------------------------- the product

class ScoutPlus(Scout):
    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.mine = Mine(self.cache, self.ig)
        self.x = X(self.cache)
        self.fb = Facebook(self.cache, self.ig)
        self.bots = BotFeed(self.cache)
        self._job_lock = threading.Lock()
        with self.cache.lock:
            ex = self.cache.db.execute
            ex("create table if not exists niches(name text primary key, cfg text, created real)")
            ex("create table if not exists feed(day text, niche text, item_id text, kind text, multiple real, is_new integer, data text,"
               " primary key(day, niche, item_id))")
            ex("create table if not exists breakouts(day text, niche text, channel_id text, score real, data text,"
               " primary key(day, niche, channel_id))")
            ex("create table if not exists tracked(channel_id text primary key, title text, ref text, added real)")
            ex("create table if not exists snapshots(channel_id text, day text, subs integer, views integer, videos integer,"
               " primary key(channel_id, day))")
            ex("create table if not exists alerts(channel_id text, video_id text, first_seen real, data text,"
               " primary key(channel_id, video_id))")
            self.cache.db.commit()

    # ---- storage helpers
    def _q(self, sql, args=()):
        with self.cache.lock:
            return self.cache.db.execute(sql, args).fetchall()

    def _w(self, sql, args=()):
        with self.cache.lock:
            self.cache.db.execute(sql, args)
            self.cache.db.commit()

    @staticmethod
    def _today(days_back=0):
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")

    def _pool(self, query, kind="any", max_age_days=180, on_topic=True):
        """Every recent video from the channels a query finds, each with its multiple. Reuses cached searches."""
        return self.outliers(query=query, kind=kind, max_age_days=max_age_days, min_multiple=0, limit=1000, on_topic=on_topic)

    def _thumb(self, vid):
        if Image is None:
            raise ScoutError("thumbnail features need Pillow on the server: pip install pillow", 501)
        return self.cache.cached("tf:" + vid, 30 * 86400, lambda: image_features(
            http_bytes("https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid)))

    def _thumbs(self, vids, cap=15):
        out = []
        for v in vids[:cap]:
            try:
                out.append(self._thumb(v["id"]))
            except Exception:  # one bad image never sinks the batch
                continue
        return out

    # ---- niches (what the daily feed watches)
    def niches(self):
        return {"niches": [dict(json.loads(c), name=n) for n, c in self._q("select name, cfg from niches order by name")]}

    def _niche(self, name):
        r = self._q("select cfg from niches where name=?", ((name or "").strip().lower(),))
        if not r:
            raise ScoutError("no niche called '%s'. See /v1/niches" % name, 404)
        return dict(json.loads(r[0][0]), name=name.strip().lower())

    def niche_add(self, name, query=None, kind="any", max_age_days=30, min_multiple=3.0, breakout_max_subs=100000, ig_niche=None,
                  x_users=None, channels=None, ig_users=None, linkedin_users=None, subreddits=None):
        """A niche can be a search query, a fixed list of YouTube channels, Instagram accounts and X handles, or any mix."""
        name = (name or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9 _-]{0,40}", name):
            raise ScoutError("niche name: letters, numbers, spaces, dashes, up to 40 characters")
        clean = lambda xs, n: list(dict.fromkeys(x.strip().lstrip("@") if not x.strip().startswith(("http", "UC")) else x.strip()
                                                 for x in (xs or []) if x and x.strip()))[:n]
        channels = [c if c.startswith(("http", "UC")) else "@" + c for c in clean(channels, 40)]
        ig_users, x_users = clean(ig_users, int(os.environ.get("SCOUT_NICHE_IG_MAX", "100"))), clean(x_users, 50)
        linkedin_users = clean(linkedin_users, 50)
        subreddits = [r.lower().replace("r/", "") for r in clean(subreddits, 30)]
        if not ((query or "").strip() or channels or ig_users or x_users or ig_niche or linkedin_users or subreddits):
            raise ScoutError("give a query, channels, ig_users, x_users, linkedin_users or subreddits for this niche")
        if kind not in ("any", "shorts", "long"):
            raise ScoutError("kind must be any, shorts or long")
        cfg = {"query": (query or "").strip() or None, "kind": kind, "max_age_days": int(max_age_days), "min_multiple": float(min_multiple),
               "breakout_max_subs": int(breakout_max_subs), "ig_niche": ig_niche or None, "x_users": x_users,
               "channels": channels, "ig_users": ig_users, "linkedin_users": linkedin_users, "subreddits": subreddits}
        self._w("insert or replace into niches values(?,?,?)", (name, json.dumps(cfg), time.time()))
        return {"saved": dict(cfg, name=name)}

    def niche_remove(self, name):
        name = (name or "").strip().lower()
        n = len(self._q("select 1 from niches where name=?", (name,)))
        self._w("delete from niches where name=?", (name,))
        return {"removed": bool(n), "name": name}

    # ---- breakout channels
    def breakouts(self, query, kind="any", max_subs=100000, min_median_views=1000, max_age_days=90, limit=15):
        yt, notes = self.yt, []
        res = yt.search(query, 40, "relevance", days=max_age_days if yt.use_api else None, kind=kind if yt.use_api else "any")
        cids = list(dict.fromkeys(v["channel_id"] for v in res["videos"] if v["channel_id"]))[:15]
        infos = yt.channels_info(cids)
        if not yt.use_api:
            notes.append("yt-dlp mode: subscriber counts come from channel pages and can be missing")
        rows = []
        for cid in cids:
            info = infos.get(cid)
            if info is None:
                if yt.use_api:
                    continue
                try:
                    info = yt.channel_info(cid)
                except ScoutError:
                    continue
            subs = info.get("subscribers")
            if subs is None or subs > max_subs:
                continue
            ups = yt.recent(info, kind, 15)
            recent = [v for v in ups if v["age_days"] is None or v["age_days"] <= max_age_days]
            if len(recent) < 3:
                continue
            mv = med([v["views"] for v in recent])
            if mv < min_median_views:
                continue
            best = max(recent, key=lambda v: v["views"])
            created = age_days(info.get("created"))
            rows.append({"channel": info.get("title"), "id": cid, "url": info.get("url"), "subscribers": subs,
                         "median_recent_views": int(mv), "views_per_subscriber": round(mv / max(subs, 100), 2),
                         "channel_age_days": round(created) if created is not None else None,
                         "recent_uploads": len(recent), "best_recent": brief(best)})
        rows.sort(key=lambda r: -r["views_per_subscriber"])
        notes.append("A breakout is a small channel whose recent videos pull far more views than its subscriber count. "
                     "Higher views_per_subscriber means YouTube is pushing it past its own audience.")
        return {"query": query, "results": rows[:limit], "mode": self.mode(), "notes": notes}

    # ---- competitor tracking
    def _snapshot(self, info):
        self._w("insert or replace into snapshots values(?,?,?,?,?)",
                (info["id"], self._today(), info.get("subscribers"), info.get("total_views"), info.get("video_count")))

    def _scan_alerts(self, cid, info, seed=False, min_multiple=2.0):
        ups = self.yt.recent(info, "any", 20)
        base = [v for v in ups if v["age_days"] is None or v["age_days"] >= 3]
        if len(base) < 5:
            return []
        new = []
        for v in ups:
            if v["age_days"] is not None and v["age_days"] > 30:
                continue
            m = med([b["views"] for b in base if b["id"] != v["id"]]) or 1
            mult = v["views"] / max(m, 1)
            if mult < min_multiple or self._q("select 1 from alerts where channel_id=? and video_id=?", (cid, v["id"])):
                continue
            row = dict(brief(v), multiple=round(mult, 1), channel_median_views=int(m), channel=info.get("title"))
            self._w("insert into alerts values(?,?,?,?)", (cid, v["id"], 0 if seed else time.time(), json.dumps(row)))
            if not seed:
                new.append(row)
        return new

    def track_add(self, ref):
        info = self.yt.channel_info(ref)
        cid = info.get("id")
        if not cid:
            raise ScoutError("could not resolve a channel id for " + ref, 404)
        self._w("insert or replace into tracked values(?,?,?,?)", (cid, info.get("title"), ref, time.time()))
        self._snapshot(info)
        self._scan_alerts(cid, info, seed=True)  # current outliers become the baseline, not alerts
        return {"tracking": {"id": cid, "title": info.get("title"), "subscribers": info.get("subscribers")}}

    def track_remove(self, ref):
        r = (ref or "").strip()
        rows = self._q("select channel_id from tracked where channel_id=? or lower(ref)=? or lower(title)=?", (r, r.lower(), r.lower()))
        if not rows:
            try:
                cid = self.yt.channel_info(r).get("id")
                rows = self._q("select channel_id from tracked where channel_id=?", (cid,))
            except ScoutError:
                rows = []
        for (cid,) in rows:
            self._w("delete from tracked where channel_id=?", (cid,))
        return {"removed": len(rows)}

    def competitors(self, days=7):
        out = []
        for cid, title, ref, added in self._q("select channel_id, title, ref, added from tracked order by title"):
            snaps = self._q("select day, subs, views, videos from snapshots where channel_id=? order by day desc limit 60", (cid,))
            latest = snaps[0] if snaps else None

            def back(n):
                target = self._today(n)
                return next((s for s in snaps if s[0] <= target), None)

            def delta(old, i):
                return latest[i] - old[i] if latest and old and latest[i] is not None and old[i] is not None and old is not latest else None

            d1, d7, d30 = back(1), back(7), back(30)
            alerts = [json.loads(d) for (d,) in self._q(
                "select data from alerts where channel_id=? and first_seen>=? order by first_seen desc", (cid, time.time() - days * 86400))]
            out.append({"channel": title, "id": cid, "subscribers": latest[1] if latest else None,
                        "total_views": latest[2] if latest else None, "videos": latest[3] if latest else None,
                        "subs_change": {"1d": delta(d1, 1), "7d": delta(d7, 1), "30d": delta(d30, 1)},
                        "views_change": {"1d": delta(d1, 2), "7d": delta(d7, 2), "30d": delta(d30, 2)},
                        "uploads_change_7d": delta(d7, 3), "new_outliers": alerts,
                        "tracked_since": dt.datetime.fromtimestamp(added, dt.timezone.utc).strftime("%Y-%m-%d")})
        return {"tracked": out, "notes": ["Changes fill in as the daily job takes a snapshot each day.",
                                          "new_outliers = uploads that crossed 2x the channel's median since you started tracking."]}

    # ---- the daily job
    def run_daily(self, only=None):
        if not self._job_lock.acquire(blocking=False):
            raise ScoutError("the daily job is already running", 409)
        try:
            return self._run_daily(only)
        finally:
            self._job_lock.release()

    def _run_daily(self, only):
        t0, day = time.time(), self._today()
        summary = {"day": day, "niches": [], "tracked": [], "errors": []}
        niches = self.niches()["niches"]
        if only:
            want = {x.strip().lower() for x in only}
            niches = [n for n in niches if n["name"] in want]
        for n in niches:
            if self.yt.use_api and self.cfg.yt_budget - self.cache.usage("yt_units") < 300:
                summary["errors"].append({"niche": n["name"], "error": "stopped: YouTube unit budget nearly used"})
                break
            try:
                found = []
                if n.get("channels"):  # a curated list: no topic filter
                    found += self.outliers(channels=n["channels"], kind=n["kind"], max_age_days=n["max_age_days"], min_multiple=n["min_multiple"],
                                           limit=40, max_channels=len(n["channels"]), on_topic=False)["results"]
                if n.get("query"):
                    found += self.outliers(query=n["query"], kind=n["kind"], max_age_days=n["max_age_days"], min_multiple=n["min_multiple"], limit=25)["results"]
                seen_ids, uniq = set(), []
                for row in sorted(found, key=lambda x: -x["multiple"]):
                    if row["id"] not in seen_ids:
                        seen_ids.add(row["id"])
                        uniq.append(row)
                r = {"results": uniq}
                new = 0
                for row in r["results"]:
                    isnew = 0 if self._q("select 1 from feed where niche=? and item_id=? and day<?", (n["name"], row["id"], day)) else 1
                    new += isnew
                    self._w("insert or replace into feed values(?,?,?,?,?,?,?)",
                            (day, n["name"], row["id"], "youtube_shorts" if row.get("is_short") else "youtube", row["multiple"], isnew, json.dumps(row)))
                b = self.breakouts(n["query"], kind=n["kind"], max_subs=n["breakout_max_subs"], limit=10) if n.get("query") else {"results": []}
                for c in b["results"]:
                    self._w("insert or replace into breakouts values(?,?,?,?,?)", (day, n["name"], c["id"], c["views_per_subscriber"], json.dumps(c)))
                ig_n = 0
                if n.get("ig_niche") or n.get("ig_users"):
                    try:
                        ig = self.ig_outliers(users=n["ig_users"], limit=20, max_users=int(os.environ.get("SCOUT_IG_PER_NICHE", "40"))) if n.get("ig_users") else self.ig_outliers(niche=n["ig_niche"], limit=15)
                        for row in ig["results"]:
                            isnew = 0 if self._q("select 1 from feed where niche=? and item_id=? and day<?", (n["name"], row["url"], day)) else 1
                            self._w("insert or replace into feed values(?,?,?,?,?,?,?)",
                                    (day, n["name"], row["url"], "instagram", row["multiple"], isnew, json.dumps(row)))
                            ig_n += 1
                        for err in ig.get("errors", [])[:5]:
                            summary["errors"].append({"niche": n["name"], "error": "instagram @%s: %s" % (err["user"], err["error"])})
                    except ScoutError as e:
                        summary["errors"].append({"niche": n["name"], "error": "instagram: " + str(e)})
                social_n = {}
                for plat, key, kw in (("x", "x_users", "authors"), ("linkedin", "linkedin_users", "authors"), ("reddit", "subreddits", "communities")):
                    if not n.get(key):
                        continue
                    so = self.bots.outliers(plat, min_multiple=n["min_multiple"], max_age_days=min(n["max_age_days"], 14), limit=20, **{kw: n[key]})
                    for row in so["results"]:
                        isnew = 0 if self._q("select 1 from feed where niche=? and item_id=? and day<?", (n["name"], row["url"], day)) else 1
                        self._w("insert or replace into feed values(?,?,?,?,?,?,?)", (day, n["name"], row["url"], plat, row["multiple"], isnew, json.dumps(row)))
                    social_n[plat] = len(so["results"])
                summary["niches"].append({"niche": n["name"], "outliers": len(r["results"]), "new": new,
                                          "breakouts": len(b["results"]), "instagram": ig_n, **social_n})
            except ScoutError as e:
                summary["errors"].append({"niche": n["name"], "error": str(e)})
                if e.status == 429:
                    break
        if not only:
            for cid, title in self._q("select channel_id, title from tracked"):
                try:
                    info = self.yt.channel_info(cid)
                    self._snapshot(info)
                    summary["tracked"].append({"channel": title, "new_outliers": len(self._scan_alerts(cid, info))})
                except ScoutError as e:
                    summary["errors"].append({"channel": title, "error": str(e)})
        if not only:
            for ch in self.mine.channel_names():
                try:
                    a = self.mine.achievements(ch)
                    new = [{"track": x["track"], "milestones": x["new_since_last_check"]} for x in a["achievements"] if x["new_since_last_check"]]
                    summary.setdefault("my_channels", []).append({"channel": ch, "new_milestones": new})
                except ScoutError as e:
                    summary["errors"].append({"my_channel": ch, "error": str(e)})
        cutoff = self._today(90)
        self._w("delete from feed where day<?", (cutoff,))
        self._w("delete from breakouts where day<?", (cutoff,))
        summary["youtube_units_today"] = self.cache.usage("yt_units")
        summary["seconds"] = round(time.time() - t0, 1)
        return summary

    # ---- the feed
    def feed(self, day=None, niche=None, only_new=False, kind=None, limit=50):
        if not day:
            r = self._q("select max(day) from feed")
            day = r[0][0] if r and r[0][0] else None
        alerts = [json.loads(d) for (d,) in self._q("select data from alerts where first_seen>=? order by first_seen desc limit 20",
                                                   (time.time() - 2 * 86400,))]
        if not day:
            return {"day": None, "items": [], "breakouts": [], "competitor_alerts": alerts,
                    "notes": ["The feed is empty. Add a niche (scout_niche_add), then run the daily job or wait for the timer."]}
        sql, args = "select niche, kind, is_new, data from feed where day=?", [day]
        if niche:
            sql += " and niche=?"
            args.append(niche.strip().lower())
        if only_new:
            sql += " and is_new=1"
        if kind:
            sql += " and kind=?"
            args.append(kind)
        sql += " order by is_new desc, multiple desc limit ?"
        args.append(int(limit))
        items = [dict(json.loads(d), niche=n, source=k, new=bool(isn)) for n, k, isn, d in self._q(sql, args)]
        bsql, bargs = "select niche, data from breakouts where day=?", [day]
        if niche:
            bsql += " and niche=?"
            bargs.append(niche.strip().lower())
        brs = [dict(json.loads(d), niche=n) for n, d in self._q(bsql + " order by score desc limit 20", bargs)]
        return {"day": day, "items": items, "breakouts": brs, "competitor_alerts": alerts}

    # ---- title and thumbnail wins
    def wins(self, query, kind="any", max_age_days=180, win_multiple=3.0, on_topic=True, with_thumbs=False, limit=12):
        r = self._pool(query, kind, max_age_days, on_topic)
        vids = r["results"]
        if len(vids) < 10:
            raise ScoutError("only %d videos to compare. Broaden the query or pass on_topic=false." % len(vids), 404)
        win, rest, rule = split_winners(vids, win_multiple)
        out = {"query": query, "kind": kind, "videos_compared": len(vids), "winners": len(win), "winner_rule": rule,
               "title_patterns": title_compare(win, rest), "winning_words": word_lift(win, rest, stems(query)),
               "title_length_chars": {"winners_median": int(med([len(v["title"]) for v in win])),
                                      "others_median": int(med([len(v["title"]) for v in rest]) or 0)},
               "duration": {"winners_median": mmss(med([v["duration"] for v in win if v["duration"]])),
                            "others_median": mmss(med([v["duration"] for v in rest if v["duration"]]))},
               "top_tags": [t for t, _ in collections.Counter(t.lower() for v in win for t in (v.get("tags") or [])).most_common(15)],
               "examples": [brief(v) for v in sorted(win, key=lambda v: -v["multiple"])[:limit]],
               "mode": self.mode(), "notes": r.get("notes", [])}
        out["notes"] = out["notes"] + ["lift above 1 means winners use that pattern more than the rest. 1.5 or more is worth copying."]
        if with_thumbs:
            fw, fr = self._thumbs(sorted(win, key=lambda v: -v["multiple"])), self._thumbs(rest)
            if fw:
                pick = lambda fs, k: round(med([f[k] for f in fs]), 3) if fs else None
                cols = collections.Counter(c["hex"] for f in fw for c in f["colors"][:2])
                out["thumbnails"] = {k: {"winners": pick(fw, k), "others": pick(fr, k)} for k in ("brightness", "saturation", "contrast")}
                out["thumbnails"]["winner_colors"] = [h for h, _ in cols.most_common(6)]
        return out

    # ---- similar thumbnails
    def similar_thumbnails(self, ref, query=None, kind="any", limit=12):
        vid = video_id(ref)
        target = self._thumb(vid)
        cands = {}
        for (d,) in self._q("select data from feed where day>=? and kind!='instagram'", (self._today(14),)):
            v = json.loads(d)
            if v.get("id"):
                cands[v["id"]] = v
        if query:
            for v in self._pool(query, kind, 365, on_topic=False)["results"]:
                cands.setdefault(v["id"], v)
        cands.pop(vid, None)
        if not cands:
            raise ScoutError("nothing to compare against. Pass a query, or add niches so the feed has thumbnails.", 400)
        rows = []
        for v in list(cands.values())[:150]:
            try:
                rows.append(dict(brief(v), similarity=thumb_similarity(target, self._thumb(v["id"]))))
            except Exception:
                continue
        rows.sort(key=lambda r: -r["similarity"])
        return {"video": vid, "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid, "features": public_thumb(target),
                "compared": len(rows), "results": rows[:limit],
                "notes": ["Similarity is visual (layout, light and color), not meaning. 1.0 is identical."]}

    # ---- optimize one video
    def optimize(self, ref, query=None):
        vid = video_id(ref)
        v = (self.yt.videos([vid]) or [None])[0]
        if not v:
            raise ScoutError("video not found", 404)
        if not query:
            query = " ".join(words(v["title"])[:3]) or v["title"]
        kind = "shorts" if v["is_short"] else "long"
        pool = [x for x in self._pool(query, kind, 180)["results"] if x["id"] != vid]
        if len(pool) < 8:
            raise ScoutError("not enough niche videos to compare against (%d). Pass a broader query." % len(pool), 404)
        win, rest, rule = split_winners(pool, 2.0)
        checks = []

        def add(name, ok, detail, fix):
            c = {"check": name, "status": "pass" if ok else "fix", "detail": detail}
            if not ok:
                c["fix"] = fix
            checks.append(c)

        L, wl = len(v["title"]), int(med([len(x["title"]) for x in win]))
        add("title length", abs(L - wl) <= max(12, 0.35 * wl), "%d characters, winners median %d" % (L, wl),
            "Aim for about %d characters." % wl)
        qs = stems(query)
        add("keyword in title", not qs or any(w[:5] in qs for w in words(v["title"])), "query: " + query,
            "Put '%s' or close wording early in the title." % query)
        feats = title_features(v["title"])
        missing = [p for p in title_compare(win, rest) if p["lift"] >= 1.5 and p["winners_pct"] >= 30 and not feats[p["pattern"]]]
        add("title patterns", not missing, "winners lean on: " + (", ".join("%s (%d%%)" % (p["pattern"], p["winners_pct"]) for p in missing) or "nothing you lack"),
            "Try working in: " + ", ".join(p["pattern"].replace("_", " ") for p in missing[:3]))
        if self.yt.use_api:
            have = {t.lower() for t in v.get("tags") or []}
            top = [t for t, _ in collections.Counter(t.lower() for x in win for t in (x.get("tags") or [])).most_common(15)]
            hit = [t for t in top if t in have]
            add("tags", len(have) >= 5 and (not top or hit), "%d tags, %d of the niche's top winner tags" % (len(have), len(hit)),
                "Add: " + ", ".join([t for t in top if t not in have][:8]))
        d = v.get("description") or ""
        if v["is_short"]:
            add("description", len(d) >= 50, "%d characters" % len(d), "Write a one-line description with the main keyword.")
        else:
            chapters = bool(re.search(r"(?m)^\s*\(?\d{1,2}:\d{2}", d))
            add("description", len(d) >= 200 and chapters, "%d characters, chapters: %s" % (len(d), "yes" if chapters else "no"),
                "Write 200+ characters with the keyword in the first line, and add chapters (0:00 Intro, ...).")
            wd = med([x["duration"] for x in win if x["duration"]])
            if wd:
                add("video length", 0.5 * wd <= v["duration"] <= 2 * wd, "%s against winners median %s" % (mmss(v["duration"]), mmss(int(wd))),
                    "Winners here run about %s." % mmss(int(wd)))
        if Image is not None:
            try:
                tf, fw = self._thumb(vid), self._thumbs(sorted(win, key=lambda x: -x["multiple"]), 12)
                if fw:
                    b, s = med([f["brightness"] for f in fw]), med([f["saturation"] for f in fw])
                    add("thumbnail", tf["brightness"] >= 0.8 * b and tf["saturation"] >= 0.8 * s,
                        "brightness %.2f vs winners %.2f, saturation %.2f vs %.2f" % (tf["brightness"], b, tf["saturation"], s),
                        "Winners here are brighter and more colorful. Lift exposure and color and keep one clear subject.")
            except ScoutError:
                pass
        perf = None
        try:
            ups = self.yt.recent(self.yt.channel_info(v["channel_id"]), kind, 25)
            m = med([x["views"] for x in ups if x["id"] != vid and (x["age_days"] is None or x["age_days"] >= 3)])
            perf = round(v["views"] / max(m, 1), 1) if m else None
        except ScoutError:
            pass
        return {"video": brief(v), "query": query, "score": round(100 * sum(c["status"] == "pass" for c in checks) / len(checks)),
                "performance_vs_channel_median": perf, "checks": checks, "winner_rule": rule,
                "niche_winners": [brief(x) for x in sorted(win, key=lambda x: -x["multiple"])[:5]], "mode": self.mode()}

    # ---- best time to post
    def timing(self, channels=None, query=None, kind="any", tz=None, n=50):
        if not channels and not query:
            raise ScoutError("give channels or a query")
        try:
            tz = tz or os.environ.get("SCOUT_TZ", "UTC")
            zone = ZoneInfo(tz) if ZoneInfo else dt.timezone(dt.timedelta(hours=5, minutes=30))
        except Exception:
            raise ScoutError("unknown timezone: " + tz)
        infos = [self.yt.channel_info(c) for c in (channels or [])[:10]]
        if query:
            res = self.yt.search(query, 40, "relevance", kind=kind if self.yt.use_api else "any")
            cids = list(dict.fromkeys(v["channel_id"] for v in res["videos"] if v["channel_id"]))[:8]
            got = self.yt.channels_info(cids)
            for c in cids:
                try:
                    infos.append(got.get(c) or self.yt.channel_info(c))
                except ScoutError:
                    continue
        rows = []
        for info in infos:
            ups = self.yt.recent(info, kind, n)
            base = [v for v in ups if v["published"] and (v["age_days"] is None or v["age_days"] >= 3)]
            if len(base) < 5:
                continue
            m = med([v["views"] for v in base]) or 1
            for v in base:
                t = dt.datetime.fromisoformat(v["published"].replace("Z", "+00:00")).astimezone(zone)
                known = self.yt.use_api or not v["published"].endswith("T00:00:00Z")
                rows.append((t.weekday(), t.hour if known else None, v["views"] / max(m, 1)))
        if len(rows) < 10:
            raise ScoutError("not enough dated uploads to read a pattern" + (
                "" if self.yt.use_api else ". yt-dlp channel listings have no dates, set YOUTUBE_API_KEY"), 404)

        def table(key, label):
            groups = collections.defaultdict(list)
            for r in rows:
                if r[key] is not None:
                    groups[r[key]].append(r[2])
            return [{label: (WEEKDAYS[k] if label == "weekday" else k), "uploads": len(xs), "median_multiple": round(med(xs), 2)}
                    for k, xs in sorted(groups.items())]

        days, hours = table(0, "weekday"), table(1, "hour")
        rank = lambda t: sorted([r for r in t if r["uploads"] >= 3], key=lambda r: -r["median_multiple"])[:3]
        notes = ["median_multiple compares each upload to its own channel's median, so big and small channels count equally.",
                 "Use this as a tiebreaker. Topic and packaging matter far more than posting time."]
        if not hours:
            notes.append("Hours need the YouTube API key. yt-dlp only gives the upload date.")
        return {"timezone": tz, "uploads_read": len(rows), "channels_read": len(infos), "by_weekday": days, "by_hour": hours,
                "best_days": rank(days), "best_hours": rank(hours), "mode": self.mode(), "notes": notes}

    # ---- posting calendar
    def calendar(self, channels=None, query=None, niche=None, days=14, per_week=2, kind="any", tz=None):
        if niche and not query:
            cfg = self._niche(niche)
            query, kind = cfg.get("query"), cfg["kind"]
            channels = channels or cfg.get("channels") or None
        t, basis = None, "default (Tue, Thu, Sat)"
        if channels or query:
            try:
                t = self.timing(channels, query, kind, tz)
            except ScoutError:
                t = None
        order = [1, 3, 5, 6, 0, 2, 4]
        if t and t["best_days"]:
            best = [WEEKDAYS.index(d["weekday"]) for d in t["best_days"]]
            order = best + [d for d in order if d not in best]
            basis = "best weekdays from %d uploads" % t["uploads_read"]
        picks = set(order[:max(1, min(int(per_week), 7))])
        hour = t["best_hours"][0]["hour"] if t and t["best_hours"] else None
        ideas = []
        if niche:
            fd = self.feed(niche=niche, limit=40)
            ideas = sorted(fd["items"], key=lambda x: (not x.get("new"), -x.get("multiple", 0)))
        try:
            tz = tz or os.environ.get("SCOUT_TZ", "UTC")
            zone = ZoneInfo(tz) if ZoneInfo else dt.timezone(dt.timedelta(hours=5, minutes=30))
        except Exception:
            raise ScoutError("unknown timezone: " + tz)
        today, slots = dt.datetime.now(zone).date(), []
        for i in range(1, int(days) + 1):
            d = today + dt.timedelta(days=i)
            if d.weekday() in picks:
                idea = ideas[len(slots)] if len(slots) < len(ideas) else None
                slots.append({"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()],
                              "time": "%02d:00" % hour if hour is not None else None,
                              "remix_source": brief(idea) if idea else None})
        return {"slots": slots, "per_week": len(picks), "timezone": tz, "basis": basis,
                "notes": ["remix_source is a proven outlier to remix with the three-angle rule, not a finished topic.",
                          "Steady beats bursts. Keep the same days every week."]}

    # ---- idea seeds for the agent
    def ideas(self, niche=None, query=None, kind="any", n=8, with_hooks=False):
        items, niche_channels = [], None
        if niche:
            cfg = self._niche(niche)
            query, kind = query or cfg.get("query"), cfg["kind"]
            niche_channels = cfg.get("channels")
            items = sorted(self.feed(niche=niche, limit=60)["items"], key=lambda x: (not x.get("new"), -x.get("multiple", 0)))
        if not items:
            chans = niche_channels
            if not query and not chans:
                raise ScoutError("give a niche or a query")
            items = self.outliers(query=query or None, channels=chans if not query else None, kind=kind, max_age_days=60,
                                  min_multiple=3, limit=n, on_topic=bool(query))["results"]
        items = items[:int(n)]
        patterns = None
        try:
            if not query:
                raise ScoutError("no query for patterns")
            w = self.wins(query, kind=kind)
            patterns = {"title_patterns": [p for p in w["title_patterns"] if p["lift"] >= 1.2][:5],
                        "winning_words": [x["word"] for x in w["winning_words"][:8]], "title_length_chars": w["title_length_chars"]}
        except ScoutError:
            pass
        seeds = []
        for it in items:
            s = {"source": brief(it)}
            if with_hooks and it.get("id"):
                try:
                    s["hook_first_30s"] = self.yt.transcript(it["id"])["hook_first_30s"]
                except ScoutError:
                    s["hook_first_30s"] = None
            s["angles"] = {"A_reframe_trait": None, "B_new_format": None, "C_stack_second_subject": None, "pick": None, "why": None}
            seeds.append(s)
        return {"niche": niche, "query": query, "seeds": seeds, "niche_patterns": patterns, "instructions": IDEA_BRIEF}

    # ---- your own channels and pages (scout_mine)
    def me_channels(self):
        return {**self.mine.channels(), **self.mine.ig_accounts()}

    def me_race(self, channel):
        return self.mine.race(channel)

    def me_scorecard(self, channel):
        return self.mine.scorecard(channel)

    def me_achievements(self, channel):
        return self.mine.achievements(channel)

    def me_audience(self, channel, days=28):
        return self.mine.audience(channel, days)

    def me_ig(self, account=None, days=28, reels=25):
        return self.mine.ig_insights(account, days, reels)

    # ---- X and Facebook (scout_social)
    def x_post(self, ref):
        return self.x.post(ref)

    def x_posts(self, refs):
        return self.x.posts(refs)

    def x_profile(self, user):
        return self.x.profile(user)

    def x_outliers(self, users, min_multiple=2.0, max_age_days=30, limit=30):
        return self.x.outliers(users, min_multiple, max_age_days, limit)

    def x_search(self, query, limit=30):
        return self.x.search(query, limit)

    def me_fb_pages(self):
        return self.fb.pages()

    def me_fb(self, page, days=28):
        return self.fb.page(page, days)

    def usage(self):
        u = super().usage()
        u.update({"bot_posts": self.bots.stats(), "x_api": self.x.has_api, "x_posts_read_today": self.cache.usage("x_posts"), "x_daily_post_cap": self.x.daily_posts,
                  "x_estimated_cost_today_usd": round(self.cache.usage("x_posts") * 0.005 + self.cache.usage("x_users") * 0.01, 2)})
        return u

    # ---- posts from your own scroll bots (X, LinkedIn, Reddit)
    def social_ingest(self, platform, posts):
        return self.bots.ingest(platform, posts)

    def social_outliers(self, platform="x", authors=None, communities=None, min_multiple=2.0, max_age_days=30, limit=30, by=None):   # by: kept, the server route passes it
        return self.bots.outliers(platform, authors, communities, min_multiple, max_age_days, limit, by=by)

    def social_top(self, platform="x", days=7, metric="likes", limit=30, authors=None, communities=None):
        return self.bots.top(platform, days, metric, limit, authors, communities)

    def social_search(self, query, platform=None, communities=None, days=30, limit=30):   # 12 Nov 2026: Algolia free tier, falls back to Scout's own database (kept on 14 Nov)
        import scout_algolia
        return scout_algolia.search(query, platform, communities, days, limit)
