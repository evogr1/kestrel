#!/usr/bin/env python3
"""Kestrel growth layer (mixed into the Kestrel class in scout_extras.py).

  clips          most-replayed moments of a long video, with timestamps and transcript 
  me_retention   where viewers leave my own videos (YouTube Analytics)
  gaps           searches with demand but few fresh videos
  changes        title and thumbnail changes on tracked channels, with views before and after
  rising         words and names climbing across every feed, this week against last week
  sponsors       brands sponsoring videos in a niche, from video descriptions

All of it is free on quota except gaps (one search per term) and me_retention (Analytics, not Data API units).
"""
import threading
import os
import shutil
import collections
import datetime as dt
import hashlib
import json
import math
import re
import time
import urllib.parse

from scout_core import ScoutError, med, video_id, parse_duration, CALLER

SOCIAL = {"youtube.com", "youtu.be", "instagram.com", "facebook.com", "fb.com", "twitter.com", "x.com", "tiktok.com", "t.me",
          "linkedin.com", "threads.net", "discord.gg", "discord.com", "patreon.com", "linktr.ee", "bit.ly", "goo.gl", "amzn.to",
          "spotify.com", "apple.com", "podcasts.apple.com", "open.spotify.com", "pinterest.com", "reddit.com", "whatsapp.com", "wa.me",
          "google.com", "forms.gle", "gmail.com", "beacons.ai", "snapchat.com", "twitch.tv", "medium.com", "substack.com"}
SPONSOR_RE = [
    re.compile(r"(?i:sponsored by|thanks to|thank you to|brought to you by|in partnership with|partnered with)\s+@?([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,2})"),
    re.compile(r"(?i:use|with)\s+(?i:my\s+)?(?i:code)\s+['\"]?([A-Z0-9]{3,20})\b"),
]
DOMAIN_RE = re.compile(r"https?://(?:www\.)?([a-z0-9\-]+(?:\.[a-z0-9\-]+)+)", re.I)


def _mmss(s):
    s = int(round(s))
    return "%d:%02d" % (s // 60, s % 60)


def _root(host):
    parts = host.lower().split(".")
    return ".".join(parts[-3:]) if len(parts) > 2 and parts[-2] in ("co", "com", "org", "net") else ".".join(parts[-2:])



_WLOCK = threading.Lock()
_WMODEL = []


def _whisper():
    """Load the speech-to-text model once per process (first call takes a few seconds, later calls none)."""
    if _WMODEL:
        return _WMODEL[0]
    with _WLOCK:
        if _WMODEL:
            return _WMODEL[0]
        name = os.environ.get("SCOUT_WHISPER_MODEL", "base")
        try:
            from faster_whisper import WhisperModel
            _WMODEL.append(("faster", WhisperModel(name, compute_type="int8", cpu_threads=os.cpu_count() or 4)))
        except Exception:
            try:
                import whisper
                _WMODEL.append(("openai", whisper.load_model(name)))
            except Exception:
                return None
        return _WMODEL[0]


def warm_whisper():
    """Called in the background when the server starts, so the first breakdown doesn't pay for loading the model."""
    if os.environ.get("SCOUT_TRANSCRIBE", "0").strip().lower() in ("1", "true", "yes", "on"):
        threading.Thread(target=_whisper, daemon=True).start()

class Growth:
    """Needs: self.yt, self.mine, self.cache, self._q, self._w, self.niches(), self.ranked(), self.bots (via social_posts table)."""

    def _growth_tables(self):
        with self.cache.lock:
            ex = self.cache.db.execute
            ex("create table if not exists vid_meta(video_id text primary key, channel text, title text, thumb text, views integer, seen real)")
            ex("create table if not exists own_snap(video text, channel text, ts real, views integer, likes integer, comments integer,"
               " primary key(video, ts))")
            ex("create table if not exists vid_changes(video_id text, at real, field text, before text, after text, views_before integer,"
               " channel text, primary key(video_id, at, field))")
            self.cache.db.commit()

    # ------------------------------------------------------------ 1. clip finder
    def clips(self, ref, n=8, clip_seconds=45, min_gap=60):
        vid = video_id(ref)
        if not vid:
            raise ScoutError("give a YouTube URL or video id")

        def go():
            rows = self.yt.dlp(["-j", "--skip-download", "https://www.youtube.com/watch?v=" + vid], 180)
            if not rows:
                raise ScoutError("yt-dlp could not read this video", 502)
            return {"title": rows[0].get("title"), "duration": rows[0].get("duration"), "channel": rows[0].get("channel"),
                    "heatmap": rows[0].get("heatmap") or [], "chapters": rows[0].get("chapters") or []}
        info = self.cache.cached("heat:" + vid, 24 * 3600, go)
        heat = [h for h in info["heatmap"] if h.get("value") is not None]
        if not heat:
            return self._clips_fallback(vid, info, int(n), int(clip_seconds), int(min_gap))
        dur = float(info.get("duration") or heat[-1]["end_time"])
        avg = sum(h["value"] for h in heat) / len(heat)
        picks = []
        for h in sorted(heat, key=lambda x: -x["value"]):
            mid = (h["start_time"] + h["end_time"]) / 2.0
            if mid < 20 or any(abs(mid - p["peak"]) < min_gap for p in picks):   # skip the intro spike everyone scrubs past
                continue
            start = max(0.0, mid - clip_seconds * 0.4)
            end = min(dur, start + clip_seconds)
            picks.append({"peak": mid, "start": start, "end": end, "replay_score": round(h["value"], 3),
                          "vs_average": round(h["value"] / avg, 2) if avg else None})
            if len(picks) >= int(n):
                break
        segs = []
        try:
            segs = self.yt.transcript(vid).get("segments") or []
        except ScoutError:
            pass
        for p in picks:
            words = " ".join(s["text"] for s in segs if p["start"] <= s["start"] < p["end"])
            p.update(start_ts=_mmss(p["start"]), end_ts=_mmss(p["end"]), transcript=words[:600] or None,
                     link="https://www.youtube.com/watch?v=%s&t=%ds" % (vid, int(p["start"])),
                     chapter=next((c.get("title") for c in info["chapters"] if c.get("start_time", 0) <= p["peak"] < c.get("end_time", 1e9)), None))
            p["peak"] = _mmss(p["peak"])
        picks.sort(key=lambda p: -p["replay_score"])
        return {"video": vid, "title": info["title"], "channel": info["channel"], "duration": _mmss(dur), "method": "most_replayed", "clips": picks,
                "notes": ["From YouTube's 'most replayed' graph: the moments people rewatch. Each clip starts a little before the peak.",
                          "Trim to the strongest 20 to 40 seconds and open on the line in the transcript, not the setup."]}

    QUOTE_RE = re.compile(r"(\?|\d|\bnever\b|\bnobody\b|\bsecret\b|\bmistake\b|\bbiggest\b|\bcrore\b|\bmillion\b|\bbillion\b|"
                          r"\bfailed\b|\bfired\b|\bquit\b|\bthe truth\b|\bwhat i learned\b|\bif you\b|\bthe reason\b|\bhonestly\b)", re.I)

    def _clips_fallback(self, vid, info, n, clip_seconds, min_gap):
        """No replay graph yet: use chapters if the video has them, else score transcript windows for quotable lines
        (questions, numbers, money, failure, strong claims). Weaker than real replay data, and labelled as such."""
        segs = []
        try:
            segs = self.yt.transcript(vid).get("segments") or []
        except ScoutError:
            pass
        dur = float(info.get("duration") or (segs[-1]["start"] if segs else 0))
        words_in = lambda a, b: " ".join(s["text"] for s in segs if a <= s["start"] < b)
        picks, method = [], None
        if info.get("chapters"):
            method = "chapters"
            for c in info["chapters"]:
                a, b = float(c.get("start_time", 0)), float(c.get("end_time", 0))
                if b - a < 20 or a < 20:
                    continue
                txt = words_in(a, min(b, a + clip_seconds))
                picks.append({"start": a, "end": min(b, a + clip_seconds), "chapter": c.get("title"),
                              "score": len(self.QUOTE_RE.findall(words_in(a, b))) / max((b - a) / 60.0, 1), "transcript": txt[:600] or None})
        elif segs:
            method = "transcript"
            t = 30.0
            while t < dur - 10:
                txt = words_in(t, t + clip_seconds)
                if txt:
                    picks.append({"start": t, "end": min(dur, t + clip_seconds), "chapter": None,
                                  "score": len(self.QUOTE_RE.findall(txt)) + 0.002 * len(txt), "transcript": txt[:600]})
                t += clip_seconds / 3.0
        if not picks:
            raise ScoutError("no replay graph, no chapters and no captions for this video, so there is nothing to pick from", 404)
        chosen = []
        for p in sorted(picks, key=lambda x: -x["score"]):
            if any(abs(p["start"] - c["start"]) < min_gap for c in chosen):
                continue
            chosen.append(p)
            if len(chosen) >= n:
                break
        for p in chosen:
            p.update(start_ts=_mmss(p["start"]), end_ts=_mmss(p["end"]), score=round(p["score"], 2),
                     link="https://www.youtube.com/watch?v=%s&t=%ds" % (vid, int(p["start"])))
        chosen.sort(key=lambda x: x["start"])
        return {"video": vid, "title": info.get("title"), "channel": info.get("channel"), "duration": _mmss(dur), "method": method,
                "clips": chosen, "notes": ["No 'most replayed' graph yet (the video needs more views), so these come from %s: "
                                           "a guess at quotable moments, not viewer data. Check each one before cutting." % method]}

    # ------------------------------------------------------------ live stats on my own uploads (vidIQ-style, no lag)
    def me_live(self, channel, n=10, snapshot=True):
        """Real-time numbers for my latest uploads from the Data API counters (minutes old), not the Analytics API
        (1 to 3 days behind). Every call stores a snapshot, so views per hour and 'vs my usual at this age' fill in
        as the live job runs every 15 minutes. Costs 2 units of the channel's own OAuth quota, none of the shared keys."""
        self._growth_tables()
        me = self.mine._me(channel)
        ups = self.mine._uploads(channel, me, max(int(n), 1))[:int(n)]
        if not ups:
            return {"channel": channel, "videos": [], "notes": ["no uploads yet"]}
        items = self.mine._get(channel, "https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics,contentDetails,status&id="
                               + ",".join(u["id"] for u in ups)).get("items") or []
        now = time.time()
        out = []
        for it in items:
            st = it.get("statistics", {})
            views, likes, comments = (int(st.get(k, 0) or 0) for k in ("viewCount", "likeCount", "commentCount"))
            pub = it["snippet"].get("publishedAt")
            try:
                pub_ts = dt.datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
            except (AttributeError, ValueError):
                pub_ts = now
            hours = max((now - pub_ts) / 3600.0, 0.01)
            prev = self._q("select ts, views from own_snap where video=? order by ts desc limit 1", (it["id"],))
            if snapshot:
                self._w("insert or replace into own_snap values(?,?,?,?,?,?)", (it["id"], channel, now, views, likes, comments))
            row = {"video": it["id"], "title": it["snippet"]["title"], "status": it.get("status", {}).get("privacyStatus"),
                   "duration": _mmss(parse_duration(it["contentDetails"].get("duration")) or 0), "hours_since_upload": round(hours, 1),
                   "views": views, "likes": likes, "comments": comments, "views_per_hour_lifetime": round(views / hours, 1)}
            if prev and now - prev[0][0] >= 300:
                gap = (now - prev[0][0]) / 3600.0
                row["views_per_hour_now"] = round((views - prev[0][1]) / gap, 1)
                row["since_last_check"] = {"minutes": round(gap * 60), "new_views": views - prev[0][1]}
            # same-age comparison against my earlier uploads' snapshots
            peers = []
            for (vid,) in self._q("select distinct video from own_snap where channel=? and video<>?", (channel, it["id"])):
                pts = self._q("select ts, views from own_snap where video=? order by ts", (vid,))
                if not pts:
                    continue
                vpub = self.cache.get("pub:" + vid)
                if not vpub:
                    continue
                target = vpub + hours * 3600
                near = min(pts, key=lambda p: abs(p[0] - target))
                if abs(near[0] - target) <= max(3600, hours * 3600 * 0.15):
                    peers.append(near[1])
            self.cache.put("pub:" + it["id"], pub_ts, 400 * 86400)
            if len(peers) >= 3 and med(peers) >= 5 and it.get("status", {}).get("privacyStatus") == "public":
                usual = med(peers)
                row["vs_my_usual_at_this_age"] = round(views / usual, 2) if usual else None
                row["my_usual_views_at_this_age"] = int(usual)
            out.append(row)
        out.sort(key=lambda r: r["hours_since_upload"])
        return {"channel": channel, "checked_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now)), "videos": out, "notes": [
            "Live public counters, minutes old. Analytics (retention, audience, traffic) stays 1 to 3 days behind on YouTube's side for everyone.",
            "views_per_hour_now and vs_my_usual_at_this_age need earlier snapshots; the live job takes one every 15 minutes."]}

    def live_all(self, n=10):
        """Snapshot every connected channel (run every 15 minutes)."""
        done, errors = [], []
        for c in self.mine.channel_names() if hasattr(self.mine, "channel_names") else []:
            try:
                r = self.me_live(c, n)
                done.append({"channel": c, "videos": len(r["videos"])})
            except ScoutError as e:
                errors.append({"channel": c, "error": str(e)})
        self._w("delete from own_snap where ts<?", (time.time() - 120 * 86400,))
        return {"snapshots": done, "errors": errors}

    # ------------------------------------------------------------ impressions and CTR (YouTube Reporting API)
    REACH_TYPES = ("channel_reach_basic_a1", "channel_reach_combined_a1")
    RPT = "https://youtubereporting.googleapis.com/v1/"

    def _reach_tables(self):
        with self.cache.lock:
            ex = self.cache.db.execute
            ex("create table if not exists reach_reports(report_id text primary key, channel text, type text, start text, fetched real)")
            ex("create table if not exists reach_rows(channel text, report_id text, day text, video text, source text, device text,"
               " impressions integer, ctr real)")
            ex("create index if not exists reach_rows_v on reach_rows(channel, video, day)")
            self.cache.db.commit()

    def _rpt(self, ch, path, body=None):
        import scout_mine
        data = json.dumps(body).encode("utf8") if body is not None else None
        hdr = {"Authorization": "Bearer " + self.mine._token(ch)}
        if data:
            hdr["Content-Type"] = "application/json"
        return scout_mine._http(self.RPT + path, data=data, headers=hdr)

    def _rpt_csv(self, ch, url):
        import urllib.request
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.mine._token(ch), "User-Agent": "kestrel/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
        if raw[:2] == b"\x1f\x8b":
            import gzip
            raw = gzip.decompress(raw)
        return raw.decode("utf8", "ignore")

    def reach_setup(self, channel):
        """Make sure YouTube is generating the reach reports for this channel. Idempotent. YouTube backfills about 30 days
        once a job exists and adds a new daily report after that."""
        jobs = self._rpt(channel, "jobs").get("jobs") or []
        have = {j["reportTypeId"]: j["id"] for j in jobs}
        made = []
        for t in self.REACH_TYPES:
            if t not in have:
                j = self._rpt(channel, "jobs", {"reportTypeId": t, "name": "kestrel " + t})
                have[t] = j["id"]
                made.append(t)
        return {"channel": channel, "jobs": have, "created_now": made}

    def reach_sync(self, channel):
        """Download every new reach report for a channel into the database."""
        import csv
        import io
        self._reach_tables()
        jobs = self.reach_setup(channel)
        got, pending = 0, []
        for t, job in jobs["jobs"].items():
            if t not in self.REACH_TYPES:
                continue
            reports = self._rpt(channel, "jobs/%s/reports" % job).get("reports") or []
            if not reports:
                pending.append(t)
            for rp in reports:
                if self._q("select 1 from reach_reports where report_id=?", (rp["id"],)):
                    continue
                text = self._rpt_csv(channel, rp["downloadUrl"])
                rows = []
                for r in csv.DictReader(io.StringIO(text)):
                    d = r.get("date") or ""
                    day = "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if len(d) == 8 else d
                    rows.append((channel, rp["id"], day, r.get("video_id"), r.get("traffic_source_type") or ("ALL" if t.endswith("basic_a1") else ""),
                                 r.get("device_type") or "", int(float(r.get("video_thumbnail_impressions") or 0)),
                                 float(r.get("video_thumbnail_impressions_ctr") or 0)))
                with self.cache.lock:
                    self.cache.db.executemany("insert into reach_rows values(?,?,?,?,?,?,?,?)", rows)
                    self.cache.db.execute("insert or replace into reach_reports values(?,?,?,?,?)",
                                          (rp["id"], channel, t, rp.get("startTime"), time.time()))
                    self.cache.db.commit()
                got += 1
        return {"channel": channel, "new_reports": got, "waiting_for_first_report": pending}

    def me_reach(self, channel, video=None, days=28, sync=True):
        """Thumbnail impressions and click-through rate per video, by traffic source and device, from YouTube's own reach reports
        (the numbers Studio shows). Daily reports, about 1 to 2 days behind."""
        self._reach_tables()
        note = []
        if sync:
            try:
                s = self.reach_sync(channel)
                if s["waiting_for_first_report"]:
                    note.append("YouTube just started generating reach reports for this channel. The first ones (with about 30 days "
                                "of history) usually arrive within 24 to 48 hours. Ask again tomorrow.")
            except ScoutError as e:
                note.append("could not refresh reports: %s" % e)
        since = str(dt.date.today() - dt.timedelta(days=int(days)))
        vfilter, args = ("and video=?", [video_id(video)]) if video else ("", [])
        basic = self._q("select video, sum(impressions), sum(impressions*ctr) from reach_rows where channel=? and source='ALL' and day>=? %s "
                        "group by video order by 2 desc" % vfilter, [channel, since] + args)
        if not basic:   # fall back to the combined report when the basic one isn't in yet
            basic = self._q("select video, sum(impressions), sum(impressions*ctr) from reach_rows where channel=? and source not in ('', 'ALL') "
                            "and day>=? %s group by video order by 2 desc" % vfilter, [channel, since] + args)
        names = {}
        try:
            names = self.mine._titles(channel, [b[0] for b in basic[:50]])
        except ScoutError:
            pass
        videos = [{"video": v, "title": names.get(v), "impressions": int(imp or 0),
                   "ctr_pct": round(100.0 * w / imp, 2) if imp else None} for v, imp, w in basic]
        src = self._q("select source, sum(impressions), sum(impressions*ctr) from reach_rows where channel=? and source not in ('', 'ALL') "
                      "and day>=? %s group by source order by 2 desc" % vfilter, [channel, since] + args)
        dev = self._q("select device, sum(impressions) from reach_rows where channel=? and device<>'' and day>=? %s group by device order by 2 desc"
                      % vfilter, [channel, since] + args)
        daily = self._q("select day, sum(impressions), sum(impressions*ctr) from reach_rows where channel=? and source='ALL' and day>=? %s "
                        "group by day order by day" % vfilter, [channel, since] + args)
        last = self._q("select max(day) from reach_rows where channel=?", (channel,))
        tot_i = sum(v["impressions"] for v in videos)
        tot_w = sum(w or 0 for _, _, w in basic)
        return {"channel": channel, "days": int(days), "data_through": last[0][0] if last else None,
                "total_impressions": tot_i, "overall_ctr_pct": round(100.0 * tot_w / tot_i, 2) if tot_i else None,
                "videos": videos[:50],
                "by_traffic_source": [{"source": s, "impressions": int(i or 0), "ctr_pct": round(100.0 * w / i, 2) if i else None} for s, i, w in src],
                "by_device": [{"device": d, "impressions": int(i or 0)} for d, i in dev],
                "daily": [{"day": d, "impressions": int(i or 0), "ctr_pct": round(100.0 * w / i, 2) if i else None} for d, i, w in daily],
                "notes": note + ["Impressions and CTR from YouTube's reach reports, the same numbers Studio shows. Daily, about 1 to 2 days behind.",
                                 "For views in the last hours use scout_my_live."]}

    def reach_all(self):
        done, errors = [], []
        for c in self.mine.channel_names():
            try:
                done.append(self.reach_sync(c))
            except ScoutError as e:
                errors.append({"channel": c, "error": str(e)})
        return {"synced": done, "errors": errors}

    # ------------------------------------------------------------ creator breakdown (show-your-work analysis + advice for me)
    def _transcribe_reel(self, url, seconds=60):
        """Best effort: download the reel's audio with yt-dlp and transcribe it with faster-whisper (or whisper).
        The model stays loaded between calls and every transcript is cached, so repeat runs are instant.
        Instagram often blocks downloads without a login (SCOUT_IG_COOKIES). Returns (text, reason).
        Lines in the text end with a full stop at each pause Whisper detects, so hooks split cleanly."""
        import os, shutil, subprocess, tempfile
        if os.environ.get("SCOUT_TRANSCRIBE", "0").strip().lower() not in ("1", "true", "yes", "on"):
            return None, "transcription is switched off (SCOUT_TRANSCRIBE=0)"
        key = "reeltx:" + url.split("?")[0].rstrip("/")
        hit = self.cache.get(key) if hasattr(self, "cache") else None
        if hit:
            return hit, None
        if not shutil.which("yt-dlp"):
            return None, "yt-dlp is not installed on the server"
        model = _whisper()
        if not model:
            return None, "no speech-to-text model on the server (pip install faster-whisper)"
        d = tempfile.mkdtemp()
        try:
            cmd = ["yt-dlp", "-q", "--no-warnings", "--no-playlist", "-f", "ba/b", "-o", os.path.join(d, "a.%(ext)s"), url]
            if os.environ.get("SCOUT_IG_COOKIES"):
                cmd[1:1] = ["--cookies", os.environ["SCOUT_IG_COOKIES"]]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            files = [f for f in os.listdir(d) if f.startswith("a.") and not f.endswith(".part")]
            if p.returncode or not files:
                return None, "Instagram refused the download (needs a logged-in cookies file)"
            path = os.path.join(d, files[0])
            with _WLOCK:
                if model[0] == "faster":
                    segs, _ = model[1].transcribe(path, beam_size=1, vad_filter=True, clip_timestamps="0,%d" % int(seconds),
                                                  condition_on_previous_text=False)
                    lines = [s.text.strip() for s in segs]
                else:
                    lines = [s["text"].strip() for s in model[1].transcribe(path).get("segments", [])]
            text = " ".join(l if l[-1:] in ".!?" else l + "." for l in (" ".join(x.split()) for x in lines) if l)
            if text:
                self.cache.put(key, text, 90 * 86400)
            return (text or None), None
        except Exception as e:
            return None, "transcription failed (%s)" % type(e).__name__
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------ shared helpers for watchlist and reel checks
    @staticmethod
    def _tz():
        n = os.environ.get("SCOUT_TZ", "UTC")
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(n)
        except Exception:
            return dt.timezone.utc

    @staticmethod
    def _ist_slot(ts):
        try:
            h = dt.datetime.fromisoformat(str(ts).replace("+0000", "+00:00").replace("Z", "+00:00")).astimezone(
                Growth._tz()).hour
        except (ValueError, TypeError):
            return None
        return "morning 6-12" if 6 <= h < 12 else "afternoon 12-17" if 12 <= h < 17 else "evening 17-22" if 17 <= h < 22 else "night 22-6"

    @staticmethod
    def _ts(ts):
        try:
            return dt.datetime.fromisoformat(str(ts).replace("+0000", "+00:00").replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None

    def _scored_posts(self, platform, handle):
        """A creator's recent posts with one score each (views where the platform gives them, else likes) and the boost flag."""
        if platform == "instagram":
            p = self.ig.profile(handle)
        elif platform == "youtube":
            p = self._yt_items(handle)
        else:
            p = self._x_items(handle)
        rows = [m for m in (p.get("media") or []) if m.get("media_product_type") == "REELS" and m.get("like_count") is not None]
        metric = "likes" if platform != "youtube" else "views"
        if platform == "instagram" and rows and sum(1 for m in rows if m.get("view_count")) >= 0.6 * len(rows):
            metric = "views"
            ratios = [m["like_count"] / m["view_count"] for m in rows if m.get("view_count")]
            norm = med(ratios) if len(ratios) >= 5 else None
            for m in rows:
                m["_likes"], m["_score"] = m["like_count"], m.get("view_count") or 0
                m["_boosted"] = bool(norm and m.get("view_count") and m["like_count"] / m["view_count"] < 0.25 * norm)
        else:
            for m in rows:
                m["_score"] = m["like_count"]
        usual = med([m["_score"] for m in rows if m.get("_score") is not None]) or 1
        return rows, metric, usual

    # ------------------------------------------------------------ watchlist: what's new since I last looked
    def _wl(self):
        self._w("create table if not exists watchlist(platform text, handle text, added real, last_checked real, primary key(platform, handle))")

    def watchlist_add(self, handle, platform="instagram"):
        plat = self.ALIASES.get((platform or "instagram").lower(), (platform or "instagram").lower())
        if plat not in self.PLATFORMS:
            raise ScoutError("platform must be instagram, youtube or x", 400)
        self._wl()
        h = handle.strip().lstrip("@") if plat != "youtube" else handle.strip()
        if not h:
            raise ScoutError("give a handle", 400)
        now = time.time()
        self._w("insert or ignore into watchlist(platform, handle, added, last_checked) values(?, ?, ?, ?)", (plat, h.lower() if plat != "youtube" else h, now, now - 7 * 86400))
        return self.watchlist()

    def watchlist_remove(self, handle, platform="instagram"):
        plat = self.ALIASES.get((platform or "instagram").lower(), (platform or "instagram").lower())
        self._wl()
        self._w("delete from watchlist where platform=? and lower(handle)=lower(?)", (plat, handle.strip().lstrip("@")))
        return self.watchlist()

    def watchlist(self):
        self._wl()
        rows = self._q("select platform, handle, last_checked from watchlist order by platform, handle")
        return {"watching": [{"platform": p, "handle": h, "last_checked": dt.datetime.fromtimestamp(c, dt.timezone.utc).isoformat()} for p, h, c in rows],
                "count": len(rows), "note": None if rows else "Empty. Add creators with scout_watchlist_add (up to about 20 is sensible)."}

    def whats_new(self, min_multiple=2.0, platform=None, limit=15):
        """Every post that went viral (vs that creator's own usual) since the last time I checked my watchlist."""
        from scout_extras import first_line
        self._wl()
        q = "select platform, handle, last_checked from watchlist" + (" where platform=?" if platform else "")
        rows = self._q(q, (self.ALIASES.get(platform, platform),) if platform else ())
        if not rows:
            raise ScoutError("your watchlist is empty: add creators first (scout_watchlist_add)", 404)
        hits, errors, now, oldest = [], [], time.time(), min(r[2] for r in rows)
        for plat, h, since in rows:
            try:
                posts, metric, usual = self._scored_posts(plat, h)
            except ScoutError as e:
                errors.append({"creator": h, "platform": plat, "error": str(e)})
                continue
            for m in posts:
                t = self._ts(m.get("timestamp"))
                if t is None or t <= since:
                    continue
                x = round(m["_score"] / max(usual, 1), 1)
                if x >= float(min_multiple):
                    hits.append({"creator": "@" + h.lstrip("@"), "platform": plat, "url": m["permalink"],
                                 "hook": first_line(m.get("caption") or "", 120), metric: m["_score"], "metric": metric,
                                 "vs_their_usual": x, "posted": m.get("timestamp"), "boosted": bool(m.get("_boosted"))})
            self._w("update watchlist set last_checked=? where platform=? and handle=?", (now, plat, h))
        hits.sort(key=lambda r: (r["boosted"], -r["vs_their_usual"]))
        return {"since": dt.datetime.fromtimestamp(oldest, dt.timezone.utc).isoformat(), "checked": len(rows) - len(errors),
                "new_hits": hits[:int(limit)], "errors": errors,
                "note": "Nothing new went %sx+ since you last checked." % min_multiple if not hits else
                        "Paid boosts are listed last and marked; don't copy those. Run a creator breakdown on any hit for a script."}

    # ------------------------------------------------------------ my own reel: why it popped or flopped
    def my_reel_check(self, reel=None, me=None):
        """My latest (or a given) Instagram reel against my own usual: views, watch time, shares, saves, hook, timing. Then what to change."""
        from scout_extras import hook_type
        prof = self._profile_rows()
        me = me or prof.get("instagram") or os.environ.get("SCOUT_SHOWCASE_ME", "")
        ins = self.cache.cached("myinsfresh:%s" % me.lower(), 20 * 60, lambda: self.mine.ig_insights(me, days=28, reels=25))
        rows = [r for r in ins.get("reels") or [] if r.get("views")]
        if len(rows) < 4:
            raise ScoutError("need at least 4 of your reels with view data to compare", 404)
        if reel:
            key = reel.rstrip("/").split("/")[-1]
            target = next((r for r in rows if key and key in (r.get("url") or "")), None)
            if not target:
                raise ScoutError("couldn't find that reel among your last 25", 404)
        else:
            settled = [r for r in rows if (r.get("age_days") or 0) >= 1]
            target = max(settled or rows, key=lambda r: self._ts(r.get("posted")) or 0)
        others = [r for r in rows if r is not target]
        usual = med([r["views"] for r in others]) or 1
        x = round(target["views"] / usual, 2)
        verdict = "popped" if x >= 1.5 else "flopped" if x <= 0.6 else "did about normal"

        def rate(r, k):
            return (r.get(k) or 0) / r["views"] if r.get("views") else None

        def cmp(k, label, unit=""):
            mine_v = target.get(k) if k == "avg_watch_seconds" else rate(target, k)
            base = med([o.get(k) if k == "avg_watch_seconds" else rate(o, k) for o in others if (o.get(k) if k == "avg_watch_seconds" else rate(o, k)) is not None])
            if mine_v is None or not base:
                return None
            ratio = mine_v / base
            fmtv = (lambda v: "%.1fs" % v) if unit == "s" else (lambda v: "%.2f%%" % (100 * v))
            return {"what": label, "this_reel": fmtv(mine_v), "your_usual": fmtv(base), "vs_usual": round(ratio, 2),
                    "verdict": "better" if ratio >= 1.25 else "worse" if ratio <= 0.8 else "same"}
        signals = [s for s in (cmp("avg_watch_seconds", "average watch time", "s"), cmp("shares", "shares per view"),
                               cmp("saved", "saves per view"), cmp("comments", "comments per view")) if s]
        # hook and timing against what usually works for me
        by_hook, by_slot = collections.defaultdict(list), collections.defaultdict(list)
        for o in others:
            by_hook[hook_type(o.get("caption") or "")].append(o["views"] / usual)
            if self._ist_slot(o.get("posted")):
                by_slot[self._ist_slot(o.get("posted"))].append(o["views"] / usual)
        best_hook = max(((k, med(v)) for k, v in by_hook.items() if len(v) >= 2), key=lambda kv: kv[1], default=(None, 0))[0]
        best_slot = max(((k, med(v)) for k, v in by_slot.items() if len(v) >= 2), key=lambda kv: kv[1], default=(None, 0))[0]
        th, ts_ = hook_type(target.get("caption") or ""), self._ist_slot(target.get("posted"))
        fixes = []
        sig = {s["what"]: s for s in signals}
        if verdict != "popped":
            if sig.get("average watch time", {}).get("verdict") == "worse":
                fixes.append("People left early (watch time %s vs your usual %s). Tighten the first 2 seconds%s." % (
                    sig["average watch time"]["this_reel"], sig["average watch time"]["your_usual"],
                    (": your %s hooks hold best" % best_hook) if best_hook and best_hook != th else ""))
            elif best_hook and best_hook != th:
                fixes.append("This opened with a %s hook; your %s hooks usually do better. Try one next." % (th, best_hook))
            if sig.get("shares per view", {}).get("verdict") == "worse":
                fixes.append("Fewer people sent it on (%s shares per view vs %s). Give viewers a reason to forward it: a surprising number, a 'tag someone who' moment." % (
                    sig["shares per view"]["this_reel"], sig["shares per view"]["your_usual"]))
            if sig.get("comments per view", {}).get("verdict") == "worse":
                asks = lambda c: bool(re.search(r"\bcomment\b|\bdm me\b|\blink in bio\b|\btell me\b|\?\s*$", c or "", re.I))
                usual_ask = sum(1 for o in others if asks(o.get("caption"))) >= 0.5 * len(others)
                fixes.insert(0, ("Comments were far below your usual (%s vs %s per view), and that's what pushes a reel. Most of your reels ask people to "
                                 "comment and this one didn't: add the ask (\"comment X and I'll send it\" or a question they want to answer)."
                                 if usual_ask and not asks(target.get("caption")) else
                                 "Comments were far below your usual (%s vs %s per view), and comments are what push a reel. End with a question "
                                 "people want to answer, or a 'comment X and I'll send it'.") % (
                    sig["comments per view"]["this_reel"], sig["comments per view"]["your_usual"]))
            if sig.get("saves per view", {}).get("verdict") == "worse" and len(fixes) < 3:
                fixes.append("Fewer saves (%s vs %s per view). Give people something to come back to: steps, a list, the exact tool or numbers on screen." % (
                    sig["saves per view"]["this_reel"], sig["saves per view"]["your_usual"]))
            if best_slot and ts_ and best_slot != ts_:
                fixes.append("It went out in the %s slot; your reels do best in the %s slot." % (ts_, best_slot))
            if not fixes:
                fixes.append("Nothing stands out as broken: it's probably the topic. Run a breakdown on a creator in your niche and borrow a proven angle.")
        else:
            better = [s["what"] for s in signals if s["verdict"] == "better"]
            fixes.append("Make another one like it within a week, same %s hook%s." % (th, (" and %s slot" % ts_) if ts_ else ""))
            if better:
                fixes.append("What carried it: %s above your usual. Keep that element." % ", ".join(better))
        return {"reel": target.get("url"), "caption": target.get("caption"), "posted": target.get("posted"), "age_days": target.get("age_days"),
                "views": target["views"], "your_usual_views": int(usual), "vs_usual": x, "verdict": verdict, "signals": signals,
                "hook_type": th, "your_best_hook": best_hook, "slot_ist": ts_, "your_best_slot": best_slot, "what_to_do": fixes[:3],
                "note": "Only %.1f days old, views may still be climbing." % target["age_days"] if (target.get("age_days") or 9) < 2 else None}

    # ------------------------------------------------------------ setup: what works, what's missing, how to fix it
    def setup_status(self, verify=True):
        """A guided health check for new users. Tests each part for real (1 cheap call each) and says how to fix what's missing."""
        import importlib.util
        plugin = os.environ.get("SCOUT_CALLER") == "plugin"
        where = ("open /config in Claude Code, find Kestrel and paste it there" if plugin
                 else "add it to your .env file and restart Kestrel")
        items = []

        def item(name, status, detail, unlocks, steps=None):
            items.append({"name": name, "status": status, "detail": detail, "unlocks": unlocks, "how_to_fix": steps or []})

        # 1. YouTube key
        keys = getattr(getattr(self, "cfg", None) or getattr(self.yt, "cfg", None), "yt_keys", []) or []
        yt_steps = ["Open https://console.cloud.google.com and sign in with any Google account (free, no card needed).",
                    "Create a project (top bar, 'New project'), any name.",
                    "Go to APIs and Services, Library, search 'YouTube Data API v3', press Enable.",
                    "Go to APIs and Services, Credentials, Create credentials, API key. Copy it.",
                    "Paste it as the YouTube API key: " + where + "."]
        if not keys:
            item("YouTube research", "missing", "No YouTube API key yet.", ["YouTube outliers, channel stats, YouTube breakdowns, keyword demand"], yt_steps)
        elif verify:
            try:
                self.yt.api("videos", 1, part="id", id="dQw4w9WgXcQ")
                item("YouTube research", "working", "%d key%s, about %d searches a day free (more via the yt-dlp fallback)." % (
                    len(keys), "" if len(keys) == 1 else "s", 100 * len(keys)), ["YouTube outliers, channel stats, YouTube breakdowns"])
            except ScoutError as e:
                item("YouTube research", "broken", "Google rejected the key: %s" % e, ["YouTube research"],
                     ["Check the key was copied whole, and that 'YouTube Data API v3' is enabled in the same project."] + yt_steps[3:])
        else:
            item("YouTube research", "working", "%d key(s) set." % len(keys), ["YouTube research"])

        # 2. Instagram (Meta's official API)
        ig_steps = ["Your Instagram must be a Business or Creator account (Instagram app, Settings, Account type) linked to a Facebook Page.",
                    "Go to https://developers.facebook.com, My Apps, Create app, type Business. It's free.",
                    "Open https://developers.facebook.com/tools/explorer, pick your app, add the permissions instagram_basic, "
                    "instagram_manage_insights, pages_show_list, pages_read_engagement, then Generate Access Token and allow it.",
                    "In the same Explorer, run: me/accounts?fields=instagram_business_account . Copy the instagram_business_account id.",
                    "Make the token last about 60 days: paste it into https://developers.facebook.com/tools/debug/accesstoken and press "
                    "'Extend Access Token'. Copy the new one.",
                    "Paste the token and the id as the Instagram token and Instagram user ID: " + where + "."]
        pool = []
        try:
            pool = self.ig.pool()
        except Exception:
            pass
        if not pool:
            item("Instagram research", "missing", "No Instagram token yet. Uses Meta's official API, free.",
                 ["Instagram creator breakdowns with real views", "Instagram outliers", "your own reel stats"], ig_steps)
        elif verify:
            try:
                p = self.ig.profile("instagram")
                item("Instagram research", "working", "Token works (read @instagram: %s followers)." % format(p.get("followers") or 0, ","),
                     ["Instagram creator breakdowns", "Instagram outliers"])
            except ScoutError as e:
                item("Instagram research", "broken", "Meta rejected the token: %s" % e, ["Instagram research"],
                     ["Tokens from the Explorer expire after about an hour unless extended. Make a fresh one:"] + ig_steps[2:])
        else:
            item("Instagram research", "working", "Token set.", ["Instagram research"])

        # 3. your own YouTube channel (optional)
        d = getattr(self.mine, "dir", "") or ""
        has_client = os.path.exists(os.path.join(d, "youtube_client.json"))
        toks = [f[len("youtube_token_"):-5] for f in (os.listdir(d) if os.path.isdir(d) else []) if f.startswith("youtube_token_")]
        if toks:
            item("Your YouTube channel stats", "working", "Connected: %s." % ", ".join(sorted(toks)), ["live views per hour, retention, impressions and CTR"])
        else:
            item("Your YouTube channel stats", "optional", "Not connected. Only needed for stats on your OWN channel.",
                 ["live views per hour", "retention", "impressions and CTR"],
                 ([] if has_client else [
                     "In the same Google Cloud project, enable 'YouTube Analytics API' and 'YouTube Reporting API'.",
                     "Credentials, Create credentials, OAuth client ID, type Desktop app. Download the JSON.",
                     "Save it as %s" % os.path.join(d or "~/.scout/youtube_oauth", "youtube_client.json")]) +
                 ["Run: python3 kestrel_connect_youtube.py mychannel   (from the Kestrel folder), sign in, pick your channel."])

        # 4. transcription (optional)
        on = os.environ.get("SCOUT_TRANSCRIBE", "0").strip().lower() in ("1", "true", "yes", "on")
        have_dlp = bool(shutil.which("yt-dlp"))
        have_w = bool(importlib.util.find_spec("faster_whisper") or importlib.util.find_spec("whisper"))
        if on and have_dlp and have_w:
            item("Reel transcription", "working", "On. Only the one picked reel is transcribed per breakdown.", ["word-for-word scripts from reels"])
        else:
            item("Reel transcription", "optional",
                 "Off. Breakdowns still work from captions; YouTube uses the video's own captions either way.", ["word-for-word reel transcripts"],
                 [s for s in ["pip install yt-dlp" if not have_dlp else None, "pip install faster-whisper" if not have_w else None,
                              "Set SCOUT_TRANSCRIBE=1. Downloading reels isn't allowed by Instagram's terms and usually needs a logged-in "
                              "cookies.txt (SCOUT_IG_COOKIES). Your call." if not on else None] if s])

        # 5. about you
        prof = self.my_profile()
        item("About you", "working" if prof["set_up"] else "missing",
             "Saved: %s." % ", ".join("%s %s" % (k, v) for k, v in prof["profile"].items()) if prof["set_up"] else
             "Kestrel doesn't know your page, niche or audience yet.", ["scripts written for your niche and audience", "them-vs-you comparisons"],
             [] if prof["set_up"] else ["Just tell Claude your Instagram username, what your page is about and who it's for."])

        order = {"broken": 0, "missing": 1, "optional": 2, "working": 3}
        todo = sorted([i for i in items if i["status"] in ("broken", "missing")], key=lambda i: order[i["status"]])
        return {"ready": not todo, "working": [i["name"] for i in items if i["status"] == "working"], "items": items,
                "next": todo[0]["name"] if todo else None,
                "how_to_guide": "Walk the user through ONE item at a time, in the order listed, using its how_to_fix steps as a short numbered "
                                "list. After they say it's done, run kestrel_setup again to confirm before moving on. Everything is free.",
                "cost": "Free. Kestrel has no credits or paid tier (the optional X API is billed by X). YouTube's free key covers about 100 searches a day per key and Kestrel "
                        "falls back to yt-dlp after that; Instagram allows about 200 calls an hour."}

    # ------------------------------------------------------------ 0. who I am (asked once, used by every breakdown)
    PROFILE_FIELDS = {"instagram": "Your Instagram username", "youtube": "Your YouTube @handle or channel", "x": "Your X handle",
                      "niche": "What your page is about, in a few words", "audience": "Who you're making it for",
                      "goal": "What you want from it (followers, leads, sales...)", "language": "Language you post in"}

    def _profile_rows(self):
        user_id = getattr(CALLER, "user_id", None)
        if user_id:
            self._w("create table if not exists my_profile_users(user_id text, k text, v text, primary key(user_id,k))")
            rows = {k: v for k, v in self._q("select k, v from my_profile_users where user_id=?", (user_id,)) if v}
            if not rows.get("instagram"):
                try:
                    accounts = self.mine.ig_accounts().get("accounts") or []
                    if accounts and accounts[0].get("username"):
                        rows["instagram"] = accounts[0]["username"]
                except ScoutError:
                    pass
            return rows
        self._w("create table if not exists my_profile(k text primary key, v text)")
        return {k: v for k, v in self._q("select k, v from my_profile") if v}

    def my_profile(self):
        """What Kestrel knows about the person using it. Empty until they answer the setup questions once."""
        p = self._profile_rows()
        missing = [k for k in ("instagram", "niche", "audience") if not p.get(k)]
        return {"profile": p, "set_up": not missing, "missing": missing,
                "ask_for": {k: self.PROFILE_FIELDS[k] for k in missing},
                "note": "Ask the user these once, in one short message, then save them with scout_my_profile_set." if missing else None}

    def my_profile_set(self, instagram=None, youtube=None, x=None, niche=None, audience=None, goal=None, language=None):
        vals = {"instagram": instagram, "youtube": youtube, "x": x, "niche": niche, "audience": audience, "goal": goal, "language": language}
        user_id = getattr(CALLER, "user_id", None)
        self._profile_rows()
        for k, v in vals.items():
            if v is None:
                continue
            v = " ".join(str(v).split())[:300]
            if k in ("instagram", "youtube", "x"):
                v = v.lstrip("@") if k != "youtube" else v
            if user_id:
                self._w("insert or replace into my_profile_users(user_id,k,v) values(?,?,?)", (user_id, k, v))
            else:
                self._w("insert or replace into my_profile(k, v) values(?, ?)", (k, v))
        return self.my_profile()

    # how each platform is described on screen and in the brief
    PLATFORMS = {
        "instagram": {"site": "Instagram", "item": "reel", "items": "reels", "metric": "likes", "verb": "Pulled every recent reel with its likes and comments"},
        "youtube": {"site": "YouTube", "item": "video", "items": "videos", "metric": "views", "verb": "Pulled every recent upload with its views and comments"},
        "x": {"site": "X", "item": "post", "items": "posts", "metric": "likes", "verb": "Pulled their recent posts with likes, reposts and replies"},
    }
    ALIASES = {"ig": "instagram", "insta": "instagram", "yt": "youtube", "twitter": "x", "tw": "x"}

    def _my_insights_async(self, me):
        """Start reading my own account in the background (cached 3 h) so it overlaps with the creator's fetch."""
        box = {}
        user_id = getattr(CALLER, "user_id", None)

        def run():
            try:
                CALLER.user_id = user_id
                box["v"] = self.cache.cached("myins:%s" % (me or user_id or "default").lower(), 3 * 3600, lambda: self.mine.ig_insights(me, days=28, reels=25))
            except ScoutError as e:
                box["e"] = str(e)
            except Exception as e:  # never let my side break their breakdown
                box["e"] = "%s" % type(e).__name__
            finally:
                CALLER.user_id = None
        th = threading.Thread(target=run, daemon=True)
        th.start()

        def result(timeout=25):
            th.join(timeout)
            if th.is_alive():
                return None, "your insights took too long, skipped this time"
            return box.get("v"), box.get("e")
        return result

    def _yt_items(self, ref, n=40):
        info = self.yt.channel_info(ref)
        ups = self.yt.recent(info, "any", n) or []
        items = [{"permalink": v.get("url") or "https://www.youtube.com/watch?v=" + v["id"], "like_count": v.get("views") or 0,
                  "comments_count": v.get("comments"), "caption": v.get("title") or "", "timestamp": v.get("published"),
                  "media_product_type": "REELS", "_id": v["id"], "_short": v.get("is_short")} for v in ups if v.get("views") is not None]
        return {"username": (info.get("handle") or info.get("title") or ref).lstrip("@"), "followers": info.get("subscribers"),
                "media_count": info.get("video_count"), "media": items, "name": info.get("title")}

    def _x_items(self, user):
        posts = self.x.user_posts(user)
        items = [{"permalink": t["url"], "like_count": t.get("likes") or 0, "comments_count": t.get("replies"),
                  "caption": t.get("text") or "", "timestamp": t.get("created"), "media_product_type": "REELS",
                  "_reposts": t.get("reposts"), "_kind": t.get("kind")} for t in posts.get("posts", [])]
        return {"username": user, "followers": posts.get("followers"), "media_count": posts.get("post_count"), "media": items,
                "source": posts.get("source")}

    def _yt_transcript(self, url, seconds=90):
        try:
            tr = self.yt.transcript(video_id(url))
        except Exception as e:
            return None, "no captions (%s)" % type(e).__name__
        segs = [s for s in (tr.get("segments") or []) if (s.get("start") or 0) < seconds]
        lines = [" ".join((s.get("text") or "").split()) for s in segs]
        text = " ".join(l if l[-1:] in ".!?" else l + "." for l in lines if l)
        return (text or None), (None if text else "the video has no captions")

    def creator_breakdown(self, user, me=None, top=5, transcribe=1, platform="instagram"):
        """Everything a creator's recent posts show about what works, step by step, then what *I* should do differently based
        on my own account's numbers. Instagram, YouTube or X. Built to be shown on screen: every step is listed with what was found."""
        from scout_extras import hook_type, first_line, tokens, adjusted
        t0, steps = time.time(), []
        plat = self.ALIASES.get((platform or "instagram").strip().lower(), (platform or "instagram").strip().lower())
        if plat not in self.PLATFORMS:
            raise ScoutError("platform must be instagram, youtube or x", 400)
        L = self.PLATFORMS[plat]

        def step(what, found):
            steps.append({"step": len(steps) + 1, "did": what, "found": found, "at_seconds": round(time.time() - t0, 1)})

        prof = self._profile_rows()
        me = me or prof.get("instagram") or os.environ.get("SCOUT_SHOWCASE_ME", "")   # what they told us, else the server default
        mine_later = self._my_insights_async(me) if me else None
        u = user.strip().lstrip("@").lower()
        if plat == "instagram":
            p = self.ig.profile(u)
        elif plat == "youtube":
            p = self._yt_items(user.strip())
            u = p["username"].lower()
        else:
            p = self._x_items(u)
        posts = p.get("media") or []
        step("Opened @%s on %s" % (u, L["site"]), "%s %s, %s %s in total, read the latest %d%s" % (
            format(p.get("followers") or 0, ","), "subscribers" if plat == "youtube" else "followers",
            format(p.get("media_count") or 0, ","), L["items"] if plat != "instagram" else "posts", len(posts),
            " (cached copy)" if p.get("stale") else ""))
        if plat == "instagram":   # Meta's Business Discovery serves view_count now: rank by real views when most reels have them
            rv = [m for m in posts if m.get("media_product_type") == "REELS"]
            if rv and sum(1 for m in rv if m.get("view_count")) >= 0.6 * len(rv):
                L = dict(L, metric="views", verb="Pulled every recent reel with its views, likes and comments")
                for m in rv:
                    m["_likes"], m["like_count"] = m.get("like_count"), m.get("view_count")
                # view_count includes paid views: a reel with far fewer likes per view than this creator's normal was probably boosted
                ratios = [m["_likes"] / m["like_count"] for m in rv if m.get("_likes") is not None and m.get("like_count")]
                norm = med(ratios) if len(ratios) >= 5 else None
                for m in rv:
                    if norm and m.get("_likes") is not None and m.get("like_count") and m["_likes"] / m["like_count"] < 0.25 * norm:
                        m["_boosted"] = True
        reels = [m for m in posts if m.get("media_product_type") == "REELS" and m.get("like_count") is not None]
        others = [m for m in posts if m not in reels and m.get("like_count") is not None]
        if len(reels) < 3:
            raise ScoutError("@%s has fewer than 3 public %s with %s, nothing to analyse" % (u, L["items"], L["metric"]), 404)
        likes = [m["like_count"] for m in reels]
        median = med(likes) or 1
        for m in reels:
            m["_x"] = round(m["like_count"] / max(median, 1), 1)
            m["_age"] = None
            try:
                m["_age"] = (time.time() - dt.datetime.fromisoformat(str(m["timestamp"]).replace("+0000", "+00:00").replace("Z", "+00:00")).timestamp()) / 86400
            except (KeyError, ValueError, TypeError):
                pass
        step(L["verb"], "%d %s%s. A normal %s gets about %s %s" % (
            len(reels), L["items"], (" and %d other posts" % len(others)) if plat == "instagram" else "", L["item"],
            format(int(median), ","), L["metric"]))
        ranked = sorted(reels, key=lambda m: -m["like_count"])
        best = ranked[:int(top)]
        step("Ranked the %s to find the viral ones" % L["items"],
             "; ".join("%s %s (%sx usual)" % (format(m["like_count"], ","), L["metric"], m["_x"]) for m in best[:3]))

        # my own reels, read in parallel above, so the post we transcribe is the one closest to what I make
        ins, my_words, my_err = None, collections.Counter(), None
        if mine_later:
            ins, my_err = mine_later()
            for r in (ins or {}).get("reels") or []:
                my_words.update(w for w in tokens(r.get("caption") or "")[0] if not w.isdigit())
        for w in tokens("%s %s" % (prof.get("niche", ""), prof.get("audience", "")))[0]:   # my stated niche counts more than one caption
            if not w.isdigit():
                my_words[w] += 3

        def shared(m):
            return sorted((w for w in tokens((m.get("caption") or ""))[0] if my_words.get(w)), key=lambda w: -my_words[w])

        def pick_score(m):
            return m["_x"] * (1 + min(len(shared(m)), 4) * 0.5)
        pool = [m for m in ranked[:max(int(top), 8) + 3] if not m.get("_boosted")][:max(int(top), 8)] or ranked[:max(int(top), 8)]
        pick = max(pool, key=pick_score)
        boosted = [m for m in best if m.get("_boosted")]
        if boosted:
            step("Spotted %d likely paid boost%s" % (len(boosted), "" if len(boosted) == 1 else "s"),
                 "%s: lots of views but very few likes for this creator, so not copied" % "; ".join(
                     '"%s"' % first_line(m.get("caption") or "", 40) for m in boosted[:2]))
        why = "%sx their usual" % pick["_x"]
        if shared(pick):
            why += ", and closest to what you post (%s)" % ", ".join(shared(pick)[:3])
        elif my_words and any(shared(m) for m in pool):
            why += ". Their %s on your topics did worse, so borrow this one's pattern" % L["items"]
        elif my_words:
            why += ". None of their hits overlap your topics, so borrow the pattern, not the subject"
        step("Picked the one %s worth stealing from" % L["item"], '"%s" (%s)' % (first_line(pick.get("caption") or "", 70), why))

        trans, why_not = {}, None
        if plat == "x":
            trans[pick["permalink"]] = pick.get("caption") or ""
            step("Read the post in full", "%d words" % len((pick.get("caption") or "").split()))
        elif int(transcribe) > 0:
            tx = self._yt_transcript if plat == "youtube" else self._transcribe_reel
            for m in [pick] + [m for m in best if m is not pick][:int(transcribe) - 1]:
                text, reason = tx(m["permalink"], seconds=90)
                if text:
                    trans[m["permalink"]] = text
                else:
                    why_not = reason
                    break
        if plat != "x":
            if trans.get(pick["permalink"]):
                t = trans[pick["permalink"]]
                step("Transcribed it word for word", '%d words%s: "%s..."' % (
                    len(t.split()), (", the first 90 seconds" if plat == "youtube" else ", about %d seconds" % round(len(t.split()) / 2.6)), t[:90]))
            else:
                step("Tried to transcribe it", "skipped: %s. Working from the %s instead." % (
                    why_not or ("transcription turned off for this run" if int(transcribe) <= 0 else "no audio"),
                    "title" if plat == "youtube" else "caption and hook"))

        def hook_of(m):
            return first_line(trans.get(m["permalink"]) or m.get("caption") or "", 120)
        types = collections.defaultdict(list)
        for m in reels:
            types[hook_type(hook_of(m))].append(m["_x"])
        type_rows = sorted(({"hook_type": k, "reels": len(v), "median_x": round(med(v), 2)} for k, v in types.items()),
                           key=lambda r: -r["median_x"])
        step("Classified every opening line by hook type",
             ", ".join("%s hooks %sx (%d reels)" % (r["hook_type"], r["median_x"], r["reels"]) for r in type_rows[:3]))

        def slot(m):
            try:
                h = dt.datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))).hour
            except (KeyError, ValueError):
                return None
            return "morning 6-12" if 6 <= h < 12 else "afternoon 12-17" if 12 <= h < 17 else "evening 17-22" if 17 <= h < 22 else "night 22-6"
        slots = collections.defaultdict(list)
        for m in reels:
            if slot(m):
                slots[slot(m)].append(m["_x"])
        slot_rows = sorted(({"slot_ist": k, "reels": len(v), "median_x": round(med(v), 2)} for k, v in slots.items() if len(v) >= 2),
                           key=lambda r: -r["median_x"])
        ages = sorted(m["_age"] for m in reels if m["_age"] is not None)
        per_week = round(7.0 * (len(ages) - 1) / (ages[-1] - ages[0]), 1) if len(ages) > 1 and ages[-1] > ages[0] else None
        step("Checked when and how often they post", "%s %s a week; best slot %s" % (per_week, L["items"], slot_rows[0]["slot_ist"] if slot_rows else "unclear"))

        hook_filler = {"nobody", "talk", "talks", "talking", "secret", "truth", "reason", "never", "stop", "watch", "wait", "listen", "tells",
                       "tell", "told", "says", "said", "lost", "made", "makes", "built", "every", "user", "users", "three", "charges"}
        in_best, in_rest = collections.Counter(), collections.Counter()
        rest = [m for m in reels if m not in best]
        for group, counter in ((best, in_best), (rest, in_rest)):
            for m in group:
                counter.update(w for w in tokens(hook_of(m) + " " + (m.get("caption") or ""))[0]
                               if w not in hook_filler and not w.isdigit())
        win_words = collections.Counter({w: c - in_rest.get(w, 0) * len(best) / max(len(rest), 1) for w, c in in_best.items()
                                         if c >= 2 or len(best) <= 2})
        win_words = collections.Counter({w: v for w, v in win_words.items() if v > 0})
        cap_len = {"short": [], "long": []}
        for m in reels:
            cap_len["short" if len(m.get("caption") or "") < 150 else "long"].append(m["_x"])
        cap_pref = max(cap_len, key=lambda k: med(cap_len[k]) if cap_len[k] else 0)
        talk = [m["_x"] for m in reels if (m.get("comments_count") or 0) >= 0.03 * (m["like_count"] or 1)]
        step("Found the topics and formats that win", "topics: %s; %s %s do better" % (
            ", ".join(w for w, _ in win_words.most_common(5)) or "no single topic stands out", cap_pref,
            "titles" if plat == "youtube" else "posts" if plat == "x" else "captions"))

        them = {"followers": p.get("followers"), "usual_likes": int(median), "reels_per_week": per_week,
                "best_hook": type_rows[0]["hook_type"] if type_rows else None, "best_slot": slot_rows[0]["slot_ist"] if slot_rows else None,
                "caption_length": cap_pref}
        mine, advice = None, []
        if me and ins is None:
            step("Tried to compare with your account", "could not read your insights: %s" % (my_err or "no data"))
        if ins is not None:
            try:
                mr = [r for r in (ins.get("reels") or []) if r.get("views")]
                m_med = med([r["views"] for r in mr]) or 1
                m_types = collections.defaultdict(list)
                m_slots = collections.defaultdict(list)
                m_ages = []
                for r in mr:
                    m_types[hook_type(r.get("caption"))].append(r["views"] / m_med)
                    s2 = slot({"timestamp": r.get("posted") or ""})
                    if s2:
                        m_slots[s2].append(r["views"] / m_med)
                    if r.get("age_days") is not None:
                        m_ages.append(r["age_days"])
                m_ages.sort()
                my_week = round(7.0 * (len(m_ages) - 1) / (m_ages[-1] - m_ages[0]), 1) if len(m_ages) > 1 and m_ages[-1] > m_ages[0] else None
                my_hook = max(m_types, key=lambda k: med(m_types[k])) if m_types else None
                my_slot = max((k for k in m_slots if len(m_slots[k]) >= 2), key=lambda k: med(m_slots[k]), default=None)
                mine = {"account": ins.get("account") or me, "followers": ins.get("followers"), "usual_views": int(m_med),
                        "reels_per_week": my_week, "best_hook": my_hook, "best_slot": my_slot}
                step("Compared with your own account @%s" % mine["account"],
                     "you post %s a week (them %s); your best hook is %s (theirs %s)" % (my_week, per_week, my_hook, them["best_hook"]))
                if per_week and my_week and per_week > my_week * 1.3:
                    advice.append("Post more often: they put out %s %s a week, you %s reels. Consistency is half of their reach." % (per_week, L["items"], my_week))
                if them["best_hook"] and them["best_hook"] != my_hook:
                    ex = next((hook_of(m) for m in best if hook_type(hook_of(m)) == them["best_hook"]), None)
                    advice.append("Open with a %s hook. It's their strongest type%s. Your reels mostly open with %s." % (  # same advice on every platform
                        them["best_hook"], (', like "%s"' % ex.rstrip(".!")) if ex else "", my_hook or "plain statements"))
                if them["best_slot"] and my_slot and them["best_slot"] != my_slot and m_slots.get(them["best_slot"]) is None:
                    advice.append("Test posting in the %s slot. That's when their %s do best, and you've never tried it." % (them["best_slot"], L["items"]))
                if win_words:
                    advice.append("Make your version of their winning topics (%s), in your own angle and words, not a copy."
                                  % ", ".join(w for w, _ in win_words.most_common(3)))
                if cap_pref == "long" and plat == "instagram":
                    advice.append("Write fuller captions: their long-caption reels beat their short ones.")
            except ScoutError as e:
                step("Tried to compare with your account", "could not read your insights: %s" % e)
        if not advice:
            advice.append("Copy the pattern, not the %s: %s hooks, about %s %s a week." % (L["item"], them["best_hook"], per_week, L["items"]))
        src = trans.get(pick["permalink"]) or pick.get("caption") or ""
        sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", " ".join(src.split())) if x.strip()]
        words = len(src.split())
        brief = {"reel": pick["permalink"], "url": pick["permalink"], "likes": pick["like_count"], "metric": L["metric"],
                 "vs_their_usual": pick["_x"], "why_this_one": why,
                 "source": ("post" if plat == "x" else "transcript") if trans.get(pick["permalink"]) else ("title" if plat == "youtube" else "caption"),
                 "text": src[:4000],
                 "hook": sents[0] if sents else hook_of(pick), "hook_type": hook_type(sents[0] if sents else hook_of(pick)),
                 "beats": sents[1:-1][:8], "payoff": sents[-1] if len(sents) > 1 else None,
                 "their_length_seconds": round(words / 2.6) if trans.get(pick["permalink"]) and plat != "x" else None,
                 "shared_topics": shared(pick)[:5], "my_topics": [w for w, _ in my_words.most_common(8)],
                 "my_best_hook": (mine or {}).get("best_hook"),
                 "for_me": {k: prof.get(k) for k in ("instagram", "niche", "audience", "goal", "language") if prof.get(k)},
                 "how_to_write": ("Write ONE post for me in my own voice: keep their structure, hook type and length, but change the "
                                  "subject or angle so it fits my topics. Never reuse their lines. First line is the hook. Then give 2 "
                                  "alternative first lines." if plat == "x" else
                                  "Write ONE spoken reel script for me in my own voice: keep their structure, hook type and pacing, but change "
                                  "the subject or angle so it fits my niche and speaks to my audience (see for_me). Never reuse their lines. Hook "
                                  "in the first 2 seconds, short spoken lines, one idea per line, end with a reason to follow. Then give 2 "
                                  "alternative hooks.")}
        step("Built your script brief from it", "%s hook, %d beats%s%s" % (
            brief["hook_type"], len(brief["beats"]), ", payoff" if brief["payoff"] else "", (", about %ds like theirs" % brief["their_length_seconds"]) if brief["their_length_seconds"] else ""))
        step("Wrote your action plan", "%d moves" % len(advice))
        return {"creator": "@" + u, "platform": plat, "site": L["site"], "unit": L["items"], "metric": L["metric"],
                "name": p.get("name"), "reels_read": len(reels), "seconds": round(time.time() - t0, 1),
                "followers": p.get("followers"), "usual": int(median),
                "distribution": sorted(m["_x"] for m in reels),
                "steps": steps, "viral_reels": [{"url": m["permalink"], "likes": m["like_count"],
                "comments": m.get("comments_count"), "vs_their_usual": m["_x"], "hook": hook_of(m), "hook_type": hook_type(hook_of(m)),
                "transcript": trans.get(m["permalink"]), "posted": m.get("timestamp"), "boosted": bool(m.get("_boosted")),
                "real_likes": m.get("_likes")} for m in best],
                "what_works_for_them": {"hook_types": type_rows, "posting_slots_ist": slot_rows, **them,
                                        "winning_topics": [w for w, _ in win_words.most_common(8)]},
                "you": mine, "what_you_should_do": advice, "script_brief": brief,
                "setup_needed": None if prof.get("niche") and prof.get("audience") else
                "Ask the user once for their niche and target audience (and page, if not given), save with scout_my_profile_set, "
                "then use them for the script.",
                "notes": [{"instagram": "'Viral' means %s against the creator's own usual (from Meta's official Business Discovery API)." % L["metric"],
                           "youtube": "YouTube gives real view counts, so 'viral' means views against the channel's own usual.",
                           "x": "X: 'viral' means likes against their own usual. Source: %s." % (p.get("source") or "X")}[plat]]}

    # ------------------------------------------------------------ 2. retention dips on my videos
    def me_retention(self, channel, video=None, n=5):
        ch = channel
        me = self.mine._me(ch)
        ids = [video_id(video)] if video else [u["id"] for u in self.mine._uploads(ch, me, max(int(n), 1))][:int(n)]
        meta = {}
        items = self.mine._get(ch, "https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails&id=" + ",".join(ids)).get("items") or []
        for it in items:
            meta[it["id"]] = {"title": it["snippet"]["title"], "published": it["snippet"].get("publishedAt"),
                              "duration": parse_duration(it["contentDetails"].get("duration"))}
        out = []
        for v in ids:
            m = meta.get(v, {})
            start = (m.get("published") or "2015-01-01")[:10]
            try:
                rows = self.mine._rep(ch, startDate=start, endDate=str(dt.date.today()), dimensions="elapsedVideoTimeRatio",
                                      metrics="audienceWatchRatio,relativeRetentionPerformance", filters="video==" + v)
            except ScoutError as e:
                out.append({"video": v, "title": m.get("title"), "error": str(e)})
                continue
            if len(rows) < 10:
                out.append({"video": v, "title": m.get("title"), "error": "not enough views yet for a retention curve"})
                continue
            dur = m.get("duration") or 0
            pts = [(float(r["elapsedVideoTimeRatio"]), float(r["audienceWatchRatio"]), r.get("relativeRetentionPerformance")) for r in rows]
            drops = []
            for (t0, w0, _), (t1, w1, _) in zip(pts, pts[1:]):
                if w0 > 0 and (w0 - w1) / w0 >= 0.04:
                    drops.append({"at": _mmss(t1 * dur) if dur else "%d%%" % (t1 * 100), "lost_pct": round(100 * (w0 - w1) / w0, 1),
                                  "ratio": t1})
            drops.sort(key=lambda d: -d["lost_pct"])
            first30 = next((w for t, w, _ in pts if t * dur >= 30), None) if dur else None
            half = next((w for t, w, _ in pts if t >= 0.5), None)
            rel = med([p[2] for p in pts if p[2] is not None])
            tips = []
            if first30 is not None and first30 < 0.6:
                tips.append("Only %d%% are still watching at 0:30. The hook or intro is losing people: start on the payoff." % (first30 * 100))
            if drops and drops[0]["ratio"] > 0.85:
                tips.append("The biggest drop is near the end. Move the call to action earlier or cut the outro.")
            if rel is not None:
                tips.append("Retention is %s similar videos of this length (relative performance %.2f, 0.5 is average)."
                            % ("above" if rel > 0.55 else "below" if rel < 0.45 else "about level with", rel))
            out.append({"video": v, "title": m.get("title"), "duration": _mmss(dur) if dur else None,
                        "still_watching_at_30s_pct": round(first30 * 100, 1) if first30 is not None else None,
                        "still_watching_at_half_pct": round(half * 100, 1) if half is not None else None,
                        "biggest_drops": [{k: d[k] for k in ("at", "lost_pct")} for d in drops[:3]], "tips": tips})
        return {"channel": ch, "videos": out}

    # ------------------------------------------------------------ 3. topic gaps
    def gaps(self, seed=None, niche=None, terms=6, kind="any", fresh_days=90):
        """Seeds: the seed argument, else the niche's "gap_seeds" in page_rules.json, else the niche's query.
        Never guessed from titles. Autocomplete suggestions that share no word with their seed are dropped."""
        from scout_extras import load_rules
        seeds = [seed] if seed else []
        if not seeds and niche:
            name = niche.strip().lower()
            n = next((x for x in self.niches()["niches"] if x["name"] == name), None)
            if not n:
                raise ScoutError("no niche called '%s'" % niche, 404)
            seeds = list(load_rules().get(name, {}).get("gap_seeds") or []) or ([n["query"]] if n.get("query") else [])
            if not seeds:
                raise ScoutError("niche '%s' has no gap seeds. Add \"gap_seeds\": [\"home repair hacks\", ...] for it in "
                                 "~/.scout/page_rules.json, or pass seed=" % name, 400)
        if not seeds:
            raise ScoutError("give a seed search or a niche")
        listing = []
        per = max(int(terms) // len(seeds), 2)
        for sd in seeds:
            words = set(re.findall(r"[a-z0-9]{3,}", sd.lower()))
            sugg = [x for x in self.yt.autocomplete(sd) if x.lower() != sd.lower() and words & set(re.findall(r"[a-z0-9]{3,}", x.lower()))]
            listing += [t for t in [sd] + sugg[:per - 1] if t not in listing]
        seed = ", ".join(seeds)
        rows = []
        for t in listing[:max(int(terms), len(seeds))]:
            try:
                vids = self.yt.search(t, 20, "relevance", None, kind)["videos"]
            except ScoutError as e:
                if e.status == 429:
                    break
                continue
            dated = [v for v in vids if v.get("age_days") is not None]
            if len(vids) < 5:
                continue
            vpd = [v["views"] / max(v["age_days"], 1) for v in dated]
            demand = med([v["views"] for v in vids])          # lifetime views: evergreen demand, not just this week's buzz
            fresh = sum(1 for v in dated if v["age_days"] <= int(fresh_days))
            fresh_share = fresh / len(dated) if dated else None
            channels = len({v.get("channel_id") or v.get("channel") for v in vids})
            stale = (1 - fresh_share) if fresh_share is not None else 0.5
            rows.append({"term": t, "median_views_top20": int(demand or 0), "views_per_day": round(med(vpd), 1) if vpd else None, "fresh_videos_in_top20": fresh if dated else None,
                         "channels_in_top20": channels, "gap_score": round((demand or 0) * max(stale, 0.1) / math.log10(channels + 10) / 1000, 1),
                         "top_result": (vids[0].get("title"), vids[0].get("url"))})
        rows.sort(key=lambda r: -r["gap_score"])
        return {"seed": seed, "gaps": rows, "notes": [
            "gap_score rises with the views the top results got and with how few of them are from the last %d days." % int(fresh_days),
            "High demand with old results means people still search it and nobody has made a fresh version."]}

    # ------------------------------------------------------------ 4. title and thumbnail changes
    def scan_changes(self, max_channels=40, thumbs=True):
        self._growth_tables()
        chans = [c for (c,) in self._q("select channel_id from tracked")]
        for n in self.niches()["niches"]:
            chans += [c for c in n.get("channels") or [] if c not in chans]
        found, now = 0, time.time()
        for c in chans[:int(max_channels)]:
            try:
                info = self.yt.channel_info(c)
                ups = self.yt.recent(info, "any", 15)
            except ScoutError as e:
                if e.status == 429:
                    break
                continue
            for v in ups:
                if (v.get("age_days") or 0) > 60:
                    continue
                th = None
                if thumbs:
                    try:
                        from scout_plus import http_bytes
                        th = hashlib.md5(http_bytes("https://i.ytimg.com/vi/%s/mqdefault.jpg" % v["id"], 10)).hexdigest()
                    except Exception:
                        th = None
                old = self._q("select title, thumb, views from vid_meta where video_id=?", (v["id"],))
                if old:
                    t0, h0, views0 = old[0]
                    if t0 and v["title"] and t0 != v["title"]:
                        self._w("insert or ignore into vid_changes values(?,?,?,?,?,?,?)", (v["id"], now, "title", t0, v["title"], views0, info.get("title")))
                        found += 1
                    if h0 and th and h0 != th:
                        self._w("insert or ignore into vid_changes values(?,?,?,?,?,?,?)", (v["id"], now, "thumbnail", h0, th, views0, info.get("title")))
                        found += 1
                self._w("insert or replace into vid_meta values(?,?,?,?,?,?)",
                        (v["id"], info.get("title"), v["title"], th or (old[0][1] if old else None), v["views"], now))
        return {"channels_scanned": min(len(chans), int(max_channels)), "changes_found": found}

    def changes(self, days=30):
        self._growth_tables()
        rows = self._q("select c.video_id, c.at, c.field, c.before, c.after, c.views_before, c.channel, m.views, m.seen "
                       "from vid_changes c left join vid_meta m on m.video_id=c.video_id where c.at>=? order by c.at desc",
                       (time.time() - int(days) * 86400,))
        out = []
        for vid, at, field, before, after, vb, chan, vnow, seen in rows:
            days_since = max((seen - at) / 86400.0, 0) if seen else 0
            row = {"video": "https://www.youtube.com/watch?v=" + vid, "channel": chan, "changed": field,
                   "when": time.strftime("%Y-%m-%d", time.gmtime(at)), "views_at_change": vb, "views_now": vnow}
            if field == "title":
                row.update(old_title=before, new_title=after)
            else:
                row["thumbnail_now"] = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid
            if vb and vnow and days_since >= 1:
                row["views_per_day_since"] = round((vnow - vb) / days_since)
            out.append(row)
        return {"days": int(days), "changes": out, "notes": [
            "Channels change titles and thumbnails when a video underperforms. Compare views per day since the change with the channel's usual.",
            "Thumbnail changes are detected by image hash; open thumbnail_now to see the new one."]}

    # ------------------------------------------------------------ 5. rising topics
    def rising(self, days=7, min_count=5, limit=25):
        from scout_extras import tokens
        now, span = time.time(), int(days) * 86400
        cur, prev, examples = collections.Counter(), collections.Counter(), {}

        def add(text, ts, url, eng):
            tk, pr = tokens(text)
            bucket = cur if ts >= now - span else prev if ts >= now - 2 * span else None
            if bucket is None:
                return
            for w in tk | {p for p in pr}:
                bucket[w] += 1 + (1 if w in pr else 0)
                if bucket is cur and (w not in examples or eng > examples[w][1]):
                    examples[w] = (url, eng)

        for data, created, seen in self._q("select data, created, seen from social_posts where coalesce(created, seen)>=?", (now - 2 * span,)):
            d = json.loads(data)
            add(d.get("text"), created or seen, d.get("url"), (d.get("likes") or 0) + (d.get("reposts") or 0))
        for day, data in self._q("select day, data from feed where day>=?", (self._today(2 * int(days)),)):
            d = json.loads(data)
            try:
                ts = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() + 43200
            except ValueError:
                continue
            add(d.get("caption") or d.get("title"), ts, d.get("url"), d.get("likes") or d.get("views") or 0)
        oldest = [x for (x,) in self._q("select min(coalesce(created, seen)) from social_posts")] + \
                 [dt.datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()
                  for (d,) in self._q("select min(day) from feed") if d]
        oldest = min([x for x in oldest if x] or [now])
        history = (now - oldest) / 86400.0
        enough = history >= 2 * int(days) * 0.8 and sum(prev.values()) > 0
        rows = []
        for w, c in cur.items():
            if c < int(min_count):
                continue
            p = prev.get(w, 0)
            row = {"term": w, "this_period": c, "last_period": p, "example": examples.get(w, (None,))[0]}
            if enough:
                row["growth"] = round((c + 1) / (p + 1), 1)
                row["new_term"] = p == 0
            rows.append(row)
        rows.sort(key=lambda r: (-r.get("growth", 0), -r["this_period"]))
        notes = []
        if not enough:
            notes.append("Only %.1f days of history, so there's no earlier period to compare. These are this period's most "
                         "mentioned terms, not growth. Growth shows once %d days of data exist." % (history, 2 * int(days)))
        return {"days": int(days), "history_days": round(history, 1), "has_growth": enough, "rising": rows[:int(limit)], "notes": notes + [
            "Counts mentions across your scroll bots and every niche feed. growth = this period against the one before.",
            "Names count double. A term jumping from near zero is a wave starting; catch it before it peaks."]}

    # ------------------------------------------------------------ 6. sponsor finder
    def sponsors(self, niche=None, days=60, limit=30):
        texts = []
        for day, n, data in self._q("select day, niche, data from feed where day>=?", (self._today(int(days)),)):
            if niche and n != niche.strip().lower():
                continue
            d = json.loads(data)
            if d.get("description"):
                texts.append((d.get("channel") or "", d.get("url"), d["description"], n))
        seen_v, brands, codes, domains = set(), collections.defaultdict(set), collections.defaultdict(set), collections.defaultdict(set)
        for chan, url, desc, n in texts:
            if url in seen_v:
                continue
            seen_v.add(url)
            for rx, bucket in ((SPONSOR_RE[0], brands), (SPONSOR_RE[1], codes)):
                for m in rx.finditer(desc):
                    bucket[m.group(1).strip(" .").strip()].add(chan)
            for host in DOMAIN_RE.findall(desc):
                root = _root(host)
                if root not in SOCIAL and not any(root.endswith(s) for s in SOCIAL):
                    domains[root].add(chan)
        def top(d):
            return sorted(({"name": k, "channels": sorted(v)[:8], "channel_count": len(v)} for k, v in d.items()),
                          key=lambda x: -x["channel_count"])[:int(limit)]
        return {"niche": niche, "videos_read": len(seen_v), "named_sponsors": top(brands), "promo_codes": top(codes),
                "linked_domains": top(domains), "notes": [
                    "Brands that pay creators in this niche. Domains used by several channels are the strongest leads.",
                    "Reads descriptions of YouTube outliers in the feed. Instagram captions rarely name paid partners in text."]}
