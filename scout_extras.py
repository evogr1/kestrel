#!/usr/bin/env python3
"""Kestrel extras: turns the research into action.

  brief        morning idea cards per page (optionally pushed to Trello)
  watch        early-breakout alerts in the first 24 to 48 hours
  hooks        library of opening lines that worked, with hook types
  stories      the same story showing up across X, Reddit, LinkedIn and Instagram
  postmortem   how each of your own reels did against your median, and why
  comments     questions and requests from comments on outlier YouTube videos
  besttime     best posting hours per page from your own data
  dashboard    a server-rendered page of all of the above for the team

All optional delivery (Trello, alert webhook) is switched on by env vars and fails soft.
"""
import collections
import datetime as dt
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from scout_core import ScoutError, med, video_id, age_days, CALLER
from scout_plus import ScoutPlus
from scout_growth import Growth

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo(os.environ.get("SCOUT_TZ", "UTC"))
    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
    PACIFIC = dt.timezone(dt.timedelta(hours=-8))

FLOOR = float(os.environ.get("SCOUT_MEDIAN_FLOOR", "0.001"))  # baseline can't be lower than 0.1% of followers

STOP = set("""mere mera meri tera teri nahi nahin hai hain kya kaise kyun aur bhi yeh woh isko usko bhai yaar sabse apne apna apni kuch bahut abhi
a an the and or but if then than so of to in on at by for with from into onto over under about as is are was were be been being
this that these those it its it's he she they them their his her we our you your i me my mine not no yes do does did done has have had
will would can could should may might must just also very more most less much many some any all each every other such only own same too
here there when where why how what which who whom whose while after before again once up down out off now new one two three get got make
made like via amp https http www com reel reels video videos watch follow comment link bio share post posts says said today year years
people person time times world first last best life money need needs want wants know think going look right really still good great
thing things work works week weeks month months day days back even ever never always something someone everyone everything another
take takes come comes give gives keep keeps start started thank thanks love lovely watch way ways part full real true big small high low
says said told tell news story stories live update updates breaking latest official india's says amid""".split())

HOOK_TYPES = [
    ("question", lambda t, low: "?" in t[:160]),
    ("warning", lambda t, low: any(w in low[:120] for w in ("stop ", "don't", "never ", "mistake", "warning", "avoid", "before you"))),
    ("secret", lambda t, low: any(w in low[:120] for w in ("nobody", "no one", "secret", "hidden", "truth", "they don't", "real reason"))),
    ("breaking", lambda t, low: "🚨" in t[:20] or low.startswith(("breaking", "just in")) or _caps(t[:60]) > 0.6),
    ("story", lambda t, low: bool(re.match(r"(in \d{4}|around \d{4}|when |this (man|woman|guy|kid|teen)|meet |a \w+ from )", low))),
    ("number", lambda t, low: bool(re.search(r"(^|\s)[\d₹$]", t[:80]))),
    ("you", lambda t, low: bool(re.match(r"(you|your|if you)\b", low))),
    ("how-to", lambda t, low: low.startswith(("how ", "here's how", "the way to", "step "))),
]


def _caps(s):
    letters = [c for c in s if c.isalpha()]
    return sum(c.isupper() for c in letters) / len(letters) if len(letters) >= 8 else 0


def hook_type(text):
    t = " ".join((text or "").split())
    low = t.lower()
    for name, test in HOOK_TYPES:
        if test(t, low):
            return name
    return "statement"


def first_line(text, n=140):
    t = " ".join((text or "").split())
    m = re.match(r"(.{12,}?[.!?])(\s|$)", t)
    return (m.group(1) if m else t)[:n]


def tokens(text):
    out, proper = set(), set()
    words = re.findall(r"[A-Za-z][A-Za-z'\-]+|\d{3,}", text or "")
    for i, w in enumerate(words):
        lw = w.lower().strip("'-")
        if lw in STOP or (len(lw) < 4 and not lw.isdigit()):
            continue
        out.add(lw)
        if w[0].isupper() and i > 0 and not w.isupper():
            proper.add(lw)
    return out, proper


def adjusted(row):
    """Multiple against a floored baseline, capped, so neither a tiny median nor a tiny account can inflate the score.

    Baseline is the largest of: the account's median, 0.1% of its followers (SCOUT_MEDIAN_FLOOR) and an absolute
    minimum (SCOUT_MIN_BASE_LIKES=300 likes, SCOUT_MIN_BASE_VIEWS=3000 views). Anything above SCOUT_MULTIPLE_CAP (50)
    is shown as the cap; rank() then orders those by raw likes or views.
    """
    e = os.environ.get
    if "account_median_likes" in row:
        base, hits, size, floor = row.get("account_median_likes") or 1, row.get("likes") or 0, row.get("followers") or 0, float(e("SCOUT_MIN_BASE_LIKES", "300"))
    else:
        base, hits, size, floor = row.get("channel_median_views") or 1, row.get("views") or 0, row.get("channel_subscribers") or 0, float(e("SCOUT_MIN_BASE_VIEWS", "3000"))
    base = max(base, size * float(e("SCOUT_MEDIAN_FLOOR", str(FLOOR))), floor, 1)
    return round(min(hits / base, float(e("SCOUT_MULTIPLE_CAP", "50"))), 1)


def rank_key(r):
    return (-r["adj_multiple"], -(r.get("likes") or r.get("views") or 0))


def load_rules():
    """Per-page brief rules from SCOUT_PAGE_RULES (default ~/.scout/page_rules.json). Missing file means no rules."""
    path = os.path.expanduser(os.environ.get("SCOUT_PAGE_RULES", "~/.scout/page_rules.json"))
    try:
        with open(path, encoding="utf8") as f:
            raw = json.load(f)
        return {str(k).lower(): v for k, v in raw.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


BAIT_RE = re.compile(r"(\bcomment\s+[\"'\u201c\u2018]?[A-Za-z0-9]{2,15}[\"'\u201d\u2019]?\s*(?:for|to get|and|&|below|if)\b|"
                     r"\bcomment\s+[\"'\u201c\u2018][A-Za-z0-9]{2,15}[\"'\u201d\u2019]|\bi'?ll (?:send|dm) you\b|\blink in (?:my )?bio\b|"
                     r"\bdm me\b|\bfree (?:pdf|guide|e-?book|template|frameworks?|checklist)\b|\btag (?:a friend|someone)\b)", re.I)


def is_bait(it):
    """Engagement bait: 'comment WIN for my free frameworks', 'link in bio', or a comment pile-up the caption asked for."""
    text = " ".join(str(it.get(k) or "") for k in ("title", "caption", "text"))
    if BAIT_RE.search(text):
        return True
    likes, comments = it.get("likes") or 0, it.get("comments") or 0
    return bool(likes and comments / likes > 0.25 and re.search(r"\bcomment\b", text, re.I))


def _word_rx(w):
    """Whole-word match. 'india' doesn't match 'Indiana'; 'india*' does (prefix)."""
    w = w.strip().lower()
    tail = "" if w.endswith("*") else r"(?![a-z0-9])"
    return re.compile(r"(?<![a-z0-9])" + re.escape(w.rstrip("*")) + tail)


def check(it, rule):
    """None when a feed item fits a page's rules, else the reason it was cut. Keys (all optional): sources, include_any,
    exclude_any, exclude_accounts, min_multiple, min_likes, min_views, max_age_days, allow_bait, english_only,
    skip_unscored. Words match whole words;
    end a word with * to match it as a prefix."""
    rule = rule or {}
    text = " ".join(str(it.get(k) or "") for k in ("title", "caption", "text")).lower()
    who = str(it.get("account") or it.get("channel") or it.get("author") or "").lower().lstrip("@")
    if rule.get("sources") and it.get("source") not in rule["sources"]:
        return "source %s not in %s" % (it.get("source"), "/".join(rule["sources"]))
    if rule.get("include_any") and not any(_word_rx(w).search(text) for w in rule["include_any"]):
        return "none of the include words"
    for w in rule.get("exclude_any") or []:
        if _word_rx(w).search(text):
            return "exclude word '%s'" % w
    if who and who in {a.lower().lstrip("@") for a in rule.get("exclude_accounts") or []}:
        return "excluded account @%s" % who
    for key, field in (("min_multiple", "adj_multiple"), ("min_likes", "likes"), ("min_views", "views")):
        if rule.get(key) is not None and (it.get(field) or 0) < float(rule[key]):
            return "%s %s below %s" % (field, it.get(field) or 0, rule[key])
    if rule.get("max_age_days") is not None and (it.get("age_days") or 0) > float(rule["max_age_days"]):
        return "older than %s days" % rule["max_age_days"]
    if rule.get("english_only"):
        letters = [c for c in text if c.isalpha()]
        if letters and sum(1 for c in letters if ord(c) > 0x24F) / len(letters) > 0.3:
            return "not in English"
    if rule.get("skip_unscored") and it.get("source") in ("reddit", "linkedin", "x") and not it.get("adj_multiple"):
        return "no engagement numbers"
    if not rule.get("allow_bait") and os.environ.get("SCOUT_BRIEF_ALLOW_BAIT") != "1" and is_bait(it):
        return "engagement bait"
    return None


def passes(it, rule):
    return check(it, rule) is None


def _post_json(url, payload, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "kestrel/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
        return json.loads(b) if b else {}


class Kestrel(Growth, ScoutPlus):
    def __init__(self, cfg=None):
        super().__init__(cfg)
        with self.cache.lock:
            ex = self.cache.db.execute
            ex("create table if not exists brief_sent(niche text, item_id text, day text, card text, primary key(niche, item_id))")
            ex("create table if not exists watch_state(key text primary key, checked real)")
            ex("create table if not exists early_alerts(item_id text primary key, first_seen real, niche text, data text)")
            ex("create table if not exists hooks(item_id text primary key, niche text, kind text, url text, hook text, spoken text,"
               " type text, multiple real, seen real, data text)")
            self.cache.db.commit()
        self._growth_tables()
        self._post = _post_json

    def ig_disconnect(self):
        """Revoke this Kestrel user's Instagram connection and all Kestrel MCP tokens for the identity."""
        from scout_core import CALLER, ScoutError
        user_id = getattr(CALLER, "user_id", None)
        if not user_id:
            raise ScoutError("Instagram disconnect requires an authenticated Kestrel OAuth session.", 401)
        import scout_auth
        import scout_oauth
        result = scout_auth.disconnect(user_id)
        scout_oauth.revoke_user(user_id)
        return {
            "disconnected": bool(result.get("disconnected")),
            "note": "Instagram credentials were removed from Kestrel and all Kestrel OAuth sessions were revoked. Remove the Kestrel connector from your AI client if you no longer need it.",
        }

    # ------------------------------------------------------------ ranked, deduped feed
    def ranked(self, day=None, niche=None, kind=None, limit=60):
        f = self.feed(day=day, niche=niche, kind=kind, limit=1000)
        by = {}
        for it in f["items"]:
            key = it.get("url") or it.get("id")
            if key in by:
                if it["niche"] not in by[key]["niches"]:
                    by[key]["niches"].append(it["niche"])
                continue
            by[key] = dict(it, niches=[it["niche"]], adj_multiple=adjusted(it))
        items = sorted(by.values(), key=rank_key)
        return {"day": f["day"], "items": items[:int(limit)], "duplicates_merged": len(f["items"]) - len(by),
                "notes": ["adj_multiple uses a baseline of at least %.1f%% of followers or subscribers." % (FLOOR * 100)]}

    # ------------------------------------------------------------ 1. morning brief
    def _card(self, it, niche):
        text = it.get("title") or it.get("caption") or it.get("text") or ""
        hook = self._q("select hook, spoken, type from hooks where item_id=?", (it.get("url") or it.get("id"),))
        name = first_line(text, 90) or "Untitled"
        src = it.get("account") or it.get("channel") or it.get("author") or ""
        stats = ("%s likes, %s views" % (it.get("likes"), it.get("views"))) if it.get("views") else "%s likes" % it.get("likes")
        lines = ["Source: %s (%s)" % (src, it.get("source")), "Numbers: %s, %sx its usual (adjusted)" % (stats, it["adj_multiple"]),
                 "Age: %s days" % it.get("age_days")]
        if hook:
            lines.append("Hook (%s): %s" % (hook[0][2], hook[0][1] or hook[0][0]))
        lines += ["", "Why it worked: check the first 3 seconds and the angle. Make our version, don't copy.", "", it.get("url") or ""]
        return {"page": niche, "name": "[%s] %s" % (niche, name), "desc": "\n".join(lines), "url": it.get("url"), "item_id": it.get("url") or it.get("id"),
                "adj_multiple": it["adj_multiple"]}

    def brief(self, day=None, per_page=5, min_multiple=None, push=False):
        """One card per item: a reel in several niches goes to the first page whose rules it fits and that still has room,
        so pages that share a Trello list never get the same card twice. Every cut is listed with its reason.

        min_multiple: page rule first, then this argument, then SCOUT_BRIEF_MIN_MULTIPLE (default 2).
        With SCOUT_JUDGE_URL set, each page's candidates are scored for fit by that endpoint (your own judge model)."""
        r = self.ranked(day=day, limit=1000)
        rules = load_rules()
        floor = float(min_multiple if min_multiple is not None else os.environ.get("SCOUT_BRIEF_MIN_MULTIPLE", "2"))
        cands = collections.OrderedDict((n["name"], []) for n in self.niches()["niches"])
        cut = []
        for it in r["items"]:
            key = it.get("url") or it.get("id")
            if self._q("select 1 from brief_sent where item_id=?", (key,)):
                continue
            for n in it["niches"]:
                if n not in cands:
                    continue
                rule = dict({"min_multiple": floor}, **rules.get(n, {}))
                room = int(rule.get("per_page", per_page)) * (3 if os.environ.get("SCOUT_JUDGE_URL") else 1)
                if len(cands[n]) >= room:
                    continue
                why = check(it, rule)
                if why:
                    cut.append({"page": n, "url": it.get("url"), "text": first_line(it.get("title") or it.get("caption") or it.get("text"), 90),
                                "adj_multiple": it["adj_multiple"], "reason": why})
                    continue
                cands[n].append(it)
                break
        pages, judged = collections.OrderedDict(), {}
        for n, items in cands.items():
            rule = rules.get(n, {})
            cap = int(rule.get("per_page", per_page))
            if items and os.environ.get("SCOUT_JUDGE_URL") and not getattr(CALLER, "user_id", None):
                items, info = self._judge(n, rule, items)
                judged[n] = info
                cut += [dict(c, page=n) for c in info.pop("rejected", [])]
            pages[n] = [self._card(it, n) for it in items[:cap]]
        reasons = collections.Counter((c["page"], c["reason"].split(" '")[0].split(" below")[0]) for c in cut)
        out = {"day": r["day"], "pages": [{"page": p, "cards": c} for p, c in pages.items()], "pushed": 0, "errors": [],
               "cut": cut[:200], "cut_summary": [{"page": p, "reason": w, "items": k} for (p, w), k in reasons.most_common()],
               "rules_loaded": sorted(rules), "judge": judged or None}
        if push:
            out.update(self._push_trello(pages))
        return out

    def _judge(self, page, rule, items):
        """POST candidates to SCOUT_JUDGE_URL as {page, about, items:[{id, text, account, source, likes, views, adj_multiple}]};
        expects {scores:[{id, fit (0-1), reason}]}. Keeps items with fit >= rule.min_fit (default 0.6), best fit first.
        If the judge is down, the items pass unjudged."""
        payload = {"page": page, "about": rule.get("about") or "", "items": [
            {"id": it.get("url") or it.get("id"), "text": it.get("title") or it.get("caption") or it.get("text") or "",
             "account": it.get("account") or it.get("channel"), "source": it.get("source"), "likes": it.get("likes"),
             "views": it.get("views"), "adj_multiple": it["adj_multiple"]} for it in items]}
        try:
            res = self._post(os.environ["SCOUT_JUDGE_URL"], payload)
            scores = {s["id"]: s for s in res.get("scores", []) if isinstance(s, dict) and "id" in s}
        except Exception as e:
            return items, {"status": "unavailable (%s), items passed unjudged" % type(e).__name__}
        need = float(rule.get("min_fit", 0.6))
        keep, rejected = [], []
        for it in items:
            s = scores.get(it.get("url") or it.get("id"))
            if s is None:
                continue
            if float(s.get("fit", 0)) >= need:
                keep.append((float(s["fit"]), it))
            else:
                rejected.append({"url": it.get("url"), "text": first_line(it.get("title") or it.get("caption") or "", 90),
                                 "adj_multiple": it["adj_multiple"], "reason": "judge: %s (fit %.2f)" % (s.get("reason") or "poor fit", float(s.get("fit", 0)))})
        keep.sort(key=lambda x: -x[0])
        return [it for _, it in keep], {"status": "ok", "scored": len(scores), "kept": len(keep), "rejected": rejected}

    def _push_trello(self, pages):
        if getattr(CALLER, "user_id", None):
            return {"pushed": 0, "errors": ["Trello push is only available to the server operator"]}
        key, tok = os.environ.get("SCOUT_TRELLO_KEY", ""), os.environ.get("SCOUT_TRELLO_TOKEN", "")
        try:
            lists = json.loads(os.environ.get("SCOUT_TRELLO_LISTS", "{}"))
        except ValueError:
            lists = {}
        if not (key and tok and lists):
            return {"pushed": 0, "errors": ["Trello not set up: set SCOUT_TRELLO_KEY, SCOUT_TRELLO_TOKEN and SCOUT_TRELLO_LISTS"]}
        pushed, errors, day = 0, [], self._today()
        for page, cards in pages.items():
            lid = lists.get(page)
            if not lid:
                continue
            for c in cards:
                q = urllib.parse.urlencode({"idList": lid, "key": key, "token": tok, "name": c["name"], "desc": c["desc"][:16000],
                                            "urlSource": c["url"] or "", "pos": "top"})
                try:
                    res = self._post("https://api.trello.com/1/cards?" + q, {})
                    self._w("insert or replace into brief_sent values(?,?,?,?)", (page, c["item_id"], day, res.get("id", "")))
                    pushed += 1
                except Exception as e:  # never leak the key in an error
                    errors.append("%s: Trello push failed (%s)" % (page, type(e).__name__))
        return {"pushed": pushed, "errors": errors}

    # ------------------------------------------------------------ 2. early-breakout watch
    def _notify(self, text, items):
        if getattr(CALLER, "user_id", None):
            return False
        url = os.environ.get("SCOUT_ALERT_WEBHOOK", "")
        if not url or not items:
            return False
        try:
            self._post(url, {"source": "kestrel", "text": text, "items": items})
            return True
        except Exception:
            return False

    def _stale_first(self, prefix, keys, n):
        state = dict(self._q("select key, checked from watch_state where key like ?", (prefix + "%",)))
        return sorted(keys, key=lambda k: state.get(prefix + k, 0))[:n]

    def watch(self):
        now, found, errors = time.time(), [], []
        ig_mult = float(os.environ.get("SCOUT_WATCH_IG_MULT", "10"))
        yt_mult = float(os.environ.get("SCOUT_WATCH_YT_MULT", "5"))
        users, chans = collections.defaultdict(list), collections.defaultdict(list)
        for n in self.niches()["niches"]:
            for u in n.get("ig_users") or []:
                users[u.lower()].append(n["name"])
            for c in n.get("channels") or []:
                chans[c].append(n["name"])
        for u in self._stale_first("ig:", list(users), int(os.environ.get("SCOUT_WATCH_IG_PER_RUN", "20"))):
            try:
                p = self.ig._fetch(u)
                self.cache.put("igp2:" + u, p, 6 * 3600)
            except ScoutError as e:
                errors.append("@%s: %s" % (u, e))
                if e.status in (429, 501):
                    break
                continue
            finally:
                self._w("insert or replace into watch_state values(?,?)", ("ig:" + u, now))
            reels = [m for m in p["media"] if m.get("media_product_type") == "REELS" and m.get("like_count") is not None]
            if len(reels) < 5:
                continue
            for m in reels:
                a = age_days((m.get("timestamp") or "").replace("+0000", "+00:00"))
                if a is None or a > 1.0:
                    continue
                base = max(med([x["like_count"] for x in reels if x["permalink"] != m["permalink"]]) or 1, (p.get("followers") or 0) * FLOOR, 1)
                mult = m["like_count"] / base
                if mult >= ig_mult:
                    found.append({"item_id": m["permalink"], "niche": users[u][0], "data": {
                        "platform": "instagram", "url": m["permalink"], "account": "@" + u, "likes": m["like_count"],
                        "hours_old": round(a * 24, 1), "multiple": round(mult, 1), "caption": first_line(m.get("caption"), 160),
                        "niches": users[u]}})
        if self.yt.use_api:
            for c in self._stale_first("yt:", list(chans), int(os.environ.get("SCOUT_WATCH_YT_PER_RUN", "15"))):
                try:
                    info = self.yt.channel_info(c)
                    ups = self.yt.recent(info, "any", 15)
                except ScoutError as e:
                    errors.append("%s: %s" % (c, e))
                    if e.status == 429:
                        break
                    continue
                finally:
                    self._w("insert or replace into watch_state values(?,?)", ("yt:" + c, now))
                base_rows = [v["views"] for v in ups if (v.get("age_days") or 99) >= 3]
                if len(base_rows) < 5:
                    continue
                base = max(med(base_rows) or 1, (info.get("subscribers") or 0) * FLOOR, 1)
                for v in ups:
                    if v.get("age_days") is None or v["age_days"] > 2:
                        continue
                    mult = v["views"] / base
                    if mult >= yt_mult:
                        found.append({"item_id": v["id"], "niche": chans[c][0], "data": {
                            "platform": "youtube_shorts" if v.get("is_short") else "youtube", "url": v.get("url"),
                            "channel": info.get("title"), "views": v["views"], "hours_old": round(v["age_days"] * 24, 1),
                            "multiple": round(mult, 1), "title": v.get("title"), "niches": chans[c]}})
        new = []
        for f in found:
            if self._q("select 1 from early_alerts where item_id=?", (f["item_id"],)):
                continue
            self._w("insert into early_alerts values(?,?,?,?)", (f["item_id"], now, f["niche"], json.dumps(f["data"])))
            new.append(f["data"])
        self._w("delete from early_alerts where first_seen<?", (now - 30 * 86400,))
        sent = self._notify("Kestrel: %d early breakout%s. Post your version today." % (len(new), "" if len(new) == 1 else "s"), new)
        return {"new_alerts": new, "notified": sent, "errors": errors[:20]}

    def early_alerts(self, hours=48, niche=None):
        rows = self._q("select first_seen, niche, data from early_alerts where first_seen>=? order by first_seen desc",
                       (time.time() - int(hours) * 3600,))
        out = [dict(json.loads(d), seen=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))) for t, n, d in rows
               if not niche or niche.strip().lower() in json.loads(d).get("niches", [n])]
        return {"hours": int(hours), "alerts": out}

    # ------------------------------------------------------------ 3. hooks
    def _spoken_hook(self, it):
        if it.get("source", "").startswith("youtube") and it.get("id"):
            try:
                tr = self.yt.transcript(it["id"])
                segs = [s["text"] for s in tr.get("segments", []) if s["start"] <= 4] if tr.get("segments") else []
                return " ".join(segs) or " ".join((tr.get("hook_first_30s") or "").split()[:15]) or None
            except ScoutError:
                return None
        return None  # Instagram audio needs a logged-in download; caption hook is used instead

    def extract_hooks(self, limit=25):
        r = self.ranked(limit=1000)
        done = 0
        for it in r["items"]:
            key = it.get("url") or it.get("id")
            if done >= int(limit) or self._q("select 1 from hooks where item_id=?", (key,)):
                continue
            text = it.get("title") or it.get("caption") or it.get("text") or ""
            hook, spoken = first_line(text), self._spoken_hook(it)
            self._w("insert or replace into hooks values(?,?,?,?,?,?,?,?,?,?)",
                    (key, it["niches"][0], it.get("source"), it.get("url"), hook, spoken, hook_type(spoken or hook),
                     it["adj_multiple"], time.time(), json.dumps({"account": it.get("account") or it.get("channel")})))
            done += 1
        return {"extracted": done}

    def hooks(self, niche=None, kind=None, hook_type=None, limit=50):
        sql, args = "select niche, kind, url, hook, spoken, type, multiple, data from hooks where 1=1", []
        if niche:
            sql += " and niche=?"
            args.append(niche.strip().lower())
        if kind:
            sql += " and kind=?"
            args.append(kind)
        if hook_type:
            sql += " and type=?"
            args.append(hook_type)
        rows = self._q(sql + " order by multiple desc", args)
        stats = collections.defaultdict(list)
        for r in rows:
            stats[r[5]].append(r[6])
        types = sorted(({"type": t, "count": len(v), "median_multiple": round(med(v), 1)} for t, v in stats.items()),
                       key=lambda x: -x["median_multiple"])
        out = [{"niche": n, "source": k, "url": u, "hook": h, "spoken": s, "type": t, "adj_multiple": m,
                "from": json.loads(d).get("account")} for n, k, u, h, s, t, m, d in rows[:int(limit)]]
        return {"types": types, "hooks": out, "total": len(rows)}

    # ------------------------------------------------------------ 4. cross-platform stories
    def stories(self, hours=24, min_platforms=2, limit=20):
        cutoff = time.time() - int(hours) * 3600
        posts = []
        for plat, data, created, seen in self._q("select platform, data, created, seen from social_posts where coalesce(created, seen)>=?",
                                                 (cutoff,)):
            d = json.loads(data)
            eng = sum((d.get(k) or 0) for k in ("likes", "reposts", "comments"))
            posts.append({"platform": plat, "url": d.get("url"), "who": d.get("author"), "text": d.get("text") or "", "eng": eng})
        for it in self.ranked(limit=1000)["items"]:
            if it.get("age_days") is not None and it["age_days"] * 86400 <= int(hours) * 3600 * 3:
                posts.append({"platform": it.get("source"), "url": it.get("url"), "who": it.get("account") or it.get("channel"),
                              "text": it.get("caption") or it.get("title") or "", "eng": it.get("likes") or it.get("views") or 0,
                              "niches": it["niches"]})
        posts.sort(key=lambda p: -p["eng"])
        clusters = []
        for p in posts[:3000]:
            tk, pr = tokens(p["text"])
            if len(tk) < 3:
                continue
            best = None
            for c in clusters:
                shared = tk & c["tokens"]
                sp = pr & c["proper"]
                if (len(shared) >= 3 and len(shared) / len(tk | c["tokens"]) >= 0.12) or len(sp) >= 2:
                    best = c
                    break
            if best is None:
                clusters.append({"tokens": set(tk), "proper": set(pr), "posts": [p], "words": collections.Counter(tk)})
            else:
                best["posts"].append(p)
                best["words"].update(tk)
                best["proper"] |= pr
        out = []
        for c in clusters:
            plats = sorted({p["platform"] for p in c["posts"]})
            if len(plats) < int(min_platforms):
                continue
            top = c["posts"][0]
            pages = sorted({n for p in c["posts"] for n in p.get("niches", [])})
            per = {}
            for p in c["posts"]:
                per.setdefault(p["platform"], [])
                if len(per[p["platform"]]) < 2:
                    per[p["platform"]].append({"url": p["url"], "who": p["who"], "engagement": p["eng"]})
            out.append({"headline": first_line(top["text"], 160), "platforms": plats, "posts": len(c["posts"]),
                        "engagement": sum(p["eng"] for p in c["posts"]), "keywords": [w for w, _ in c["words"].most_common(6)],
                        "pages": pages, "examples": per})
        out.sort(key=lambda s: (-len(s["platforms"]), -s["engagement"]))
        return {"hours": int(hours), "stories": out[:int(limit)], "posts_scanned": len(posts)}

    # ------------------------------------------------------------ 5. own-reel post-mortem
    @staticmethod
    def _slot(iso):
        try:
            t = dt.datetime.fromisoformat((iso or "").replace("+0000", "+00:00")).astimezone(IST)
        except ValueError:
            return None, None
        h = t.hour
        slot = "morning 6-12" if 6 <= h < 12 else "afternoon 12-17" if 12 <= h < 17 else "evening 17-22" if 17 <= h < 22 else "night 22-6"
        return slot, t.strftime("%a")

    def postmortem(self, account, days=14):
        ins = self.mine.ig_insights(account, days=28, reels=25)
        if "accounts" in ins:
            return {"accounts": [self.postmortem(a["label"], days) for a in ins["accounts"] if a.get("label")]}
        rows = [dict(r) for r in self._all_reels(ins) if r.get("views")]
        settled = [r for r in rows if (r.get("age_days") or 0) >= 2]
        if len(settled) < 5:
            raise ScoutError("need at least 5 reels older than 2 days to judge against", 404)
        base = med([r["views"] for r in settled]) or 1
        share_m = med([100.0 * r["shares"] / r["views"] for r in settled if r.get("shares") is not None]) or 0
        save_m = med([100.0 * r["saved"] / r["views"] for r in settled if r.get("saved") is not None]) or 0
        watch_m = med([r.get("avg_watch_seconds") for r in settled if r.get("avg_watch_seconds")]) or 0
        for r in settled:
            r["ratio"] = r["views"] / base
            r["slot"], r["weekday"] = self._slot(r.get("posted"))
            r["hook_type"] = hook_type(r.get("caption"))
        group = lambda key: {k: round(med([x["ratio"] for x in v]), 2) for k, v in self._group(settled, key).items() if len(v) >= 2}
        slots, types = group("slot"), group("hook_type")
        out = []
        for r in settled:
            if r["age_days"] > int(days):
                continue
            why = []
            sr = 100.0 * (r.get("shares") or 0) / r["views"]
            sv = 100.0 * (r.get("saved") or 0) / r["views"]
            if share_m and sr >= 1.5 * share_m:
                why.append("shared a lot (%.2f%% vs your usual %.2f%%), the algorithm pushes shared reels" % (sr, share_m))
            elif share_m and sr <= 0.6 * share_m:
                why.append("few shares (%.2f%% vs %.2f%%)" % (sr, share_m))
            if save_m and sv >= 1.5 * save_m:
                why.append("saved a lot (%.2f%% vs %.2f%%)" % (sv, save_m))
            w = r.get("avg_watch_seconds")
            if w and watch_m and w >= 1.25 * watch_m:
                why.append("people watched longer (%ss vs %ss)" % (w, watch_m))
            elif w and watch_m and w <= 0.75 * watch_m:
                why.append("people dropped early (%ss vs %ss), check the hook" % (w, watch_m))
            if slots.get(r["slot"]) and slots[r["slot"]] >= 1.3:
                why.append("posted in your strong slot (%s)" % r["slot"])
            elif slots.get(r["slot"]) and slots[r["slot"]] <= 0.7:
                why.append("posted in a weak slot (%s)" % r["slot"])
            if types.get(r["hook_type"]) and types[r["hook_type"]] >= 1.3:
                why.append("%s hook, which works for you" % r["hook_type"])
            ratio = r["ratio"]
            verdict = "hit" if ratio >= 2 else "above" if ratio >= 1.2 else "flop" if ratio < 0.6 else "normal"
            out.append({"url": r.get("url"), "caption": r.get("caption"), "posted": r.get("posted"), "views": r["views"],
                        "vs_median": round(ratio, 2), "verdict": verdict, "why": why or ["nothing stands out, it did about as usual"]})
        out.sort(key=lambda x: -x["vs_median"])
        return {"account": ins.get("account"), "median_views": int(base), "reels": out, "by_slot": slots, "by_hook_type": types,
                "usual": {"share_rate_pct": round(share_m, 2), "save_rate_pct": round(save_m, 2), "avg_watch_seconds": watch_m},
                "notes": ["Reasons are what stands out in the numbers, not proof of cause. Use them to pick what to test next."]}

    @staticmethod
    def _all_reels(ins):
        return ins.get("reels") or ins.get("top_reels") or []

    @staticmethod
    def _group(rows, key):
        g = collections.defaultdict(list)
        for r in rows:
            if r.get(key):
                g[r[key]].append(r)
        return g

    def scorecard_weekly(self, account=None):
        res = []
        labels = [account] if account else [t["label"] for t in self.ig.pool()]
        for lab in labels:
            try:
                pm = self.postmortem(lab, days=7)
            except ScoutError as e:
                res.append({"account": lab, "error": str(e)})
                continue
            r = pm["reels"]
            res.append({"account": pm["account"], "reels_this_week": len(r), "hits": sum(x["verdict"] == "hit" for x in r),
                        "flops": sum(x["verdict"] == "flop" for x in r), "best": r[0] if r else None, "worst": r[-1] if r else None,
                        "best_slot": max(pm["by_slot"], key=pm["by_slot"].get) if pm["by_slot"] else None,
                        "best_hook_type": max(pm["by_hook_type"], key=pm["by_hook_type"].get) if pm["by_hook_type"] else None})
        ok = [x for x in res if not x.get("error")]
        text = "Kestrel weekly: " + "; ".join("%s %d reels, %d hits, %d flops" % (x["account"], x["reels_this_week"], x["hits"], x["flops"])
                                               for x in ok)
        return {"accounts": res, "notified": self._notify(text, ok)}

    # ------------------------------------------------------------ comment mining
    def comments(self, ref=None, niche=None, n=100, videos=5):
        if not self.yt.use_api:
            raise ScoutError("comment mining needs YOUTUBE_API_KEY", 501)
        if ref:
            ids = [video_id(ref)]
        else:
            ids = [it["id"] for it in self.ranked(niche=niche, limit=200)["items"]
                   if (it.get("source") or "").startswith("youtube") and it.get("id")][:int(videos)]
        if not ids:
            raise ScoutError("no YouTube videos to read. Give a ref or a niche with YouTube outliers", 404)
        allq, allr, words, per = [], [], collections.Counter(), []

        def read(v):
            data = self.yt.api("commentThreads", 1, part="snippet", videoId=v, maxResults=min(int(n), 100), order="relevance",
                               textFormat="plainText")
            return [{"text": " ".join((c["snippet"]["topLevelComment"]["snippet"].get("textDisplay") or "").split())[:300],
                     "likes": c["snippet"]["topLevelComment"]["snippet"].get("likeCount", 0)} for c in data.get("items", [])]

        for v in ids:
            try:
                cs = self.cache.cached("cm:%s:%s" % (v, n), 12 * 3600, lambda: read(v))
            except ScoutError as e:
                per.append({"video": v, "error": str(e)})
                if e.status == 429:
                    break
                continue
            qs = [c for c in cs if "?" in c["text"] or re.match(r"(how|what|why|where|when|can|does|is|are|do|which|who)\b", c["text"].lower())]
            rq = [c for c in cs if re.search(r"(part 2|next video|please (make|do|try|review)|can you (make|do|try|review)|do one on|you should)",
                                            c["text"].lower())]
            for c in cs:
                tk, _ = tokens(c["text"])
                words.update(tk)
            allq += [dict(c, video=v) for c in qs]
            allr += [dict(c, video=v) for c in rq]
            per.append({"video": v, "comments": len(cs), "questions": len(qs), "requests": len(rq)})
        allq.sort(key=lambda c: -c["likes"])
        allr.sort(key=lambda c: -c["likes"])
        return {"videos": per, "top_questions": allq[:15], "top_requests": allr[:10],
                "common_words": [w for w, _ in words.most_common(20)],
                "notes": ["Each top question is a ready video idea. Requests tell you what viewers want next."]}

    # ------------------------------------------------------------ best posting time
    def besttime(self, account):
        t = self.mine._ig_pick(account)[0]
        online, errors = collections.Counter(), []
        try:
            r = self.mine._ig_get(t, t["ig_user_id"] + "/insights", metric="online_followers", period="lifetime")
            vals = ((r.get("data") or [{}])[0].get("values") or [])
            for day in vals:
                end = day.get("end_time")
                for h, c in (day.get("value") or {}).items():
                    try:
                        base = dt.datetime.fromisoformat(end.replace("+0000", "+00:00")) if end else dt.datetime.now(dt.timezone.utc)
                        local = base.astimezone(PACIFIC).replace(hour=int(h), minute=0).astimezone(IST)
                        online[local.hour] += c or 0
                    except (ValueError, AttributeError):
                        continue
        except ScoutError as e:
            errors.append("online_followers: %s" % e)
        perf = {}
        try:
            ins = self.mine.ig_insights(t["label"], days=28, reels=25)
            rows = [r for r in self._all_reels(ins) if r.get("views")]
            base = med([r["views"] for r in rows]) or 1
            by = collections.defaultdict(list)
            for r in rows:
                try:
                    hr = dt.datetime.fromisoformat(r["posted"].replace("+0000", "+00:00")).astimezone(IST).hour
                    by[hr].append(r["views"] / base)
                except (ValueError, KeyError, AttributeError):
                    continue
            perf = {h: round(med(v), 2) for h, v in by.items() if len(v) >= 2}
        except ScoutError as e:
            errors.append("reels: %s" % e)
        top_online = max(online.values()) if online else 0
        hours = []
        for h in range(24):
            o = online.get(h, 0) / top_online if top_online else None
            p = perf.get(h)
            score = (0.6 * o if o is not None else 0) + (0.4 * min(p, 3) / 3 if p is not None else 0)
            hours.append({"hour_ist": "%02d:00" % h, "online_share": round(o, 2) if o is not None else None, "your_reels_vs_median": p,
                          "score": round(score, 3)})
        best = sorted((x for x in hours if x["score"] > 0), key=lambda x: -x["score"])[:3]
        return {"account": account, "best_hours_ist": [b["hour_ist"] for b in best], "hours": hours, "errors": errors,
                "notes": ["Online followers come from Meta in Pacific time and are converted to your time zone (SCOUT_TZ). Your own reel results weigh 40%."]}

    # ------------------------------------------------------------ autopilot crawl (replaces the cookie crawler)
    @staticmethod
    def _load_seeds(path=None):
        path = os.path.expanduser(path or os.environ.get("SCOUT_AUTOPILOT_SEEDS", "~/.scout/ig_seeds.json"))
        try:
            with open(path, encoding="utf8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            raise ScoutError("cannot read seed file %s (%s)" % (path, type(e).__name__), 404)
        if isinstance(raw, list):
            raw = {"all": raw}
        out = {}
        for g, v in raw.items():
            users = v.get("seeds") or v.get("users") or v.get("accounts") or [] if isinstance(v, dict) else v
            users = [(u.get("username") or u.get("user") or "") if isinstance(u, dict) else str(u) for u in users]
            out[str(g).lower()] = [u.strip().lstrip("@").lower() for u in users if u and u.strip()]
        return out

    def ig_seed_outliers(self, group=None, min_multiple=2.0, max_age_days=14, limit=40, max_users=40):
        """Instagram outliers for your seed groups, through the token pool (no cookies).
        Ranked on likes against each account's median with the follower floor. No views, music or video files from this API."""
        seeds = self._load_seeds()
        groups = {group.lower(): seeds.get(group.lower(), [])} if group else seeds
        res, errors, stale, seen = [], [], 0, {}
        for g, users in groups.items():
            if not users:
                errors.append({"group": g, "error": "no seeds in this group"})
                continue
            r = self.ig.outliers(users=users, min_multiple=1.0, max_age_days=int(max_age_days), limit=1000, max_users=int(max_users))
            for row in r["results"]:
                row["adj_multiple"] = adjusted(row)
                if row["adj_multiple"] < float(min_multiple):
                    continue
                if row["url"] in seen:   # the same reel from an account listed in several groups
                    if g not in seen[row["url"]]["groups"]:
                        seen[row["url"]]["groups"].append(g)
                    continue
                seen[row["url"]] = dict(row, group=g, groups=[g])
                res.append(seen[row["url"]])
            errors += [dict(e, group=g) for e in r.get("errors", [])]
        for u in {x["account"] for x in res}:
            p = self.cache.get("igp2:" + u.lstrip("@"))
            stale += bool(p and p.get("stale"))
        res.sort(key=rank_key)
        return {"groups": sorted(groups), "results": res[:int(limit)], "errors": errors[:30], "stale_accounts": stale,
                "notes": ["Ranked on likes, not views. The API gives no video file or music for other accounts' reels."]}

    # ------------------------------------------------------------ dashboard
    def dashboard_html(self, niche=None):
        e = html.escape
        r = self.ranked(niche=niche, limit=40)
        al = self.early_alerts(48, niche)["alerts"]
        st = self.stories(24)["stories"][:10]
        hk = self.hooks(niche=niche, limit=15)
        names = [n["name"] for n in self.niches()["niches"]]
        nav = " ".join('<a class="chip%s" href="/dashboard%s">%s</a>' % (" on" if (niche or "") == n else "", "?niche=" + e(n) if n else "",
                                                                       e(n or "all")) for n in [""] + names)

        def row(it):
            what = it.get("title") or it.get("caption") or ""
            who = it.get("account") or it.get("channel") or ""
            num = ("%s views" % format(it["views"], ",")) if it.get("views") else ("%s likes" % format(it.get("likes") or 0, ","))
            return ('<tr><td class="m">%sx</td><td><a href="%s" target="_blank" rel="noopener">%s</a><div class="s">%s · %s · %s · %s</div></td></tr>'
                    % (it["adj_multiple"], e(it.get("url") or ""), e(first_line(what, 110)), e(who), num, e(it.get("source") or ""),
                       e(", ".join(it["niches"]))))

        alert_rows = "".join('<li><a href="%s" target="_blank" rel="noopener">%s</a> <span class="s">%s · %sx in %sh</span></li>' % (
            e(a.get("url") or ""), e(a.get("title") or a.get("caption") or ""), e(a.get("account") or a.get("channel") or ""),
            a.get("multiple"), a.get("hours_old")) for a in al) or '<li class="s">Nothing breaking right now.</li>'
        story_rows = "".join('<li>%s <span class="s">%s · %d posts</span></li>' % (e(s["headline"]), e(", ".join(s["platforms"])), s["posts"])
                             for s in st) or '<li class="s">No cross-platform stories in the last 24 hours.</li>'
        hook_rows = "".join('<li>"%s" <span class="s">%s · %sx</span></li>' % (e(h["spoken"] or h["hook"]), e(h["type"]), h["adj_multiple"])
                            for h in hk["hooks"]) or '<li class="s">No hooks extracted yet.</li>'
        page = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kestrel</title><style>
:root{--bg:#f7f6f3;--fg:#17161a;--mute:#6b6874;--card:#fff;--line:#e6e3dd;--acc:#e8590c}
@media (prefers-color-scheme:dark){:root{--bg:#141317;--fg:#f2f0ec;--mute:#9a96a3;--card:#1d1c21;--line:#2c2a31;--acc:#ff7a33}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px max(16px,env(safe-area-inset-left))}
h1{font-size:26px;margin:0 0 4px}h1 b{color:var(--acc)}h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);margin:0 0 10px}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:16px}@media (max-width:820px){.grid{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
table{width:100%;border-collapse:collapse}td{padding:8px 6px;border-top:1px solid var(--line);vertical-align:top}
td.m{font-weight:700;color:var(--acc);white-space:nowrap;width:60px}a{color:inherit}.s{color:var(--mute);font-size:13px}
ul{margin:0;padding-left:18px}li{margin:0 0 8px}.chip{display:inline-block;padding:4px 10px;border:1px solid var(--line);border-radius:99px;
margin:0 6px 6px 0;text-decoration:none;font-size:13px}.chip.on{background:var(--acc);border-color:var(--acc);color:#fff}
.side{display:flex;flex-direction:column;gap:16px}</style></head><body><main>
<h1>kestrel<b>.</b></h1><p class="s">Feed for @@DAY@@ · adjusted multiples · @@DUP@@ duplicates merged</p><nav>@@NAV@@</nav>
<div class="grid"><section><h2>Top outliers</h2><table>@@ROWS@@</table></section>
<div class="side"><section><h2>Breaking now (48h)</h2><ul>@@ALERTS@@</ul></section>
<section><h2>Stories across platforms</h2><ul>@@STORIES@@</ul></section>
<section><h2>Hooks that worked</h2><ul>@@HOOKS@@</ul></section></div></div></main></body></html>"""
        vals = {"DAY": e(r["day"] or "no data yet"), "DUP": str(r["duplicates_merged"]), "NAV": nav,
                "ROWS": "".join(row(i) for i in r["items"]) or '<tr><td class="s">No items yet.</td></tr>',
                "ALERTS": alert_rows, "STORIES": story_rows, "HOOKS": hook_rows}
        for k, v in vals.items():
            page = page.replace("@@%s@@" % k, v)
        return page

    # ------------------------------------------------------------ daily job additions
    def _run_daily(self, only):
        out = super()._run_daily(only)
        if not only:
            try:
                out["hooks"] = self.extract_hooks(int(os.environ.get("SCOUT_HOOKS_PER_DAY", "25")))
            except ScoutError as e:
                out["errors"].append({"hooks": str(e)})
            try:
                out["reach"] = self.reach_all()
            except ScoutError as e:
                out["errors"].append({"reach": str(e)})
            try:
                out["title_thumb_changes"] = self.scan_changes()
            except ScoutError as e:
                out["errors"].append({"changes": str(e)})
            if os.environ.get("SCOUT_TRELLO_LISTS"):
                b = self.brief(push=True)
                out["brief"] = {"pushed": b["pushed"], "errors": b["errors"]}
        return out
