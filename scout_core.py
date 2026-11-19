#!/usr/bin/env python3
"""Scout core: your own YouTube and Instagram research layer. No vidIQ, no paid third party.

Data sources
  YouTube : YouTube Data API v3 when YOUTUBE_API_KEY is set (exact ages, subscriber counts, result totals),
            otherwise yt-dlp (public pages, no key, fewer fields).
  Instagram: official Business Discovery API through your own token pool.
Stdlib only. yt-dlp is optional but needed for transcripts.
"""
import glob
import json
import sys
import math
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import datetime as dt
import urllib.error
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
UA = "scout/2.0"


class ScoutError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


class HttpErr(Exception):
    def __init__(self, code, body):
        super().__init__(f"http {code}")
        self.code, self.body = code, body


class Config:
    def __init__(self, **kw):
        e = os.environ.get
        self.yt_key = kw.get("yt_key", e("YOUTUBE_API_KEY", ""))
        # 14 Nov 2026 (bulletproof): extra keys YOUTUBE_API_KEY_2 .. _9, each with its own daily budget; one caller may use at most
        # SCOUT_YT_CALLER_PCT of the total, and when every key is spent Kestrel falls back to yt-dlp instead of failing.
        self.yt_keys = kw.get("yt_keys") or [k for k in [self.yt_key] + [e("YOUTUBE_API_KEY_%d" % i, "") for i in range(2, 10)] if k]
        self.yt_caller_pct = int(kw.get("yt_caller_pct", e("SCOUT_YT_CALLER_PCT", "30")))
        self.yt_budget = int(kw.get("yt_budget", e("SCOUT_YT_DAILY_UNITS", "8000")))
        self.ytdlp = kw.get("ytdlp", e("YTDLP") or shutil.which("yt-dlp") or os.path.join(HOME, ".local/bin/yt-dlp"))
        self.proxy = kw.get("proxy", e("YT_PROXY", ""))
        self.dlp_pause = float(kw.get("dlp_pause", e("SCOUT_DLP_PAUSE", "1.0")))
        self.db = kw.get("db", e("SCOUT_DB", os.path.join(HOME, ".scout", "cache.sqlite")))
        self.ig_pool = kw.get("ig_pool", e("SCOUT_IG_POOL", ""))
        self.ig_seeds = kw.get("ig_seeds", e("SCOUT_IG_SEEDS", ""))
        self.ig_stop_pct = int(kw.get("ig_stop_pct", e("SCOUT_IG_STOP_PCT", "80")))
        self.ig_version = kw.get("ig_version", e("SCOUT_IG_GRAPH_VERSION", "v26.0"))


def _outside_script():
    """First file in the call stack that isn't part of Kestrel (and isn't the stdlib), else the running script."""
    import traceback
    mine = {"scout_core.py", "scout_plus.py", "scout_extras.py", "scout_growth.py", "scout_mine.py", "scout_social.py", "scout_jobs.py",
            "scout_server.py", "scout_remote_mcp.py", "threading.py", "socketserver.py", "server.py"}
    for fr in reversed(traceback.extract_stack()[:-2]):
        base = os.path.basename(fr.filename)
        if base not in mine and "/lib/python" not in fr.filename:
            return "%s:%s" % (base, fr.name)
    return os.path.basename(sys.argv[0] or "python") or "python"


CALLER = threading.local()   # .name = X-Scout-Caller header or the route, set per request by scout_server

# ---------------------------------------------------------------- small helpers

def http_json(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf8", "ignore")), r.headers
    except urllib.error.HTTPError as e:
        raise HttpErr(e.code, e.read().decode("utf8", "ignore")[:400])
    except (urllib.error.URLError, TimeoutError) as e:
        raise ScoutError("network error: " + str(getattr(e, "reason", e)), 502)


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def parse_duration(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, sec = [int(x or 0) for x in m.groups()]
    return d * 86400 + h * 3600 + mi * 60 + sec


def age_days(iso):
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return max((dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400, 0.0)


def video_id(ref):
    ref = (ref or "").strip()
    if re.fullmatch(r"[\w-]{11}", ref):
        return ref
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})", ref)
    if not m:
        raise ScoutError("cannot read a YouTube video id from: " + ref[:80])
    return m.group(1)


def parse_channel_ref(ref):
    ref = (ref or "").strip()
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", ref)
    if m:
        return "id", m.group(1)
    m = re.search(r"youtube\.com/(@[\w.\-]+)", ref)
    if m:
        return "handle", m.group(1)
    if re.fullmatch(r"UC[\w-]{22}", ref):
        return "id", ref
    if ref.startswith("@"):
        return "handle", ref
    if re.fullmatch(r"[\w.\-]+", ref):
        return "handle", "@" + ref
    raise ScoutError("cannot read a channel reference from: " + ref[:80])


# ---------------------------------------------------------------- cache and usage

class Cache:
    def __init__(self, path):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("pragma journal_mode=wal")
            self.db.execute("create table if not exists cache(k text primary key, v text, exp real)")
            self.db.execute("create table if not exists usage(day text, name text, n integer, primary key(day, name))")
            self.db.commit()

    def get(self, k):
        with self.lock:
            r = self.db.execute("select v, exp from cache where k=?", (k,)).fetchone()
        if r and r[1] > time.time():
            return json.loads(r[0])
        return None

    def put(self, k, v, ttl):
        with self.lock:
            self.db.execute("insert or replace into cache values(?,?,?)", (k, json.dumps(v), time.time() + ttl))
            self.db.commit()

    def cached(self, k, ttl, fn):
        v = self.get(k)
        if v is None:
            v = fn()
            self.put(k, v, ttl)
        return v

    def add_usage(self, name, n):
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        with self.lock:
            self.db.execute("insert into usage values(?,?,?) on conflict(day,name) do update set n=n+excluded.n", (day, name, n))
            self.db.commit()

    def usage(self, name):
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        with self.lock:
            r = self.db.execute("select n from usage where day=? and name=?", (day, name)).fetchone()
        return r[0] if r else 0


# ---------------------------------------------------------------- YouTube

class YouTube:
    def __init__(self, cfg, cache):
        self.cfg, self.cache = cfg, cache

    @property
    def use_api(self):
        """True while a key has budget left AND this caller is under its share. Otherwise every method takes its yt-dlp path."""
        return self._key_for(1) is not None

    def _caller(self):
        """X-Scout-Caller from the request, or for code that imports Kestrel directly (no request), the script that
        called in: "anon:<script>" so it shows up by name in /v1/usage instead of hiding as "anon"."""
        name = getattr(CALLER, "name", None)
        if name:
            return name
        return "anon:" + _outside_script()

    def _key_for(self, units):
        keys = self.cfg.yt_keys
        if not keys: return None
        who = self._caller()
        cap = self.cfg.yt_budget * len(keys) * self.cfg.yt_caller_pct / 100.0
        if self.cache.usage("ytc:" + who) + units > cap: return None
        if who.startswith("anon"):   # every caller without a name shares one small budget (SCOUT_YT_ANON_PCT, default 10%)
            anon_cap = self.cfg.yt_budget * len(keys) * float(os.environ.get("SCOUT_YT_ANON_PCT", "10")) / 100.0
            if self.cache.usage("ytc_anon_total") + units > anon_cap: return None
        for i, k in enumerate(keys):
            if self.cache.usage("yt_units:%d" % i) + units <= self.cfg.yt_budget:
                return i, k
        return None

    # ---- low level
    def api(self, endpoint, units, **params):
        for _ in range(len(self.cfg.yt_keys) or 1):
            pick = self._key_for(units)
            if not pick:
                raise ScoutError("YouTube API budget used up for this caller or every key today; retry and Kestrel will use yt-dlp.", 429)
            i, key = pick
            params["key"] = key
            try:
                data, _ = http_json("https://www.googleapis.com/youtube/v3/%s?%s" % (endpoint, urllib.parse.urlencode(params)))
            except HttpErr as e:
                if e.code in (403, 429) and ("quota" in e.body.lower() or "rate" in e.body.lower()):
                    self.cache.add_usage("yt_units:%d" % i, self.cfg.yt_budget)   # Google says this key is spent: skip it until tomorrow
                    continue
                raise ScoutError("YouTube API %s: %s" % (e.code, e.body[:200]), 429 if e.code in (403, 429) else 502)
            self.cache.add_usage("yt_units:%d" % i, units)
            self.cache.add_usage("yt_units", units)
            who = self._caller()
            self.cache.add_usage("ytc:" + who, units)
            if who.startswith("anon"):
                self.cache.add_usage("ytanon:%s:%s" % (who, endpoint), units)
                self.cache.add_usage("ytc_anon_total", units)
            return data
        raise ScoutError("every YouTube key is out of quota today; retry and Kestrel will use yt-dlp.", 429)

    def dlp(self, args, timeout=150):
        if not (os.path.exists(self.cfg.ytdlp) or shutil.which(self.cfg.ytdlp)):
            raise ScoutError("yt-dlp not found. Install it or set YTDLP, or set YOUTUBE_API_KEY.", 501)
        cmd = [self.cfg.ytdlp, "--no-warnings", "--ignore-errors"]
        if self.cfg.proxy:
            cmd += ["--proxy", self.cfg.proxy]
        try:
            r = subprocess.run(cmd + args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ScoutError("yt-dlp timed out", 504)
        time.sleep(self.cfg.dlp_pause)
        rows = []
        for line in r.stdout.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        return rows

    # ---- normalisers
    def _norm_api(self, it):
        sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
        dur = parse_duration(cd.get("duration"))
        pub = sn.get("publishedAt")
        a = age_days(pub)
        return {"id": it["id"], "title": sn.get("title", ""), "views": int(st.get("viewCount", 0) or 0),
                "likes": int(st.get("likeCount", 0) or 0), "comments": int(st.get("commentCount", 0) or 0),
                "duration": dur, "is_short": 0 < dur <= 180, "published": pub,
                "age_days": round(a, 1) if a is not None else None,
                "channel": sn.get("channelTitle", ""), "channel_id": sn.get("channelId", ""),
                "tags": sn.get("tags") or [], "description": (sn.get("description") or "")[:3000],
                "url": "https://www.youtube.com/watch?v=" + it["id"],
                "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % it["id"]}

    def _norm_dlp(self, d):
        vid = d.get("id")
        dur = int(d.get("duration") or 0)
        pub, ud = None, d.get("upload_date")
        if ud and len(str(ud)) == 8:
            ud = str(ud)
            pub = "%s-%s-%sT00:00:00Z" % (ud[:4], ud[4:6], ud[6:])
        elif d.get("timestamp"):
            pub = dt.datetime.fromtimestamp(d["timestamp"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        a = age_days(pub)
        return {"id": vid, "title": d.get("title") or "", "views": int(d.get("view_count") or 0),
                "likes": int(d.get("like_count") or 0), "comments": int(d.get("comment_count") or 0),
                "duration": dur, "is_short": 0 < dur <= 180, "published": pub,
                "age_days": round(a, 1) if a is not None else None,
                "channel": d.get("channel") or d.get("uploader") or "", "channel_id": d.get("channel_id") or "",
                "tags": d.get("tags") or [], "description": (d.get("description") or "")[:3000],
                "url": "https://www.youtube.com/watch?v=" + str(vid),
                "thumbnail": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid}

    @staticmethod
    def _kind_ok(v, kind):
        if kind == "shorts":
            return v["is_short"]
        if kind == "long":
            return v["duration"] > 180
        return True

    # ---- videos
    def videos(self, ids):
        ids = [i for i in ids if i]
        out = {}
        if self.use_api:
            for i in range(0, len(ids), 50):
                chunk = ids[i:i + 50]
                data = self.cache.cached("vids:" + ",".join(chunk), 3 * 3600, lambda c=chunk: self.api(
                    "videos", 1, part="snippet,statistics,contentDetails", id=",".join(c)))
                for it in data.get("items", []):
                    out[it["id"]] = self._norm_api(it)
        else:
            for vid in ids:
                rows = self.cache.cached("vid:" + vid, 6 * 3600, lambda v=vid: self.dlp(
                    ["-j", "--skip-download", "https://www.youtube.com/watch?v=" + v]))
                if rows:
                    out[vid] = self._norm_dlp(rows[0])
        return [out[i] for i in ids if i in out]

    # ---- channels
    def _norm_channel(self, it):
        sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
        subs = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0) or 0)
        cid = it["id"]
        return {"id": cid, "title": sn.get("title", ""), "handle": sn.get("customUrl", ""),
                "subscribers": subs, "total_views": int(st.get("viewCount", 0) or 0),
                "video_count": int(st.get("videoCount", 0) or 0),
                "created": sn.get("publishedAt"), "country": sn.get("country"),
                "uploads_playlist": (cd.get("relatedPlaylists", {}) or {}).get("uploads") or "UU" + cid[2:],
                "url": "https://www.youtube.com/channel/" + cid}

    def channel_info(self, ref):
        kind, val = parse_channel_ref(ref)

        def go():
            if self.use_api:
                p = {"part": "snippet,statistics,contentDetails"}
                p["id" if kind == "id" else "forHandle"] = val
                items = self.api("channels", 1, **p).get("items")
                if not items:
                    raise ScoutError("channel not found: " + ref, 404)
                return self._norm_channel(items[0])
            url = ("https://www.youtube.com/channel/" + val) if kind == "id" else ("https://www.youtube.com/" + val)
            rows = self.dlp(["-J", "--flat-playlist", "--playlist-end", "1", url + "/videos"])
            if not rows:
                raise ScoutError("channel not found: " + ref, 404)
            d = rows[0]
            cid = d.get("channel_id") or (val if kind == "id" else "")
            return {"id": cid, "title": d.get("channel") or d.get("uploader") or "", "handle": val if kind == "handle" else "",
                    "subscribers": d.get("channel_follower_count"), "total_views": None, "video_count": None,
                    "created": None, "country": None, "uploads_playlist": None,
                    "url": ("https://www.youtube.com/channel/" + cid) if cid else url}

        return self.cache.cached("ch:%s:%s" % (kind, val.lower()), 3 * 3600, go)

    def channels_info(self, ids):
        """Batch lookup by channel id. API only; yt-dlp mode returns {} (channel size unknown)."""
        out = {}
        if not self.use_api:
            return out
        ids = [i for i in dict.fromkeys(ids) if i]
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            data = self.cache.cached("chs:" + ",".join(chunk), 3 * 3600, lambda c=chunk: self.api(
                "channels", 1, part="snippet,statistics,contentDetails", id=",".join(c)))
            for it in data.get("items", []):
                out[it["id"]] = self._norm_channel(it)
        return out

    def recent(self, ch, kind="any", n=30):
        def go():
            if self.use_api:
                got, token, pages = [], None, 0
                while len(got) < n and pages < 3:
                    p = {"part": "contentDetails", "playlistId": ch["uploads_playlist"], "maxResults": 50}
                    if token:
                        p["pageToken"] = token
                    data = self.api("playlistItems", 1, **p)
                    ids = [it["contentDetails"]["videoId"] for it in data.get("items", [])]
                    got += [v for v in self.videos(ids) if self._kind_ok(v, kind)]
                    token, pages = data.get("nextPageToken"), pages + 1
                    if not token:
                        break
                return got[:n]
            tab = "shorts" if kind == "shorts" else "videos"
            rows = self.dlp(["-j", "--flat-playlist", "--playlist-end", str(n), ch["url"].rstrip("/") + "/" + tab])
            vids = [self._norm_dlp(d) for d in rows if d.get("id")]
            for v in vids:
                v["channel"] = v["channel"] or ch.get("title", "")
                v["channel_id"] = v["channel_id"] or ch.get("id", "")
                if kind == "shorts":
                    v["is_short"] = True
            return [v for v in vids if v["views"] and self._kind_ok(v, kind)] if kind != "shorts" else [v for v in vids if v["views"]]

        return self.cache.cached("recent:%s:%s:%d" % (ch.get("id") or ch["url"], kind, n), 3 * 3600, go)

    # ---- search
    def search(self, q, n=30, order="relevance", days=None, kind="any"):
        def go():
            if self.use_api:
                p = {"part": "snippet", "type": "video", "q": q, "maxResults": min(n, 50), "order": order}
                if days:
                    p["publishedAfter"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
                if kind == "shorts":
                    p["videoDuration"] = "short"
                data = self.api("search", 100, **p)
                ids = [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
                vids = self.videos(ids)
                total = (data.get("pageInfo") or {}).get("totalResults")
            else:
                prefix = "ytsearchdate" if order == "date" else "ytsearch"
                rows = self.dlp(["-j", "--flat-playlist", "%s%d:%s" % (prefix, n, q)])
                vids = [self._norm_dlp(d) for d in rows if d.get("id")]
                if order == "viewCount":
                    vids.sort(key=lambda v: -v["views"])
                total = None
            return {"videos": [v for v in vids if self._kind_ok(v, kind)], "total_estimate": total}

        return self.cache.cached("search:%s:%d:%s:%s:%s" % (q.lower(), n, order, days, kind), 6 * 3600, go)

    def autocomplete(self, seed):
        def go():
            url = "https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q=" + urllib.parse.quote(seed)
            try:
                data, _ = http_json(url, timeout=15)
            except (HttpErr, ScoutError):
                return []
            return [s for s in (data[1] if isinstance(data, list) and len(data) > 1 else []) if isinstance(s, str)]
        return self.cache.cached("ac:" + seed.lower(), 24 * 3600, go)

    # ---- transcript
    def transcript(self, vid, lang="en"):
        def go():
            with tempfile.TemporaryDirectory() as d:
                self.dlp(["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "%s.*,%s" % (lang, lang),
                          "--sub-format", "json3", "-o", os.path.join(d, "%(id)s"), "https://www.youtube.com/watch?v=" + vid], 180)
                files = sorted(glob.glob(os.path.join(d, "*.json3")))
                if not files:
                    raise ScoutError("no captions found for this video", 404)
                with open(files[0], encoding="utf8") as f:
                    return parse_json3(json.load(f))
        return self.cache.cached("tr:%s:%s" % (vid, lang), 30 * 86400, go)


def parse_json3(data):
    segs = []
    for ev in data.get("events", []):
        txt = "".join(s.get("utf8", "") for s in ev.get("segs", []) or []).replace("\n", " ").strip()
        if txt:
            segs.append({"start": round((ev.get("tStartMs") or 0) / 1000, 1), "text": txt})
    full = " ".join(s["text"] for s in segs)
    words = len(full.split())
    last = segs[-1]["start"] if segs else 0
    return {"segments": segs, "text": full, "words": words,
            "words_per_minute": round(words / (last / 60), 0) if last > 30 else None,
            "hook_first_30s": " ".join(s["text"] for s in segs if s["start"] <= 30)}


# ---------------------------------------------------------------- Instagram

def load_pool(paths):
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf8") as f:
                d = json.load(f)
        except ValueError:
            continue
        items = []
        if isinstance(d, list):
            items = [(None, x) for x in d]
        elif isinstance(d, dict) and ("token" in d or "access_token" in d):
            items = [(None, d)]
        elif isinstance(d, dict):
            items = list(d.items())
        out = []
        for i, (k, x) in enumerate(items):
            if not isinstance(x, dict):
                continue
            tok, uid = x.get("token") or x.get("access_token"), x.get("ig_user_id") or x.get("id")
            if tok and uid:
                out.append({"label": str(x.get("label") or x.get("name") or k or "acct%d" % (i + 1)), "token": tok, "ig_user_id": str(uid)})
        if out:
            return out
    return []


class Instagram:
    FIELDS = ("business_discovery.username({u}){{followers_count,media_count,name,"
              "media.limit(50){{caption,like_count,comments_count,view_count,media_product_type,media_type,permalink,timestamp}}}}")
    FIELDS_NO_VIEWS = FIELDS.replace("view_count,", "")   # for Graph versions or apps that don't serve view_count yet

    def __init__(self, cfg, cache):
        self.cfg, self.cache = cfg, cache

    def pool(self):
        if os.environ.get("SCOUT_IG_TOKEN") and os.environ.get("SCOUT_IG_USER_ID"):
            return [{"label": os.environ.get("SCOUT_IG_APP", "default"), "token": os.environ["SCOUT_IG_TOKEN"],
                     "ig_user_id": os.environ["SCOUT_IG_USER_ID"]}]
        return load_pool([self.cfg.ig_pool, os.path.join(HOME, ".scout", "ig_pool.json"),
                          os.path.join(HOME, ".kestrel-ig-discovery-pool.json"), os.path.join(HOME, ".kestrel-ig-discovery.json")])

    def seeds(self):
        for p in (self.cfg.ig_seeds, os.path.join(HOME, ".scout", "ig_seeds.json")):
            if p and os.path.exists(p):
                try:
                    with open(p, encoding="utf8") as f:
                        return json.load(f)
                except ValueError:
                    pass
        return {}

    def state(self, label):
        return self.cache.get("igstate:" + label) or {}

    def status(self):
        now = time.time()
        return [{"label": t["label"], "usage_pct": self.state(t["label"]).get("pct"),
                 "paused_minutes": max(0, round((self.state(t["label"]).get("pause_until", 0) - now) / 60)),
                 "invalid": bool(self.state(t["label"]).get("invalid"))} for t in self.pool()]

    def _usage_pct(self, hdr):
        try:
            u = json.loads(hdr.get("x-app-usage") or "{}")
            return max([u.get("call_count", 0), u.get("total_time", 0), u.get("total_cputime", 0)])
        except ValueError:
            return None

    def profile(self, username):
        username = username.strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-z0-9._]{1,30}", username):
            raise ScoutError("that doesn't look like an Instagram username", 400)
        try:
            return self.cache.cached("igp2:" + username, 6 * 3600, lambda: self._fetch(username))   # igp2: answers that include view_count
        except ScoutError as e:
            old = self.cache.get("igp_last:" + username) if e.status in (401, 429) else None
            if old: return {**old, "stale": True}
            raise

    def _fetch(self, username):
        pool = self.pool()
        if not pool:
            raise ScoutError("no Instagram token found. " + ("Add it in the plugin settings (/config, Kestrel). Say \"set up Kestrel\" for the steps."
                             if os.environ.get("SCOUT_CALLER") == "plugin" else "Set SCOUT_IG_TOKEN and SCOUT_IG_USER_ID (or SCOUT_IG_POOL) in your .env."), 501)
        now = time.time()
        # 14 Nov: Business Discovery counts against the Meta app's hourly Platform limit (200 x app users), shared by every
        # token from that app. Spend at most SCOUT_IG_PER_HOUR calls per clock hour per app so bursts (daily run, autopilot,
        # watch) can't push the whole pool over; past the cap the caller gets the last good answer (stale) or a 429.
        # Per-app override: SCOUT_IG_PER_HOUR_<APP> (e.g. SCOUT_IG_PER_HOUR_APP1=50 keeps headroom for other tools on the same app).
        default_cap = int(os.environ.get("SCOUT_IG_PER_HOUR", "120"))
        cap_for = lambda a: int(os.environ.get("SCOUT_IG_PER_HOUR_" + re.sub(r"[^A-Z0-9]", "_", a.upper()), default_cap))
        hour = time.strftime("%Y%m%d%H", time.gmtime(now))
        apps = {t.get("app") or "default" for t in pool}
        spent = {a: self.cache.usage("ig_hour:%s:%s" % (a, hour)) for a in apps}
        left = {a: cap_for(a) - spent[a] for a in apps}
        pool = [t for t in pool if left[t.get("app") or "default"] > 0]
        if not pool:
            raise ScoutError("Instagram hourly budget used on every app (%s). Serving cached data until the next hour."
                             % ", ".join("%s %d" % (a, cap_for(a)) for a in sorted(apps)), 429)
        order = sorted(pool, key=lambda t: (-left[t.get("app") or "default"], self.state(t["label"]).get("pct") or 0))
        last = None
        for t in order:
            st = self.state(t["label"])
            if st.get("invalid") or st.get("pause_until", 0) > now:
                continue
            if (st.get("pct") or 0) >= self.cfg.ig_stop_pct and now - st.get("at", 0) < 3600:
                continue
            fields = self.FIELDS_NO_VIEWS if self.cache.get("ig_no_view_count") else self.FIELDS
            q = urllib.parse.urlencode({"fields": fields.format(u=username), "access_token": t["token"]})
            url = "https://graph.facebook.com/%s/%s?%s" % (self.cfg.ig_version, t["ig_user_id"], q)
            try:
                data, hdr = http_json(url)
            except HttpErr as e:
                try:
                    err = json.loads(e.body).get("error", {})
                except ValueError:
                    err = {}
                code, msg = err.get("code"), err.get("message", e.body[:120])
                if code in (4, 17, 32, 613) or e.code == 429:
                    self.cache.put("igstate:" + t["label"], {**st, "pause_until": now + 3600, "at": now}, 86400)
                    last = ScoutError("Instagram rate limit on %s, paused 1h" % t["label"], 429)
                    continue
                if code in (190, 102):
                    self.cache.put("igstate:" + t["label"], {**st, "invalid": True, "at": now}, 86400)
                    last = ScoutError("Instagram token %s is invalid or expired" % t["label"], 401)
                    continue
                if "view_count" in str(msg) and fields is self.FIELDS:   # this app/version can't read views: remember, retry without
                    self.cache.put("ig_no_view_count", True, 86400)
                    return self._fetch(username)
                raise ScoutError("Instagram: %s (is @%s a Business or Creator account?)" % (msg, username), 404)
            pct = self._usage_pct(hdr)
            self.cache.put("igstate:" + t["label"], {"pct": pct, "at": now, "pause_until": st.get("pause_until", 0)}, 86400)
            self.cache.add_usage("ig_calls", 1)
            self.cache.add_usage("ig_hour:%s:%s" % (t.get("app") or "default", hour), 1)
            bd = data.get("business_discovery") or {}
            media = (bd.get("media") or {}).get("data", [])
            out = {"username": username, "name": bd.get("name"), "followers": bd.get("followers_count"),
                   "media_count": bd.get("media_count"), "media": media, "via": t["label"]}
            self.cache.put("igp_last:" + username, out, 30 * 86400)
            return out
        raise last or ScoutError("Instagram quota is high on every token, serving cached data only. Try later or add accounts.", 429)

    def outliers(self, users=None, niche=None, min_multiple=1.5, max_age_days=60, limit=30, max_users=15):
        notes = ["Reels are scored against that account's own median: by views where Meta returns them, otherwise by likes (see scored_by)."]
        if niche:
            users = self.seeds().get(niche)
            if not users:
                raise ScoutError("no Instagram seeds for niche '%s'. Add them to ~/.scout/ig_seeds.json" % niche, 404)
        if not users:
            raise ScoutError("give users or niche")
        users = list(dict.fromkeys(u.strip().lstrip("@").lower() for u in users if u.strip()))
        if len(users) > max_users:
            notes.append("only the first %d accounts were read" % max_users)
            users = users[:max_users]
        rows, errors = [], []
        for u in users:
            try:
                p = self.profile(u)
            except ScoutError as e:
                errors.append({"user": u, "error": str(e)})
                if e.status in (429, 501):
                    break
                continue
            reels = [m for m in p["media"] if m.get("media_product_type") == "REELS" and m.get("like_count") is not None]
            if len(reels) < 5:
                errors.append({"user": u, "error": "fewer than 5 reels, baseline too thin"})
                continue
            key = "view_count" if sum(1 for x in reels if x.get("view_count")) >= 0.6 * len(reels) else "like_count"
            for m in reels:
                a = age_days(m.get("timestamp", "").replace("+0000", "+00:00"))
                if a is not None and a > max_age_days or m.get(key) is None:
                    continue
                others = [x[key] for x in reels if x["permalink"] != m["permalink"] and x.get(key) is not None]
                base = med(others) or 1
                mult = m[key] / max(base, 1)
                if mult < min_multiple:
                    continue
                rows.append({"url": m["permalink"], "account": "@" + u, "followers": p["followers"],
                             "caption": " ".join((m.get("caption") or "").split())[:200], "likes": m["like_count"],
                             "comments": m.get("comments_count"), "views": m.get("view_count"), "scored_by": key.split("_")[0] + "s",
                             "account_median_likes" if key == "like_count" else "account_median_views": int(base), "multiple": round(mult, 1),
                             "likes_per_follower_pct": round(100 * m["like_count"] / p["followers"], 2) if p.get("followers") else None,
                             "age_days": round(a, 1) if a is not None else None, "posted": m.get("timestamp")})
        rows.sort(key=lambda r: -r["multiple"])
        return {"results": rows[:limit], "accounts_read": len(users) - len(errors), "errors": errors, "notes": notes}


# ---------------------------------------------------------------- the product

class Scout:
    def __init__(self, cfg=None):
        self.cfg = cfg or Config()
        self.cache = Cache(self.cfg.db)
        self.yt = YouTube(self.cfg, self.cache)
        self.ig = Instagram(self.cfg, self.cache)

    def mode(self):
        return "youtube-data-api" if self.yt.use_api else "yt-dlp (set YOUTUBE_API_KEY for exact ages and channel sizes)"

    # ---- outliers (YouTube long, Shorts)
    def outliers(self, query=None, channels=None, kind="any", max_age_days=90, min_multiple=2.0, min_subs=None,
                 max_subs=None, limit=30, per_channel=25, max_channels=12, search_n=40, on_topic=True):
        if not query and not channels:
            raise ScoutError("give a query or a list of channels")
        yt, notes = self.yt, []
        infos = {}
        for ref in (channels or [])[:max_channels]:
            i = yt.channel_info(ref)
            infos[i["id"] or i["url"]] = i
        if query:
            # yt-dlp search results are mostly long videos, so find channels from all results and read their Shorts tab later
            res = yt.search(query, search_n, "relevance", days=max_age_days if yt.use_api else None, kind=kind if yt.use_api else "any")
            hits, names = {}, {}
            for v in res["videos"]:
                if v["channel_id"]:
                    hits[v["channel_id"]] = hits.get(v["channel_id"], 0) + 1
                    names[v["channel_id"]] = v["channel"]
            top = sorted(hits, key=lambda c: -hits[c])[:max_channels]
            batch = yt.channels_info(top)
            for cid in top:
                infos.setdefault(cid, batch.get(cid) or {"id": cid, "title": names[cid], "subscribers": None,
                                                           "uploads_playlist": "UU" + cid[2:], "url": "https://www.youtube.com/channel/" + cid})
        out, scanned, size_unknown, dropped = [], 0, False, 0
        stop = {"the", "and", "for", "with", "how", "to", "a", "of", "in", "on", "my", "your", "best"}
        stems = [w[:5] for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) >= 3 and w not in stop]
        topic_on = bool(on_topic and query and stems)
        for cid, info in infos.items():
            subs = info.get("subscribers")
            if (min_subs or max_subs) and subs is None:
                size_unknown = True
            elif subs is not None and ((min_subs and subs < min_subs) or (max_subs and subs > max_subs)):
                continue
            ups = yt.recent(info, kind, per_channel)
            base = [v for v in ups if v["age_days"] is None or v["age_days"] >= 3]
            if len(base) < 5:
                notes.append("skipped %s: fewer than 5 older uploads, baseline too thin" % (info.get("title") or cid))
                continue
            scanned += 1
            for v in ups:
                if v["age_days"] is not None and v["age_days"] > max_age_days:
                    continue
                m = med([b["views"] for b in base if b["id"] != v["id"]]) or 1
                mult = v["views"] / max(m, 1)
                if mult < min_multiple:
                    continue
                if topic_on:
                    words = re.findall(r"[a-z0-9]+", (v.get("title") or "").lower())
                    if not any(w[:5] in stems for w in words):
                        dropped += 1
                        continue
                out.append({**v, "multiple": round(mult, 1), "channel_median_views": int(m), "channel_subscribers": subs,
                            "views_per_day": round(v["views"] / max(v["age_days"], 1), 1) if v["age_days"] is not None else None})
        if dropped:
            notes.append("%d outliers dropped because their titles don't match the query. Pass on_topic=false to keep them." % dropped)
        if size_unknown:
            notes.append("channel size filter not applied to channels with unknown subscriber count")
        if not yt.use_api:
            notes.append("yt-dlp mode: ages may be missing and channel sizes unknown. Set YOUTUBE_API_KEY for full filters.")
        out.sort(key=lambda r: -r["multiple"])
        seen, uniq = set(), []
        for r in out:
            if r["id"] not in seen:
                seen.add(r["id"])
                uniq.append(r)
        return {"results": uniq[:limit], "channels_scanned": scanned, "mode": self.mode(), "notes": notes}

    # ---- channel stats
    def channel_stats(self, ref, kind="any", n=30):
        info = self.yt.channel_info(ref)
        ups = self.yt.recent(info, kind, n)
        if not ups:
            raise ScoutError("no uploads found for this channel and kind", 404)
        views = [v["views"] for v in ups]
        m = med(views)
        ages = [v["age_days"] for v in ups if v["age_days"] is not None]
        per_week = None
        if len(ages) >= 2 and max(ages) - min(ages) > 0:
            per_week = round((len(ages) - 1) / (max(ages) - min(ages)) * 7, 1)
        subs = info.get("subscribers")
        return {"channel": info, "sample_size": len(ups), "kind": kind,
                "median_views": int(m), "mean_views": int(statistics.mean(views)), "best_views": max(views),
                "uploads_per_week": per_week,
                "hit_rate_2x_pct": round(100 * sum(1 for x in views if x >= 2 * m) / len(views), 1),
                "median_views_per_subscriber": round(m / subs, 3) if subs else None,
                "consistency_median_over_mean": round(m / max(statistics.mean(views), 1), 2),
                "top": sorted(ups, key=lambda v: -v["views"])[:5],
                "latest": sorted(ups, key=lambda v: v["age_days"] if v["age_days"] is not None else 9e9)[:5],
                "mode": self.mode()}

    # ---- keyword demand vs supply
    def keyword_demand(self, seed, terms=6, kind="any"):
        sugg = [s for s in self.yt.autocomplete(seed) if s.lower() != seed.lower()]
        listing = [seed] + sugg[:max(terms - 1, 0)]
        rows = []
        for t in listing:
            res = self.yt.search(t, 20, "relevance", None, kind)
            vids = res["videos"]
            if not vids:
                continue
            vpd = [v["views"] / max(v["age_days"], 1) for v in vids if v["age_days"] is not None]
            unit = "views_per_day" if vpd else "views_total"
            demand = med(vpd) if vpd else med([v["views"] for v in vids])
            total = res["total_estimate"]
            channels = {v["channel_id"] for v in vids if v["channel_id"]}
            small = None
            if self.yt.use_api and channels:
                info = self.yt.channels_info(list(channels))
                subs = [info[c]["subscribers"] for c in channels if c in info and info[c]["subscribers"] is not None]
                small = round(100 * sum(1 for s in subs if s < 10000) / len(subs), 1) if subs else None
            basis = total if total else len(channels)
            rows.append({"term": t, "demand": round(demand, 1) if demand else 0, "demand_unit": unit,
                         "supply_results_estimate": total, "distinct_channels_in_top20": len(channels),
                         "small_channel_share_pct": small,
                         "opportunity": round((demand or 0) / math.log10(basis + 10), 1),
                         "basis": "result_total" if total else "channel_count"})
        rows.sort(key=lambda r: -r["opportunity"])
        return {"seed": seed, "terms": rows, "mode": self.mode(),
                "notes": ["Estimates, not search volume. demand = median views per day of the top 20 results (what viewers actually watch for the term). "
                          "supply = the API's result total or the number of distinct channels in the top 20. opportunity = demand divided by log10(supply). "
                          "Compare terms to each other. Real search volume is a licensed dataset with no free source."]}

    # ---- video study
    def video(self, ref, with_transcript=False, lang="en"):
        vid = video_id(ref)
        v = (self.yt.videos([vid]) or [None])[0]
        if not v:
            raise ScoutError("video not found", 404)
        v["thumbnails"] = {"max": "https://i.ytimg.com/vi/%s/maxresdefault.jpg" % vid, "high": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid,
                           "frames": ["https://i.ytimg.com/vi/%s/hq%d.jpg" % (vid, i) for i in (1, 2, 3)]}
        if with_transcript:
            v["transcript"] = self.yt.transcript(vid, lang)
        return v

    def transcript(self, ref, lang="en"):
        vid = video_id(ref)
        return {"id": vid, **self.yt.transcript(vid, lang)}

    # ---- Instagram
    def ig_outliers(self, **kw):
        return self.ig.outliers(**kw)

    def ig_profile(self, user, full=False):
        p = self.ig.profile(user)
        reels = [m for m in p["media"] if m.get("media_product_type") == "REELS" and m.get("like_count") is not None]
        likes = [m["like_count"] for m in reels]
        return {"username": p["username"], "name": p["name"], "followers": p["followers"], "media_count": p["media_count"],
                "reels_sampled": len(reels), "median_reel_likes": int(med(likes)) if likes else None,
                "best_reel_likes": max(likes) if likes else None,
                "top_reels": sorted(({"url": m["permalink"], "likes": m["like_count"], "comments": m.get("comments_count"),
                                      "caption": " ".join((m.get("caption") or "").split())[:140], "posted": m.get("timestamp")} for m in reels),
                                    key=lambda r: -r["likes"])[:5],
                **({"media": p["media"], "stale": bool(p.get("stale"))} if full else {})}

    def usage(self):
        with self.cache.lock:
            day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            callers = self.cache.db.execute("select name, n from usage where day=? and name like 'ytc:%' order by n desc limit 12", (day,)).fetchall()
            anon = self.cache.db.execute("select name, n from usage where day=? and name like 'ytanon:%' order by n desc limit 12", (day,)).fetchall()
        return {"mode": self.mode(), "youtube_units_today": self.cache.usage("yt_units"), "youtube_daily_budget": self.cfg.yt_budget * max(1, len(self.cfg.yt_keys)),
                "youtube_keys": [{"key": i + 1, "units_today": min(self.cache.usage("yt_units:%d" % i), self.cfg.yt_budget), "budget": self.cfg.yt_budget} for i in range(len(self.cfg.yt_keys))],
                "youtube_callers_today": {n[4:]: u for n, u in callers}, "youtube_caller_cap_pct": self.cfg.yt_caller_pct,
                "youtube_unnamed_callers_today": [{"caller": n[7:].rsplit(":", 1)[0], "endpoint": n.rsplit(":", 1)[1], "units": u} for n, u in anon],
                "instagram_calls_today": self.cache.usage("ig_calls"), "instagram_tokens": self.ig.status()}
