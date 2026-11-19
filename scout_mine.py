#!/usr/bin/env python3
"""Scout Mine: your own channels and pages, from your own OAuth tokens.

YouTube: reuses the YouTube OAuth files (youtube_client.json plus youtube_token_<name>.json,
scopes yt-analytics.readonly and youtube.readonly). Nothing new to sign in.
Instagram: uses the same token pool as Scout, reading each connected account's own insights.

Race to monetization, scorecard, achievements, audience and subscriber insights, Instagram insights.
Tokens never leave this module: no answer, error or log line carries one.
"""
import datetime as dt
import glob
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from scout_core import ScoutError, med, age_days, HOME, UA

YT_ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
YT_DATA = "https://www.googleapis.com/youtube/v3/"

SUB_MILESTONES = [100, 500, 1000, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000]
VIEW_MILESTONES = [10000, 100000, 1000000, 10000000, 100000000, 1000000000]
VIDEO_MILESTONES = [10000, 100000, 1000000, 10000000]


def _http(url, data=None, headers=None, timeout=40):
    req = urllib.request.Request(url, data=data, headers=dict({"User-Agent": UA}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf8", "ignore"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf8", "ignore")
        try:
            msg = json.loads(body).get("error", {})
            msg = msg.get("message") if isinstance(msg, dict) else (msg or body)
        except ValueError:
            msg = body
        raise ScoutError("%s %s" % (e.code, re.sub(r"(access_token|refresh_token)=[^&\s]+", r"\1=***", str(msg))[:240]),
                         401 if e.code in (400, 401) else 429 if e.code == 429 else 502)
    except (urllib.error.URLError, TimeoutError) as e:
        raise ScoutError("network error: " + str(getattr(e, "reason", e)), 502)


def _pct(have, need):
    return round(min(100.0, 100.0 * (have or 0) / need), 1)


def _eta(need_left, per_day):
    if need_left <= 0:
        return 0
    return int(need_left / per_day) + 1 if per_day and per_day > 0 else None


def _change(now, before):
    if now is None or before in (None, 0):
        return None
    return round(100.0 * (now - before) / before, 1)


class Mine:
    def __init__(self, cache, ig, oauth_dir=None):
        self.cache, self.ig = cache, ig
        self.dir = oauth_dir or os.environ.get("SCOUT_YT_OAUTH_DIR") or os.path.join(HOME, ".scout", "youtube_oauth")
        self._access = {}
        with cache.lock:
            cache.db.execute("create table if not exists achievements(channel text, key text, reached_at real, primary key(channel, key))")
            cache.db.commit()

    # ---------------------------------------------------------------- YouTube auth
    def channel_names(self):
        names = []
        for p in sorted(glob.glob(os.path.join(self.dir, "youtube_token_*.json"))):
            names.append(os.path.basename(p)[len("youtube_token_"):-len(".json")])
        return names

    def _client(self):
        p = os.path.join(self.dir, "youtube_client.json")
        if not os.path.exists(p):
            raise ScoutError("no youtube_client.json in %s. Set SCOUT_YT_OAUTH_DIR to the folder with your OAuth files." % self.dir, 501)
        with open(p, encoding="utf8") as f:
            c = json.load(f)
        c = c.get("installed") or c.get("web") or c
        return c["client_id"], c.get("client_secret", "")

    def _token(self, ch):
        if ch not in self.channel_names():
            raise ScoutError("no OAuth token for channel '%s'. Connected: %s" % (ch, ", ".join(self.channel_names()) or "none"), 404)
        hit = self._access.get(ch)
        if hit and hit[1] > time.time() + 60:
            return hit[0]
        with open(os.path.join(self.dir, "youtube_token_%s.json" % ch), encoding="utf8") as f:
            t = json.load(f)
        if not t.get("refresh_token"):
            raise ScoutError("token file for '%s' has no refresh token. Sign that channel in again." % ch, 401)
        cid, secret = self._client()
        d = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "refresh_token": t["refresh_token"],
                                    "grant_type": "refresh_token"}).encode()
        n = _http("https://oauth2.googleapis.com/token", data=d)
        self._access[ch] = (n["access_token"], time.time() + int(n.get("expires_in", 3600)))
        return n["access_token"]

    def _get(self, ch, url):
        return _http(url, headers={"Authorization": "Bearer " + self._token(ch)})

    def _rep(self, ch, **q):
        q.setdefault("ids", "channel==MINE")
        r = self._get(ch, YT_ANALYTICS + "?" + urllib.parse.urlencode(q))
        cols = [c["name"] for c in r.get("columnHeaders", [])]
        return [dict(zip(cols, row)) for row in r.get("rows", []) or []]

    def _one(self, ch, **q):
        return (self._rep(ch, **q) or [{}])[0]

    def _me(self, ch):
        items = self._get(ch, YT_DATA + "channels?part=snippet,statistics,contentDetails&mine=true").get("items") or []
        if not items:
            raise ScoutError("this token has no YouTube channel", 404)
        it = items[0]
        st = it.get("statistics", {})
        return {"id": it["id"], "title": it["snippet"]["title"], "published": it["snippet"].get("publishedAt"),
                "subscribers": int(st.get("subscriberCount", 0) or 0), "views": int(st.get("viewCount", 0) or 0),
                "videos": int(st.get("videoCount", 0) or 0), "uploads": it["contentDetails"]["relatedPlaylists"]["uploads"]}

    def _uploads(self, ch, me, n=50):
        items = self._get(ch, YT_DATA + "playlistItems?part=contentDetails&maxResults=%d&playlistId=%s" % (n, me["uploads"])).get("items") or []
        return [{"id": i["contentDetails"]["videoId"], "published": i["contentDetails"].get("videoPublishedAt")} for i in items]

    def _titles(self, ch, ids):
        ids = [i for i in ids if i][:50]
        if not ids:
            return {}
        items = self._get(ch, YT_DATA + "videos?part=snippet,contentDetails&id=" + ",".join(ids)).get("items") or []
        return {v["id"]: v["snippet"]["title"] for v in items}

    @staticmethod
    def _days(n_back, length):
        end = dt.date.today() - dt.timedelta(days=1 + n_back)
        return str(end - dt.timedelta(days=length - 1)), str(end)

    def _cached(self, key, fn, ttl=3 * 3600):
        return self.cache.cached("mine:" + key, ttl, fn)

    def _split(self, ch, start, end, metrics):
        """Views and minutes split into Shorts and long-form. Falls back to a single total if the dimension is refused."""
        try:
            rows = self._rep(ch, startDate=start, endDate=end, dimensions="creatorContentType", metrics=metrics)
            out = {"shorts": {}, "long": {}, "split": True}
            for r in rows:
                k = "shorts" if r.get("creatorContentType") == "shorts" else "long"
                for m in metrics.split(","):
                    out[k][m] = out[k].get(m, 0) + (r.get(m) or 0)
            return out
        except ScoutError:
            tot = self._one(ch, startDate=start, endDate=end, metrics=metrics)
            return {"shorts": {}, "long": tot, "split": False}

    # ---------------------------------------------------------------- YouTube features
    def channels(self):
        out = []
        for ch in self.channel_names():
            try:
                me = self._cached("me:" + ch, lambda c=ch: self._me(c))
                out.append({"channel": ch, "title": me["title"], "subscribers": me["subscribers"], "views": me["views"], "videos": me["videos"]})
            except ScoutError as e:
                out.append({"channel": ch, "error": str(e)})
        return {"channels": out, "oauth_dir_found": os.path.isdir(self.dir)}

    def race(self, channel):
        def go():
            me = self._me(channel)
            s365, e = self._days(0, 365)
            s90, _ = self._days(0, 90)
            s28, _ = self._days(0, 28)
            y = self._split(channel, s365, e, "views,estimatedMinutesWatched")
            q = self._split(channel, s90, e, "views")
            m = self._split(channel, s28, e, "views,estimatedMinutesWatched")
            net28 = self._one(channel, startDate=s28, endDate=e, metrics="subscribersGained,subscribersLost")
            net_day = ((net28.get("subscribersGained") or 0) - (net28.get("subscribersLost") or 0)) / 28.0
            hours = (y["long"].get("estimatedMinutesWatched") or 0) / 60.0
            hours_day = (m["long"].get("estimatedMinutesWatched") or 0) / 60.0 / 28.0
            shorts90 = q["shorts"].get("views") or 0
            shorts_day = (m["shorts"].get("views") or 0) / 28.0
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
            uploads90 = sum(1 for u in self._uploads(channel, me) if u["published"] and
                            dt.datetime.fromisoformat(u["published"].replace("Z", "+00:00")) >= cutoff)
            subs = me["subscribers"]

            def path(name, subs_need, other_name, have, need, per_day, steady):
                left_s, left_o = subs_need - subs, need - have
                eta = [_eta(left_s, net_day), _eta(left_o, per_day)]
                return {"path": name, "subscribers": {"have": subs, "need": subs_need, "pct": _pct(subs, subs_need)},
                        other_name: {"have": round(have, 1), "need": need, "pct": _pct(have, need), "steady_state_at_current_pace": round(steady)},
                        "met": subs >= subs_need and have >= need,
                        "eta_days": None if None in eta else max(eta)}

            paths = [
                path("Full YPP, long-form: 1,000 subs and 4,000 watch hours in 365 days", 1000, "watch_hours_365d", hours, 4000, hours_day, hours_day * 365),
                path("Full YPP, Shorts: 1,000 subs and 10M Shorts views in 90 days", 1000, "shorts_views_90d", shorts90, 10000000, shorts_day, shorts_day * 90),
                path("Early YPP, long-form: 500 subs, 3 uploads in 90 days, 3,000 watch hours", 500, "watch_hours_365d", hours, 3000, hours_day, hours_day * 365),
                path("Early YPP, Shorts: 500 subs, 3 uploads in 90 days, 3M Shorts views", 500, "shorts_views_90d", shorts90, 3000000, shorts_day, shorts_day * 90),
            ]
            for p in paths[2:]:
                p["uploads_90d"] = {"have": uploads90, "need": 3}
                p["met"] = p["met"] and uploads90 >= 3
            best = min((p for p in paths if p["eta_days"] is not None), key=lambda p: p["eta_days"], default=None)
            notes = ["eta_days assumes the last 28 days' pace holds and ignores old views rolling out of the window. "
                     "steady_state shows where the rolling total settles at today's pace. If it's below the bar, that path won't close without growth.",
                     "YouTube counts public long-form watch hours only. Scout splits Shorts out with the creatorContentType dimension."]
            if not y["split"]:
                notes.append("YouTube refused the Shorts split for this channel, so watch hours include Shorts and read high.")
            return {"channel": channel, "title": me["title"], "subscribers": subs, "net_subs_per_day": round(net_day, 1),
                    "watch_hours_per_day": round(hours_day, 1), "shorts_views_per_day": round(shorts_day),
                    "paths": paths, "fastest_path": best["path"] if best else None, "eligible_now": any(p["met"] for p in paths), "notes": notes}
        return self._cached("race:" + channel, go)

    def scorecard(self, channel):
        def go():
            me = self._me(channel)
            M = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained,subscribersLost"
            s1, e1 = self._days(0, 28)
            s0, e0 = self._days(28, 28)
            cur, prev = self._one(channel, startDate=s1, endDate=e1, metrics=M), self._one(channel, startDate=s0, endDate=e0, metrics=M)
            ups = self._uploads(channel, me)
            now = dt.datetime.now(dt.timezone.utc)

            def count(a, b):
                return sum(1 for u in ups if u["published"] and a <= (now - dt.datetime.fromisoformat(u["published"].replace("Z", "+00:00"))).days < b)

            up_now, up_before = count(0, 28), count(28, 56)

            def eng(r):
                v = r.get("views") or 0
                return round(100.0 * ((r.get("likes") or 0) + (r.get("comments") or 0) + (r.get("shares") or 0)) / v, 2) if v else None

            def net(r):
                return (r.get("subscribersGained") or 0) - (r.get("subscribersLost") or 0)

            rows = [("views", cur.get("views"), prev.get("views")),
                    ("watch_hours", round((cur.get("estimatedMinutesWatched") or 0) / 60, 1), round((prev.get("estimatedMinutesWatched") or 0) / 60, 1)),
                    ("avg_view_seconds", cur.get("averageViewDuration"), prev.get("averageViewDuration")),
                    ("avg_view_pct", round(cur.get("averageViewPercentage") or 0, 1), round(prev.get("averageViewPercentage") or 0, 1)),
                    ("net_subscribers", net(cur), net(prev)), ("engagement_pct", eng(cur), eng(prev)), ("uploads", up_now, up_before)]
            metrics = [{"metric": n, "last_28d": a, "previous_28d": b, "change_pct": _change(a, b)} for n, a, b in rows]
            score = 50.0
            for n in ("views", "watch_hours", "net_subscribers"):
                c = next(x["change_pct"] for x in metrics if x["metric"] == n)
                if c is not None:
                    score += max(-15, min(15, c / 2.0))
            avp = cur.get("averageViewPercentage") or 0
            score += 10 if avp >= 50 else 5 if avp >= 35 else -5 if avp < 25 else 0
            score += 5 if up_now >= 4 else -10 if up_now == 0 else 0
            score = int(max(0, min(100, round(score))))
            top = self._rep(channel, startDate=s1, endDate=e1, dimensions="video", sort="-views", maxResults=5,
                            metrics="views,estimatedMinutesWatched,averageViewPercentage,subscribersGained")
            names = self._titles(channel, [r["video"] for r in top])
            traffic = self._rep(channel, startDate=s1, endDate=e1, dimensions="insightTrafficSourceType", metrics="views", sort="-views")
            tv = sum(t["views"] for t in traffic) or 1
            return {"channel": channel, "title": me["title"], "window": [s1, e1], "score": score,
                    "grade": "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D" if score >= 35 else "E",
                    "metrics": metrics,
                    "top_videos_28d": [dict(r, title=names.get(r["video"]), url="https://www.youtube.com/watch?v=" + r["video"]) for r in top],
                    "traffic_share_pct": {t["insightTrafficSourceType"]: round(100.0 * t["views"] / tv, 1) for t in traffic[:8]},
                    "notes": ["Score: 50 base, up to 15 each for growth in views, watch hours and net subscribers, plus retention and upload consistency.",
                              "Impressions and click-through rate are only in YouTube Studio. The API doesn't expose them."]}
        return self._cached("score:" + channel, go)

    def achievements(self, channel):
        me = self._me(channel)
        s, e = (me["published"] or "2005-04-23")[:10], self._days(0, 1)[1]
        best = (self._rep(channel, startDate=s, endDate=e, dimensions="video", sort="-views", maxResults=1, metrics="views") or [{}])[0]
        best_views = best.get("views") or 0
        known = {k for (k,) in self._q("select key from achievements where channel=?", (channel,))}
        first_run = not known
        out = []
        for label, have, steps in (("subscribers", me["subscribers"], SUB_MILESTONES), ("lifetime views", me["views"], VIEW_MILESTONES),
                                   ("views on one video", best_views, VIDEO_MILESTONES)):
            reached = [m for m in steps if have >= m]
            nxt = next((m for m in steps if have < m), None)
            for m in reached:
                key = "%s:%d" % (label, m)
                if key not in known:
                    self._w("insert or ignore into achievements values(?,?,?)", (channel, key, time.time()))
            new = [] if first_run else [m for m in reached if "%s:%d" % (label, m) not in known]
            out.append({"track": label, "have": have, "reached": reached, "new_since_last_check": new,
                        "next": nxt, "pct_to_next": _pct(have, nxt) if nxt else 100.0})
        return {"channel": channel, "title": me["title"], "achievements": out,
                "notes": ["new_since_last_check fills in from the second check on. The daily job checks every channel."]}

    def _q(self, sql, args=()):
        with self.cache.lock:
            return self.cache.db.execute(sql, args).fetchall()

    def _w(self, sql, args=()):
        with self.cache.lock:
            self.cache.db.execute(sql, args)
            self.cache.db.commit()

    def audience(self, channel, days=28):
        """Every section is fetched on its own: YouTube Analytics returns 500 for some reports on small channels
        (demographics below its privacy threshold), and that must not sink the rest. Failed sections come back empty,
        with the reason in "unavailable"."""
        def go():
            s, e = self._days(0, int(days))
            unavailable = {}

            def rep(name, **q):
                try:
                    return self._rep(channel, startDate=s, endDate=e, **q)
                except ScoutError as err:
                    if err.status in (401, 403) and "scope" in str(err).lower():
                        raise
                    unavailable[name] = str(err)
                    return []
            demo = rep("age_gender", dimensions="ageGroup,gender", metrics="viewerPercentage", sort="gender,ageGroup")
            countries = rep("countries", dimensions="country", metrics="views,estimatedMinutesWatched", sort="-views", maxResults=10)
            subbed = rep("subscribed_vs_not", dimensions="subscribedStatus", metrics="views,estimatedMinutesWatched,averageViewPercentage")
            devices = rep("devices", dimensions="deviceType", metrics="views", sort="-views")
            traffic = rep("traffic_sources", dimensions="insightTrafficSourceType", metrics="views,estimatedMinutesWatched", sort="-views")
            search = rep("search_terms", dimensions="insightTrafficSourceDetail", metrics="views", sort="-views", maxResults=15,
                         filters="insightTrafficSourceType==YT_SEARCH")
            subs_by_video = rep("videos_that_bring_subscribers", dimensions="video", sort="-subscribersGained", maxResults=10,
                                metrics="subscribersGained,views")
            names = {}
            if subs_by_video:
                try:
                    names = self._titles(channel, [r["video"] for r in subs_by_video])
                except ScoutError:
                    pass
            ages, genders = {}, {}
            for r in demo:
                ages[r["ageGroup"]] = round(ages.get(r["ageGroup"], 0) + r["viewerPercentage"], 1)
                genders[r["gender"]] = round(genders.get(r["gender"], 0) + r["viewerPercentage"], 1)
            for r in subs_by_video:
                r["title"] = names.get(r["video"])
                r["subs_per_1k_views"] = round(1000.0 * r["subscribersGained"] / r["views"], 2) if r.get("views") else None
            total = sum(r.get("views") or 0 for r in traffic) or 1
            for r in traffic:
                r["share_pct"] = round(100.0 * (r.get("views") or 0) / total, 1)
            if len(unavailable) >= 6:
                raise ScoutError("YouTube Analytics returned nothing for this channel in the last %s days (%s). Small or brand-new "
                                 "channels often have no data yet." % (days, next(iter(unavailable.values()))), 404)
            notes = ["videos_that_bring_subscribers is the strongest signal for what to make more of: high subs_per_1k_views converts viewers into fans.",
                     "Impressions and click-through rate: use scout_my_reach.",
                     "Analytics runs 1 to 3 days behind. For videos from the last few days use scout_my_live (minutes old)."]
            if unavailable:
                notes.append("Some sections are empty because YouTube didn't return them (usually too little data for privacy thresholds).")
            return {"channel": channel, "window": [s, e], "age_pct": ages, "gender_pct": genders, "top_countries": countries,
                    "subscribed_vs_not": subbed, "devices": devices, "traffic_sources": traffic, "top_search_terms": search,
                    "videos_that_bring_subscribers": subs_by_video, "unavailable": unavailable, "notes": notes}
        return self._cached("aud2:%s:%s" % (channel, days), go)

    # ---------------------------------------------------------------- Instagram (own accounts)
    def _ig_get(self, t, path, **q):
        q["access_token"] = t["token"]
        return _http("https://graph.facebook.com/%s/%s?%s" % (self.ig.cfg.ig_version, path, urllib.parse.urlencode(q)))

    def _ig_pick(self, account):
        pool = self.ig.pool()
        if not pool:
            raise ScoutError("no Instagram token found. Set SCOUT_IG_POOL.", 501)
        if not account:
            return pool
        a = account.strip().lstrip("@").lower()
        hit = [t for t in pool if t["label"].lower() == a or t["ig_user_id"] == a]
        if not hit:
            for t in pool:
                try:
                    if self._ig_profile(t)["username"].lower() == a:
                        hit = [t]
                        break
                except ScoutError:
                    continue
        if not hit:
            raise ScoutError("no connected Instagram account matches '%s'" % account, 404)
        return hit

    def _ig_profile(self, t):
        return self.cache.cached("mine:igme:" + t["ig_user_id"], 6 * 3600,
                                 lambda: self._ig_get(t, t["ig_user_id"], fields="username,name,followers_count,follows_count,media_count"))

    def ig_accounts(self):
        out = []
        for t in self.ig.pool():
            try:
                p = self._ig_profile(t)
                out.append({"label": t["label"], "username": p.get("username"), "followers": p.get("followers_count"), "posts": p.get("media_count")})
            except ScoutError as e:
                out.append({"label": t["label"], "error": str(e)})
        return {"accounts": out}

    def ig_insights(self, account=None, days=28, reels=25):
        tokens = self._ig_pick(account)
        if len(tokens) > 1:
            return {"accounts": [self.ig_insights(t["label"], days, reels) for t in tokens]}
        t = tokens[0]

        def go():
            p = self._ig_profile(t)
            until = int(time.time())
            since = until - min(int(days), 30) * 86400
            acct, errors = {}, []
            for m in ("reach", "views", "accounts_engaged", "total_interactions", "follows_and_unfollows", "profile_links_taps"):
                try:
                    r = self._ig_get(t, t["ig_user_id"] + "/insights", metric=m, period="day", metric_type="total_value", since=since, until=until)
                    d = (r.get("data") or [{}])[0]
                    tv = d.get("total_value") or {}
                    acct[m] = tv.get("value") if "breakdowns" not in tv else tv
                except ScoutError as e:
                    errors.append("%s: %s" % (m, e))
            demo = {}
            for b in ("country", "age", "gender"):
                try:
                    r = self._ig_get(t, t["ig_user_id"] + "/insights", metric="follower_demographics", period="lifetime",
                                     metric_type="total_value", timeframe="this_month", breakdown=b)
                    res = (((r.get("data") or [{}])[0].get("total_value") or {}).get("breakdowns") or [{}])[0].get("results") or []
                    rows = sorted(({"key": x["dimension_values"][0], "followers": x["value"]} for x in res), key=lambda x: -x["followers"])
                    demo[b] = rows[:10]
                except (ScoutError, KeyError, IndexError) as e:
                    errors.append("demographics %s: %s" % (b, e))
            media = self._ig_get(t, t["ig_user_id"] + "/media", limit=50,
                                 fields="id,caption,media_product_type,timestamp,permalink,like_count,comments_count").get("data") or []
            rl = [m for m in media if m.get("media_product_type") == "REELS"][:int(reels)]
            rows = []
            for m in rl:
                row = {"url": m.get("permalink"), "caption": " ".join((m.get("caption") or "").split())[:500], "posted": m.get("timestamp"),
                       "likes": m.get("like_count"), "comments": m.get("comments_count")}
                try:
                    ins = self._ig_get(t, m["id"] + "/insights", metric="views,reach,saved,shares,total_interactions,ig_reels_avg_watch_time")
                    for d in ins.get("data") or []:
                        row[d["name"]] = (d.get("values") or [{}])[0].get("value")
                    if row.get("ig_reels_avg_watch_time") is not None:
                        row["avg_watch_seconds"] = round(row.pop("ig_reels_avg_watch_time") / 1000.0, 1)
                except ScoutError as e:
                    row["error"] = str(e)
                a = age_days((m.get("timestamp") or "").replace("+0000", "+00:00"))
                row["age_days"] = round(a, 1) if a is not None else None
                rows.append(row)
            with_views = [r for r in rows if r.get("views")]

            def rate(k):
                xs = [100.0 * r[k] / r["views"] for r in with_views if r.get(k) is not None]
                return round(med(xs), 2) if xs else None

            recent = [r["views"] for r in with_views if r["age_days"] is not None and r["age_days"] <= 14]
            older = [r["views"] for r in with_views if r["age_days"] is not None and 14 < r["age_days"] <= 28]
            summary = {"median_views": int(med([r["views"] for r in with_views])) if with_views else None,
                       "save_rate_pct": rate("saved"), "share_rate_pct": rate("shares"),
                       "median_avg_watch_seconds": med([r.get("avg_watch_seconds") for r in with_views]),
                       "median_views_last_14d": int(med(recent)) if recent else None,
                       "median_views_14_to_28d": int(med(older)) if older else None}
            summary["trend_pct"] = _change(summary["median_views_last_14d"], summary["median_views_14_to_28d"])
            return {"account": p.get("username"), "label": t["label"], "followers": p.get("followers_count"), "posts": p.get("media_count"),
                    "account_insights_%dd" % min(int(days), 30): acct, "follower_demographics": demo, "reels_summary": summary,
                    "top_reels": sorted(with_views, key=lambda r: -r["views"])[:5], "reels": rows, "reels_read": len(rows), "errors": errors,
                    "notes": ["Shares and saves per view are the strongest Instagram signals. Make more of what has the highest share rate."]}
        return self.cache.cached("mine:igins:%s:%s:%s" % (t["ig_user_id"], days, reels), 3 * 3600, go)
