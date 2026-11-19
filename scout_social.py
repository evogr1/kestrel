#!/usr/bin/env python3
"""Scout Social: X and Facebook.

X
  Free, no key : stats for any public post (likes, replies, video views) through X's embed endpoint.
  X API key    : profile outliers and search through the official X API v2 (pay per use, about $0.005 per post read).
                 Set X_BEARER_TOKEN. A daily cap (SCOUT_X_DAILY_POSTS, default 1000 posts, about $5) stops runaway spend.
Facebook
  Your own Pages through the same Meta token pool Scout already uses: followers, page insights, posts and reels with
  plays, and which reels beat the Page's own median. Other people's Pages need Meta's Page Public Content Access
  review, so Scout doesn't read them.
"""
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from scout_core import ScoutError, med, age_days, UA

X_API = "https://api.x.com/2/"
X_TIMELINE = "https://syndication.twitter.com/srv/timeline-profile/screen-name/"
X_EMBED = "https://cdn.syndication.twimg.com/tweet-result?lang=en&token=a&id="


def _http(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=dict({"User-Agent": "Mozilla/5.0 (" + UA + ")"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf8", "ignore"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf8", "ignore")
        try:
            j = json.loads(body)
            msg = j.get("detail") or j.get("title") or (j.get("error") or {}).get("message") or body
        except ValueError:
            msg = body
        msg = re.sub(r"(access_token|token)=[^&\s]+", r"\1=***", str(msg))[:220]
        raise ScoutError("%s %s" % (e.code, msg), 429 if e.code == 429 else 404 if e.code == 404 else 401 if e.code in (401, 403) else 502)
    except (urllib.error.URLError, TimeoutError) as e:
        raise ScoutError("network error: " + str(getattr(e, "reason", e)), 502)


def _http_text(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (" + UA + ")", "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf8", "ignore")
    except urllib.error.HTTPError as e:
        raise ScoutError("X answered %s" % e.code, 429 if e.code == 429 else 502)
    except (urllib.error.URLError, TimeoutError) as e:
        raise ScoutError("network error: " + str(getattr(e, "reason", e)), 502)


def _x_time(s):
    """'Wed Oct 10 20:19:24 +0000 2018' or ISO -> ISO."""
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return s


def parse_x_embed(html, user):
    """The public profile embed (no login, no API) carries the latest posts as JSON in __NEXT_DATA__."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html or "", re.S)
    if not m:
        return None
    try:
        entries = json.loads(m.group(1))["props"]["pageProps"]["timeline"]["entries"]
    except (ValueError, KeyError, TypeError):
        return None
    posts, followers, count = [], None, None
    for e in entries:
        t = ((e or {}).get("content") or {}).get("tweet") or {}
        if not t.get("id_str"):
            continue
        u = t.get("user") or {}
        if (u.get("screen_name") or "").lower() != user.lower() or t.get("retweeted_status") or (t.get("full_text") or "").startswith("RT @"):
            continue
        followers, count = u.get("followers_count", followers), u.get("statuses_count", count)
        text = re.sub(r"\s*https://t\.co/\w+$", "", t.get("full_text") or t.get("text") or "").strip()
        media = ((t.get("extended_entities") or t.get("entities") or {}).get("media") or [])
        posts.append({"url": "https://x.com/%s/status/%s" % (u.get("screen_name") or user, t["id_str"]), "text": text,
                      "likes": t.get("favorite_count"), "reposts": t.get("retweet_count"), "replies": t.get("reply_count"),
                      "created": _x_time(t.get("created_at")),
                      "kind": "video" if any(x.get("type") in ("video", "animated_gif") for x in media) else "image" if media else "text"})
    return {"posts": posts, "followers": followers, "post_count": count, "source": "public X embed"}


def post_id(ref):
    ref = (ref or "").strip()
    if re.fullmatch(r"\d{5,25}", ref):
        return ref
    m = re.search(r"(?:x|twitter)\.com/[^/]+/status(?:es)?/(\d{5,25})", ref)
    if not m:
        raise ScoutError("cannot read an X post id from: " + ref[:80])
    return m.group(1)


def handle(u):
    u = (u or "").strip()
    m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", u)
    u = m.group(1) if m else u.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", u):
        raise ScoutError("not an X handle: " + u[:40])
    return u


class X:
    def __init__(self, cache):
        self.cache = cache
        self.bearer = os.environ.get("X_BEARER_TOKEN", "")
        self.daily_posts = int(os.environ.get("SCOUT_X_DAILY_POSTS", "1000"))

    @property
    def has_api(self):
        return bool(self.bearer)

    # ---- free: one post
    def post(self, ref):
        pid = post_id(ref)

        def go():
            d = _http(X_EMBED + pid)
            if not d or d.get("__typename") == "TweetTombstone" or not d.get("id_str"):
                raise ScoutError("post not found or not public", 404)
            u = d.get("user") or {}
            video = d.get("video") or {}
            created = d.get("created_at")
            a = age_days(created)
            return {"id": pid, "url": "https://x.com/%s/status/%s" % (u.get("screen_name", "i"), pid),
                    "author": "@" + u.get("screen_name", ""), "author_name": u.get("name"), "verified": bool(u.get("is_blue_verified")),
                    "text": d.get("text", ""), "likes": d.get("favorite_count"), "replies": d.get("conversation_count"),
                    "video_views": (int(video["viewCount"]) or None) if str(video.get("viewCount", "")).isdigit() else None,
                    "has_video": bool(video), "has_photo": bool(d.get("photos")), "created": created,
                    "age_days": round(a, 1) if a is not None else None,
                    "thumbnail": video.get("poster") or ((d.get("photos") or [{}])[0].get("url"))}
        return self.cache.cached("xpost:" + pid, 3600, go)

    def posts(self, refs):
        out, errors = [], []
        for r in refs[:20]:
            try:
                out.append(self.post(r))
            except ScoutError as e:
                errors.append({"ref": r, "error": str(e)})
        return {"posts": out, "errors": errors, "notes": ["Free route: likes, replies and video views. Reposts and impressions for text posts need X_BEARER_TOKEN."]}

    # ---- API
    def _need_api(self):
        if not self.has_api:
            raise ScoutError("this needs the official X API. Set X_BEARER_TOKEN (pay per use, about $0.005 per post read, "
                             "with SCOUT_X_DAILY_POSTS as a daily cap). Single posts work free through /v1/x/post.", 501)

    def _api(self, path, posts_expected=0, **q):
        self._need_api()
        used = self.cache.usage("x_posts")
        if posts_expected and used + posts_expected > self.daily_posts:
            raise ScoutError("X daily read cap reached (%d posts). Raise SCOUT_X_DAILY_POSTS or wait for tomorrow." % self.daily_posts, 429)
        d = _http(X_API + path + "?" + urllib.parse.urlencode(q), headers={"Authorization": "Bearer " + self.bearer})
        n = len(d.get("data") or []) if isinstance(d.get("data"), list) else 0
        if n:
            self.cache.add_usage("x_posts", n)
        if path.startswith("users/by"):
            self.cache.add_usage("x_users", 1)
        return d

    @staticmethod
    def _norm(t, user=None):
        pm = t.get("public_metrics") or {}
        a = age_days(t.get("created_at"))
        u = user or {}
        return {"id": t["id"], "url": "https://x.com/%s/status/%s" % (u.get("username", "i"), t["id"]),
                "author": "@" + u.get("username", "") if u else None, "text": t.get("text", ""),
                "views": pm.get("impression_count"), "likes": pm.get("like_count"), "reposts": pm.get("retweet_count"),
                "replies": pm.get("reply_count"), "quotes": pm.get("quote_count"), "bookmarks": pm.get("bookmark_count"),
                "created": t.get("created_at"), "age_days": round(a, 1) if a is not None else None,
                "has_media": bool((t.get("attachments") or {}).get("media_keys"))}

    def profile(self, user):
        h = handle(user)

        def go():
            d = self._api("users/by/username/" + h, **{"user.fields": "public_metrics,created_at,description,verified"})
            u = d.get("data")
            if not u:
                raise ScoutError("X account not found: @" + h, 404)
            pm = u.get("public_metrics") or {}
            return {"id": u["id"], "username": u["username"], "name": u.get("name"), "followers": pm.get("followers_count"),
                    "following": pm.get("following_count"), "posts": pm.get("tweet_count"), "created": u.get("created_at"),
                    "bio": u.get("description")}
        return self.cache.cached("xprof:" + h.lower(), 24 * 3600, go)

    def timeline(self, user, n=50):
        p = self.profile(user)

        def go():
            d = self._api("users/%s/tweets" % p["id"], posts_expected=n, max_results=max(5, min(int(n), 100)),
                          exclude="retweets,replies", **{"tweet.fields": "public_metrics,created_at,attachments"})
            return [self._norm(t, {"username": p["username"]}) for t in d.get("data") or []]
        return p, self.cache.cached("xtl:%s:%d" % (p["id"], n), 6 * 3600, go)

    def user_posts(self, user, n=50):
        """A creator's recent original posts, for breakdowns. Tries, in order: the X API (only if X_BEARER_TOKEN is set),
        the free public profile embed, then posts the scroll bots already saved for that account."""
        h, errors = handle(user), []
        if self.bearer:
            try:
                p, tl = self.timeline(h, n)
                return {"posts": [{"url": t["url"], "text": t["text"], "likes": t["likes"], "reposts": t["reposts"], "replies": t["replies"],
                                   "created": t["created"], "kind": "media" if t.get("has_media") else "text"} for t in tl],
                        "followers": p.get("followers"), "post_count": p.get("posts"), "source": "X API"}
            except ScoutError as e:
                errors.append("X API: %s" % e)
        try:
            d = self.cache.cached("xembed:" + h.lower(), 2 * 3600, lambda: parse_x_embed(_http_text(X_TIMELINE + h), h) or {})
            if d.get("posts"):
                return d
            errors.append("public embed: no posts returned")
        except ScoutError as e:
            errors.append("public embed: %s" % e)
        try:
            with self.cache.lock:
                rows = self.cache.db.execute("select url, data, created from social_posts where platform='x' and lower(author) in (?, ?) "
                                             "order by coalesce(created, seen) desc limit ?", (h.lower(), "@" + h.lower(), n)).fetchall()
        except Exception:
            rows = []
        posts = []
        for url, data, created in rows:
            try:
                d = json.loads(data or "{}")
            except ValueError:
                d = {}
            if d.get("likes") is not None:
                posts.append({"url": url, "text": d.get("text") or "", "likes": d.get("likes"), "reposts": d.get("reposts"),
                              "replies": d.get("replies"), "created": dt.datetime.fromtimestamp(created, dt.timezone.utc).isoformat() if created else None,
                              "kind": d.get("kind") or "text"})
        if len(posts) >= 3:
            return {"posts": posts, "followers": None, "post_count": None, "source": "posts your scroll bots saved"}
        raise ScoutError("couldn't read @%s on X (%s). X shut its free API; set X_BEARER_TOKEN for the paid one." % (h, "; ".join(errors)), 502)

    def outliers(self, users, min_multiple=2.0, max_age_days=30, limit=30, n=50):
        if not users:
            raise ScoutError("give X handles")
        self._need_api()
        rows, errors, read = [], [], 0
        for u in users[:15]:
            try:
                p, tl = self.timeline(u, n)
            except ScoutError as e:
                errors.append({"user": u, "error": str(e)})
                if e.status in (429, 501):
                    break
                continue
            metric = "views" if any(t["views"] for t in tl) else "likes"
            base = [t for t in tl if t["age_days"] is None or t["age_days"] >= 1]
            if len(base) < 5:
                errors.append({"user": u, "error": "fewer than 5 older posts, baseline too thin"})
                continue
            read += 1
            for t in tl:
                if t["age_days"] is not None and t["age_days"] > max_age_days:
                    continue
                m = med([b[metric] for b in base if b["id"] != t["id"]]) or 1
                mult = (t[metric] or 0) / max(m, 1)
                if mult >= min_multiple:
                    rows.append(dict(t, author="@" + p["username"], followers=p["followers"], multiple=round(mult, 1),
                                     scored_on=metric, account_median=int(m)))
        rows.sort(key=lambda r: -r["multiple"])
        return {"results": rows[:limit], "accounts_read": read, "errors": errors, "x_posts_read_today": self.cache.usage("x_posts"),
                "notes": ["Scored on views (impressions) against the account's own median, or likes when views are hidden."]}

    def search(self, query, limit=30, max_results=50):
        q = query.strip()
        if " -is:retweet" not in q:
            q += " -is:retweet -is:reply"

        def go():
            d = self._api("tweets/search/recent", posts_expected=max_results, query=q, max_results=max(10, min(int(max_results), 100)),
                          sort_order="relevancy", expansions="author_id",
                          **{"tweet.fields": "public_metrics,created_at,author_id,attachments", "user.fields": "public_metrics,username"})
            users = {u["id"]: u for u in (d.get("includes") or {}).get("users", [])}
            out = []
            for t in d.get("data") or []:
                u = users.get(t.get("author_id"), {})
                r = self._norm(t, u)
                f = (u.get("public_metrics") or {}).get("followers_count")
                r["followers"] = f
                r["views_per_follower"] = round(r["views"] / f, 2) if r.get("views") and f else None
                out.append(r)
            return out
        rows = self.cache.cached("xsearch:" + q.lower(), 6 * 3600, go)
        rows = sorted(rows, key=lambda r: -(r["views_per_follower"] or 0))
        return {"query": query, "results": rows[:limit], "x_posts_read_today": self.cache.usage("x_posts"),
                "notes": ["Recent search covers the last 7 days. views_per_follower above 1 means the post reached well past the author's followers."]}


class Facebook:
    """Your own Facebook Pages through the Meta token pool."""

    def __init__(self, cache, ig):
        self.cache, self.ig = cache, ig
        self._page_tokens = {}

    def _get(self, path, token, **q):
        q["access_token"] = token
        return _http("https://graph.facebook.com/%s/%s?%s" % (self.ig.cfg.ig_version, path, urllib.parse.urlencode(q)))

    def _pages(self):
        if self._page_tokens and self._page_tokens.get("_at", 0) > time.time() - 3600:
            return self._page_tokens["pages"]
        pages, seen = {}, set()
        for t in self.ig.pool():
            if t["token"] in seen:
                continue
            seen.add(t["token"])
            try:
                d = self._get("me/accounts", t["token"], fields="id,name,access_token,followers_count,fan_count", limit=100)
            except ScoutError:
                continue
            for p in d.get("data") or []:
                pages.setdefault(p["id"], p)
        if not pages:
            raise ScoutError("no Facebook Pages visible to the Meta tokens in the pool", 404)
        self._page_tokens = {"pages": pages, "_at": time.time()}
        return pages

    def pages(self):
        return {"pages": [{"id": p["id"], "name": p["name"], "followers": p.get("followers_count"), "likes": p.get("fan_count")}
                          for p in self._pages().values()]}

    def _pick(self, page):
        pages = self._pages()
        key = (page or "").strip().lower()
        for p in pages.values():
            if p["id"] == key or p["name"].lower() == key:
                return p
        hits = [p for p in pages.values() if key and key in p["name"].lower()]
        if len(hits) == 1:
            return hits[0]
        raise ScoutError("no single Page matches '%s'. Pages: %s" % (page, ", ".join(p["name"] for p in pages.values())), 404)

    def page(self, page, days=28):
        p = self._pick(page)
        tok = p["access_token"]

        def go():
            errors = []
            until = int(time.time())
            since = until - min(int(days), 90) * 86400
            ins = {}
            for m in ("page_media_view", "page_total_media_view_unique", "page_post_engagements", "page_daily_follows_unique", "page_video_views"):
                try:
                    r = self._get(p["id"] + "/insights", tok, metric=m, period="day", since=since, until=until)
                    vals = [(v.get("value") or 0) for d in r.get("data") or [] for v in d.get("values") or [] if isinstance(v.get("value"), (int, float))]
                    ins[m] = sum(vals)
                except ScoutError as e:
                    errors.append("%s: %s" % (m, e))
            posts = []
            try:
                d = self._get(p["id"] + "/posts", tok, limit=50, fields="id,message,created_time,permalink_url,shares,"
                              "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)")
                for x in d.get("data") or []:
                    a = age_days(x.get("created_time", "").replace("+0000", "+00:00"))
                    r_ = ((x.get("reactions") or {}).get("summary") or {}).get("total_count", 0)
                    c_ = ((x.get("comments") or {}).get("summary") or {}).get("total_count", 0)
                    s_ = (x.get("shares") or {}).get("count", 0)
                    posts.append({"url": x.get("permalink_url"), "text": " ".join((x.get("message") or "").split())[:140],
                                  "reactions": r_, "comments": c_, "shares": s_, "engagement": r_ + c_ + s_,
                                  "age_days": round(a, 1) if a is not None else None})
            except ScoutError as e:
                errors.append("posts: skipped (needs pages_read_user_content, not granted to the Scout app; reels and insights still work). %s" % e)
            reels = []
            try:
                d = self._get(p["id"] + "/video_reels", tok, limit=25, fields="id,description,created_time,permalink_url,length")
                for v in d.get("data") or []:
                    row = {"url": "https://www.facebook.com" + v["permalink_url"] if str(v.get("permalink_url", "")).startswith("/") else v.get("permalink_url"),
                           "text": " ".join((v.get("description") or "").split())[:140], "seconds": v.get("length")}
                    a = age_days(v.get("created_time", "").replace("+0000", "+00:00"))
                    row["age_days"] = round(a, 1) if a is not None else None
                    try:
                        vi = self._get(v["id"] + "/video_insights", tok, metric="blue_reels_play_count,post_impressions_unique,post_video_avg_time_watched")
                        for m in vi.get("data") or []:
                            row[m["name"]] = (m.get("values") or [{}])[0].get("value")
                        row["plays"] = row.pop("blue_reels_play_count", None)
                        row["reach"] = row.pop("post_impressions_unique", None)
                        t = row.pop("post_video_avg_time_watched", None)
                        row["avg_watch_seconds"] = round(t / 1000.0, 1) if isinstance(t, (int, float)) else None
                    except ScoutError as e:
                        row["error"] = str(e)
                    reels.append(row)
            except ScoutError as e:
                errors.append("reels: %s" % e)

            def outl(items, key):
                xs = [i for i in items if i.get(key)]
                if len(xs) < 5:
                    return []
                out = []
                for i in xs:
                    m = med([o[key] for o in xs if o is not i]) or 1
                    if i[key] / m >= 2:
                        out.append(dict(i, multiple=round(i[key] / m, 1)))
                return sorted(out, key=lambda r: -r["multiple"])[:10]

            return {"page": p["name"], "id": p["id"], "followers": p.get("followers_count"), "likes": p.get("fan_count"),
                    "insights_%dd" % min(int(days), 90): ins, "posts_read": len(posts), "reels_read": len(reels),
                    "median_post_engagement": med([x["engagement"] for x in posts]) if posts else None,
                    "median_reel_plays": med([r["plays"] for r in reels if r.get("plays")]) if reels else None,
                    "post_outliers": outl(posts, "engagement"), "reel_outliers": outl(reels, "plays"),
                    "top_reels": sorted([r for r in reels if r.get("plays")], key=lambda r: -r["plays"])[:5], "errors": errors,
                    "notes": ["Outliers beat the Page's own median by 2x or more.",
                              "Other people's Pages need Meta's Page Public Content Access review, so Scout reads only Pages you manage."]}
        return self.cache.cached("fbpage:%s:%s" % (p["id"], days), 3 * 3600, go)


# ---------------------------------------------------------------- posts pushed in by your own scroll bots

PLATFORMS = ("x", "linkedin", "reddit")
METRICS = ("views", "likes", "reposts", "comments", "saves")


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().lower().replace(",", "")
    m = re.fullmatch(r"([\d.]+)\s*([km]?)", s)
    if not m:
        return None
    n = float(m.group(1)) * {"": 1, "k": 1000, "m": 1000000}[m.group(2)]
    return int(n)


class BotFeed:
    """Stores posts your Chrome scroll bots push in, and finds outliers in them. No platform API involved."""

    def __init__(self, cache):
        self.cache = cache
        with cache.lock:
            cache.db.execute("create table if not exists social_posts(platform text, url text, author text, community text, created real,"
                             " seen real, data text, primary key(platform, url))")
            cache.db.execute("create index if not exists social_author on social_posts(platform, author)")
            cache.db.commit()

    def _q(self, sql, args=()):
        with self.cache.lock:
            return self.cache.db.execute(sql, args).fetchall()

    def ingest(self, platform, posts):
        platform = (platform or "").strip().lower()
        if platform == "twitter":
            platform = "x"
        if platform not in PLATFORMS:
            raise ScoutError("platform must be one of: " + ", ".join(PLATFORMS))
        if not isinstance(posts, list) or not posts:
            raise ScoutError("posts must be a non-empty list")
        if len(posts) > 500:
            raise ScoutError("send at most 500 posts per call")
        now, saved, skipped = time.time(), 0, []
        with self.cache.lock:
            for i, p in enumerate(posts):
                if not isinstance(p, dict) or not str(p.get("url", "")).startswith("http"):
                    skipped.append(i)
                    continue
                author = str(p.get("author") or "").strip().lstrip("@").lower() or None
                community = str(p.get("community") or p.get("subreddit") or "").strip().lower().replace("r/", "") or None
                created = None
                if p.get("created"):
                    a = age_days(str(p["created"]))
                    created = now - a * 86400 if a is not None else None
                row = {"url": p["url"], "author": author, "community": community, "text": " ".join(str(p.get("text") or "").split())[:500],
                       "author_followers": _num(p.get("author_followers")), "media": p.get("media"), "created": p.get("created")}
                for m in METRICS:
                    row[m] = _num(p.get(m))
                old = self.cache.db.execute("select data, created from social_posts where platform=? and url=?", (platform, p["url"])).fetchone()
                if old:  # keep the latest numbers, never lose a field the bot didn't send this time
                    prev = json.loads(old[0])
                    for k, v in row.items():
                        if v is None and prev.get(k) is not None:
                            row[k] = prev[k]
                    created = created or old[1]
                self.cache.db.execute("insert or replace into social_posts values(?,?,?,?,?,?,?)",
                                      (platform, p["url"], author, community, created, now, json.dumps(row)))
                saved += 1
            self.cache.db.execute("delete from social_posts where seen < ?", (now - 120 * 86400,))
            self.cache.db.commit()
        return {"platform": platform, "saved": saved, "skipped": skipped}

    def _rows(self, platform, max_age_days):
        cutoff = time.time() - max_age_days * 86400
        out = []
        for data, created, seen in self._q("select data, created, seen from social_posts where platform=? and coalesce(created, seen) >= ?",
                                           (platform, cutoff)):
            r = json.loads(data)
            r["age_days"] = round((time.time() - (created or seen)) / 86400, 1)
            out.append(r)
        return out

    def outliers(self, platform="x", authors=None, communities=None, min_multiple=2.0, max_age_days=30, limit=30, by=None):
        platform = "x" if platform == "twitter" else (platform or "x").lower()
        if platform not in PLATFORMS:
            raise ScoutError("platform must be one of: " + ", ".join(PLATFORMS))
        by = by or ("community" if platform == "reddit" else "author")
        rows = self._rows(platform, max(max_age_days, 60))  # a longer window for the baseline
        want_a = {a.strip().lstrip("@").lower() for a in (authors or []) if a.strip()}
        want_c = {c.strip().lower().replace("r/", "") for c in (communities or []) if c.strip()}
        groups = {}
        for r in rows:
            k = r.get(by)
            if not k:
                continue
            if (want_a and r.get("author") not in want_a and by == "author") or (want_c and r.get("community") not in want_c and by == "community"):
                continue
            groups.setdefault(k, []).append(r)
        out, thin = [], []
        for k, items in groups.items():
            metric = next((m for m in ("views", "likes", "comments") if sum(1 for i in items if i.get(m)) >= 5), None)
            if not metric:
                thin.append(k)
                continue
            for i in items:
                if i["age_days"] > max_age_days or not i.get(metric):
                    continue
                m = med([o[metric] for o in items if o is not i and o.get(metric)]) or 1
                mult = i[metric] / max(m, 1)
                if mult >= min_multiple:
                    out.append(dict(i, platform=platform, multiple=round(mult, 1), scored_on=metric, baseline=int(m), baseline_group=by,
                                    baseline_posts=len(items)))
        out.sort(key=lambda r: -r["multiple"])
        notes = ["Scored against the median of the same %s's other posts your bots have seen." % by]
        if thin:
            notes.append("%d %ss skipped: fewer than 5 posts with numbers so far. They fill in as the bots keep scrolling." % (len(thin), by))
        return {"platform": platform, "results": out[:limit], "groups_scored": len(groups) - len(thin), "notes": notes}

    def top(self, platform="x", days=7, metric="likes", limit=30, authors=None, communities=None):
        rows = self._rows(platform, days)
        want_a = {a.strip().lstrip("@").lower() for a in (authors or []) if a.strip()}
        want_c = {c.strip().lower().replace("r/", "") for c in (communities or []) if c.strip()}
        rows = [r for r in rows if (not want_a or r.get("author") in want_a) and (not want_c or r.get("community") in want_c)]
        if metric not in METRICS:
            raise ScoutError("metric must be one of: " + ", ".join(METRICS))
        rows.sort(key=lambda r: -(r.get(metric) or 0))
        return {"platform": platform, "days": days, "metric": metric, "results": rows[:limit]}

    def stats(self):
        out = {}
        for platform, n, a, last in self._q("select platform, count(*), count(distinct author), max(seen) from social_posts group by platform"):
            out[platform] = {"posts": n, "authors": a, "last_push": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last)) if last else None}
        return out
