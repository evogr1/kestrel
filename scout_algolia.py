"""Algolia search for Scout .

What it does: copies the posts the scroll bots collect (X, LinkedIn, Reddit, in social_posts) into an Algolia index, so
Scout can search them by meaning-ish keyword match with typo tolerance, filtered by platform, page/subreddit and age.

Free plan only (10k searches and 50k records a month, no card). Our own limits keep it inside that:
- at most 45,000 records: the newest posts are kept, older ones are deleted from the index (Scout's own copy stays)
- at most 300 searches a day (per day), counted in ~/.scout-algolia.json; after that Scout searches its own database
- no keys, an Algolia error or ~/.scout-algolia-off: Scout searches its own database. Search never fails because of Algolia.
Keys go in your .env: ALGOLIA_APP_ID, ALGOLIA_WRITE_KEY (a key with addObject/deleteObject/editSettings on the
index only), ALGOLIA_SEARCH_KEY. They are never printed or logged. Tune in the same .env: ALGOLIA_INDEX, ALGOLIA_MAX_RECORDS,
ALGOLIA_DAY_SEARCHES.

    python3 scout_algolia.py sync           push new posts (cron, every 30 min)
    python3 scout_algolia.py search "query" [platform]
    python3 scout_algolia.py status
"""
import datetime
import hashlib
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request

H = os.path.expanduser("~")
STATE = H + "/.scout-algolia.json"
OFF = H + "/.scout-algolia-off"
METRICS = ("likes", "views", "reposts", "comments", "saves", "replies")


def _env():
    e = dict(os.environ)
    try:
        for line in open(H + "/scout/.env"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass
    return e


def _cfg():
    e = _env()
    return {"app": e.get("ALGOLIA_APP_ID", ""), "write": e.get("ALGOLIA_WRITE_KEY", ""), "search": e.get("ALGOLIA_SEARCH_KEY", ""),
            "index": e.get("ALGOLIA_INDEX", "scout_posts"), "max_records": int(e.get("ALGOLIA_MAX_RECORDS", 45000)),
            "day_searches": int(e.get("ALGOLIA_DAY_SEARCHES", 300))}


def _state():
    try: return json.load(open(STATE))
    except Exception: return {}


def _save(s):
    tmp = STATE + ".tmp"; json.dump(s, open(tmp, "w")); os.replace(tmp, STATE)


def _today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))).date().isoformat()


def enabled(kind="search"):
    c = _cfg()
    if os.path.exists(OFF): return False, "~/.scout-algolia-off exists"
    if not c["app"] or not c["search" if kind == "search" else "write"]: return False, "no Algolia keys in ~/scout/.env"
    if kind == "search":
        used = int((_state().get("searches") or {}).get(_today(), 0))
        if used >= c["day_searches"]: return False, f"daily search cap used ({used} of {c['day_searches']})"
    return True, ""


def _call(method, path, body=None, key="write", timeout=30):
    c = _cfg()
    host = f"{c['app']}-dsn.algolia.net" if key == "search" else f"{c['app']}.algolia.net"
    req = urllib.request.Request(f"https://{host}/1/indexes/{urllib.parse.quote(c['index'])}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Algolia-Application-Id": c["app"], "X-Algolia-API-Key": c[key], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:   # never echo the request (it carries the key)
        raise RuntimeError(f"Algolia HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:160]}")


def _db():
    for p in (os.environ.get("SCOUT_DB", ""), H + "/.scout/cache.sqlite"):
        if p and os.path.exists(p): return sqlite3.connect(p, timeout=30)
    import glob
    for p in glob.glob(H + "/scout/**/*.db", recursive=True) + glob.glob(H + "/.scout*/*.db"):
        con = sqlite3.connect(p, timeout=30)
        if con.execute("select name from sqlite_master where name='social_posts'").fetchone(): return con
    raise RuntimeError("Scout database with social_posts not found")


def _record(platform, url, author, community, created, seen, data):
    d = json.loads(data)
    r = {"objectID": hashlib.sha1((platform + "|" + url).encode()).hexdigest(), "platform": platform, "url": url,
         "author": author or "", "community": community or "", "text": (d.get("text") or "")[:500],
         "created": int(created or seen), "seen": int(seen), "followers": d.get("author_followers")}
    for m in METRICS:
        if d.get(m) is not None: r[m] = d[m]
    return r


def sync():
    ok, why = enabled("write")
    if not ok: return {"synced": 0, "skipped": why}
    c, s = _cfg(), _state()
    if not s.get("settings_done"):
        _call("PUT", "/settings", {"searchableAttributes": ["text", "author", "community"],
                                   "attributesForFaceting": ["filterOnly(platform)", "filterOnly(community)", "filterOnly(author)"],
                                   "customRanking": ["desc(views)", "desc(likes)"], "numericAttributesForFiltering": ["created", "seen"]})
        s["settings_done"] = True; _save(s)
    con = _db()
    row = con.execute("select seen from social_posts order by seen desc limit 1 offset ?", (c["max_records"] - 1,)).fetchone()
    cutoff = int(row[0]) if row else 0   # keep only the newest max_records posts in Algolia
    last = float(s.get("last_seen", 0))
    rows = con.execute("select platform, url, author, community, created, seen, data from social_posts where seen > ? and seen >= ? order by seen",
                       (last, cutoff)).fetchall()
    n = 0
    for i in range(0, len(rows), 1000):
        chunk = rows[i:i + 1000]
        _call("POST", "/batch", {"requests": [{"action": "updateObject", "body": _record(*r)} for r in chunk]}, timeout=120)
        n += len(chunk); s["last_seen"] = chunk[-1][5]; _save(s)
    deleted = False
    if cutoff and cutoff > int(s.get("cutoff", 0)):
        _call("POST", "/deleteByQuery", {"params": urllib.parse.urlencode({"numericFilters": f"seen<{cutoff}"})})
        s["cutoff"] = cutoff; deleted = True
    week_ago = int(time.time() - 120 * 86400)   # Scout drops posts not seen for 120 days; drop them here too
    if int(s.get("aged_out", 0)) < time.time() - 86400:
        _call("POST", "/deleteByQuery", {"params": urllib.parse.urlencode({"numericFilters": f"seen<{week_ago}"})}); s["aged_out"] = int(time.time())
    s["last_sync"] = int(time.time()); _save(s)
    return {"synced": n, "trimmed_older_than": cutoff if deleted else None}


def _local(query, platform, communities, days, limit):
    con = _db(); words = [w for w in str(query).lower().split() if w]
    sql, args = "select platform, url, author, community, created, seen, data from social_posts where coalesce(created, seen) >= ?", [time.time() - days * 86400]
    if platform: sql += " and platform = ?"; args.append(platform)
    if communities: sql += " and community in (%s)" % ",".join("?" * len(communities)); args += [x.lower() for x in communities]
    for w in words[:6]: sql += " and lower(data) like ?"; args.append(f"%{w}%")
    out = [_record(*r) for r in con.execute(sql + " order by seen desc limit 500", args).fetchall()]
    out.sort(key=lambda r: -(r.get("views") or r.get("likes") or 0))
    return out[:limit]


def search(query, platform=None, communities=None, days=30, limit=30):
    """Search the bot-collected posts. Uses Algolia when it can, Scout's own database otherwise. Never raises for Algolia."""
    platform = {"twitter": "x"}.get((platform or "").lower(), (platform or "").lower()) or None
    communities = [x.strip().lower().replace("r/", "") for x in (communities or []) if x and x.strip()]
    days, limit = int(days or 30), max(1, min(int(limit or 30), 100))
    ok, why = enabled("search")
    if ok:
        f = [f"created>={int(time.time() - days * 86400)}"]
        flt = []
        if platform: flt.append(f"platform:{json.dumps(platform)}")
        if communities: flt.append("(" + " OR ".join(f"community:{json.dumps(x)}" for x in communities) + ")")
        p = {"query": str(query)[:400], "hitsPerPage": limit, "numericFilters": ",".join(f)}
        if flt: p["filters"] = " AND ".join(flt)
        try:
            d = _call("POST", "/query", {"params": urllib.parse.urlencode(p)}, key="search", timeout=15)
            s = _state(); s.setdefault("searches", {}); s["searches"] = {k: v for k, v in s["searches"].items() if k >= _today()[:8]}
            s["searches"][_today()] = int(s["searches"].get(_today(), 0)) + 1; _save(s)
            hits = [{k: v for k, v in h.items() if not k.startswith("_") and k != "objectID"} for h in d.get("hits", [])]
            return {"results": hits, "source": "algolia", "total": d.get("nbHits"), "searches_today": s["searches"][_today()]}
        except Exception as e:
            why = str(e)[:160]
    return {"results": _local(query, platform, communities, days, limit), "source": "scout database", "note": why}


def status():
    c, s = _cfg(), _state()
    return {"keys": bool(c["app"] and c["write"] and c["search"]), "index": c["index"], "off": os.path.exists(OFF),
            "last_sync": s.get("last_sync"), "searches_today": (s.get("searches") or {}).get(_today(), 0),
            "day_search_cap": c["day_searches"], "max_records": c["max_records"]}


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a[:1] == ["sync"]: print(json.dumps(sync()))
    elif a[:1] == ["search"]: print(json.dumps(search(a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else None, limit=10), ensure_ascii=False)[:3000])
    else: print(json.dumps(status()))
