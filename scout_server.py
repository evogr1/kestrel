#!/usr/bin/env python3
"""Scout REST API. One key, JSON in and out.

  SCOUT_API_KEY=... python3 scout_server.py            (binds 127.0.0.1:8799)

Auth: header  X-Scout-Key: <key>   or   Authorization: Bearer <key>
Reads are GET with query parameters. Writes are POST with a JSON body (query parameters also work).
"""
import hmac
import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scout_core import ScoutError, Config
from scout_extras import Kestrel
import scout_legal
import scout_connect
import scout_core
import scout_remote_mcp


def _list(s):
    if isinstance(s, list):
        return [str(x).strip() for x in s if str(x).strip()]
    return [x.strip() for x in str(s).split(",") if x.strip()]


def _bool(s):
    return s if isinstance(s, bool) else str(s).lower() in ("1", "true", "yes", "on")


def _raw(v):
    return v


def _conv(fn, v):
    if fn is str:
        return str(v)
    return fn(v)


K = {"kind": str, "limit": int, "query": str}
# route -> (method on ScoutPlus, http method, {param: converter})
ROUTES = {
    "/v1/outliers": ("outliers", "GET", {**K, "channels": _list, "max_age_days": int, "min_multiple": float, "min_subs": int,
                                         "max_subs": int, "per_channel": int, "max_channels": int, "search_n": int, "on_topic": _bool}),
    "/v1/channel": ("channel_stats", "GET", {"ref": str, "kind": str, "n": int}),
    "/v1/keywords": ("keyword_demand", "GET", {"seed": str, "terms": int, "kind": str}),
    "/v1/video": ("video", "GET", {"ref": str, "with_transcript": _bool, "lang": str}),
    "/v1/transcript": ("transcript", "GET", {"ref": str, "lang": str}),
    "/v1/ig/outliers": ("ig_outliers", "GET", {"users": _list, "niche": str, "min_multiple": float, "max_age_days": int, "limit": int, "max_users": int}),
    "/v1/ig/profile": ("ig_profile", "GET", {"user": str, "full": _bool}),
    "/v1/usage": ("usage", "GET", {}),
    # v2
    "/v1/feed": ("feed", "GET", {"day": str, "niche": str, "only_new": _bool, "kind": str, "limit": int}),
    "/v1/niches": ("niches", "GET", {}),
    "/v1/niches/add": ("niche_add", "POST", {"name": str, "query": str, "kind": str, "max_age_days": int, "min_multiple": float,
                                             "breakout_max_subs": int, "ig_niche": str, "x_users": _list,
                                             "channels": _list, "ig_users": _list,
                                             "linkedin_users": _list, "subreddits": _list}),
    "/v1/niches/remove": ("niche_remove", "POST", {"name": str}),
    "/v1/breakouts": ("breakouts", "GET", {**K, "max_subs": int, "min_median_views": int, "max_age_days": int}),
    "/v1/competitors": ("competitors", "GET", {"days": int}),
    "/v1/track/add": ("track_add", "POST", {"ref": str}),
    "/v1/track/remove": ("track_remove", "POST", {"ref": str}),
    "/v1/wins": ("wins", "GET", {**K, "max_age_days": int, "win_multiple": float, "on_topic": _bool, "with_thumbs": _bool}),
    "/v1/thumbnails/similar": ("similar_thumbnails", "GET", {"ref": str, "query": str, "kind": str, "limit": int}),
    "/v1/optimize": ("optimize", "GET", {"ref": str, "query": str}),
    "/v1/timing": ("timing", "GET", {"channels": _list, "query": str, "kind": str, "tz": str, "n": int}),
    "/v1/calendar": ("calendar", "GET", {"channels": _list, "query": str, "niche": str, "days": int, "per_week": int, "kind": str, "tz": str}),
    "/v1/ideas": ("ideas", "GET", {"niche": str, "query": str, "kind": str, "n": int, "with_hooks": _bool}),
    "/v1/jobs/daily": ("run_daily", "POST", {"only": _list}),
    # your own channels and pages
    "/v1/me/channels": ("me_channels", "GET", {}),
    "/v1/me/race": ("me_race", "GET", {"channel": str}),
    "/v1/me/scorecard": ("me_scorecard", "GET", {"channel": str}),
    "/v1/me/achievements": ("me_achievements", "GET", {"channel": str}),
    "/v1/me/audience": ("me_audience", "GET", {"channel": str, "days": int}),
    "/v1/me/ig": ("me_ig", "GET", {"account": str, "days": int, "reels": int}),
    # X and Facebook
    "/v1/x/post": ("x_post", "GET", {"ref": str}),
    "/v1/x/posts": ("x_posts", "GET", {"refs": _list}),
    "/v1/x/profile": ("x_profile", "GET", {"user": str}),
    "/v1/x/outliers": ("x_outliers", "GET", {"users": _list, "min_multiple": float, "max_age_days": int, "limit": int}),
    "/v1/x/search": ("x_search", "GET", {"query": str, "limit": int}),
    "/v1/me/fb/pages": ("me_fb_pages", "GET", {}),
    "/v1/me/fb": ("me_fb", "GET", {"page": str, "days": int}),
    # posts pushed in by your own scroll bots
    "/v1/ingest": ("social_ingest", "POST", {"platform": str, "posts": _raw}),
    "/v1/social/outliers": ("social_outliers", "GET", {"platform": str, "authors": _list, "communities": _list, "by": str, "min_multiple": float,
                                                       "max_age_days": int, "limit": int}),
    "/v1/social/search": ("social_search", "GET", {"query": str, "platform": str, "communities": _list, "days": int, "limit": int}),   # kept 14 Nov: scout_social_search uses it
    # action layer
    "/v1/ranked": ("ranked", "GET", {"day": str, "niche": str, "kind": str, "limit": int}),
    "/v1/brief": ("brief", "GET", {"day": str, "per_page": int, "min_multiple": float}),
    "/v1/brief/push": ("brief", "POST", {"day": str, "per_page": int, "min_multiple": float, "push": _bool}),
    "/v1/watch": ("watch", "POST", {}),
    "/v1/alerts/early": ("early_alerts", "GET", {"hours": int, "niche": str}),
    "/v1/hooks": ("hooks", "GET", {"niche": str, "kind": str, "hook_type": str, "limit": int}),
    "/v1/hooks/extract": ("extract_hooks", "POST", {"limit": int}),
    "/v1/stories": ("stories", "GET", {"hours": int, "min_platforms": int, "limit": int}),
    "/v1/me/postmortem": ("postmortem", "GET", {"account": str, "days": int}),
    "/v1/me/weekly": ("scorecard_weekly", "POST", {"account": str}),
    "/v1/comments": ("comments", "GET", {"ref": str, "niche": str, "n": int, "videos": int}),
    "/v1/me/besttime": ("besttime", "GET", {"account": str}),
    # growth layer
    "/v1/clips": ("clips", "GET", {"ref": str, "n": int, "clip_seconds": int, "min_gap": int}),
    "/v1/ig/breakdown": ("creator_breakdown", "GET", {"user": str, "me": str, "top": int, "transcribe": int, "platform": str}),
    "/v1/setup": ("setup_status", "GET", {}),
    "/v1/watchlist": ("watchlist", "GET", {}),
    "/v1/watchlist/add": ("watchlist_add", "POST", {"handle": str, "platform": str}),
    "/v1/watchlist/remove": ("watchlist_remove", "POST", {"handle": str, "platform": str}),
    "/v1/watchlist/new": ("whats_new", "GET", {"min_multiple": float, "platform": str, "limit": int}),
    "/v1/me/reel_check": ("my_reel_check", "GET", {"reel": str, "me": str}),
    "/v1/me/profile": ("my_profile", "GET", {}),
    "/v1/me/profile/set": ("my_profile_set", "POST", {"instagram": str, "youtube": str, "x": str, "niche": str, "audience": str,
                                                      "goal": str, "language": str}),
    "/v1/breakdown": ("creator_breakdown", "GET", {"user": str, "me": str, "top": int, "transcribe": int, "platform": str}),
    "/v1/me/reach": ("me_reach", "GET", {"channel": str, "video": str, "days": int}),
    "/v1/me/reach/sync": ("reach_sync", "POST", {"channel": str}),
    "/v1/me/live": ("me_live", "GET", {"channel": str, "n": int}),
    "/v1/me/retention": ("me_retention", "GET", {"channel": str, "video": str, "n": int}),
    "/v1/gaps": ("gaps", "GET", {"seed": str, "niche": str, "terms": int, "kind": str, "fresh_days": int}),
    "/v1/changes": ("changes", "GET", {"days": int}),
    "/v1/changes/scan": ("scan_changes", "POST", {"max_channels": int, "thumbs": _bool}),
    "/v1/rising": ("rising", "GET", {"days": int, "min_count": int, "limit": int}),
    "/v1/sponsors": ("sponsors", "GET", {"niche": str, "days": int, "limit": int}),
    "/v1/ig/seed_outliers": ("ig_seed_outliers", "GET", {"group": str, "min_multiple": float, "max_age_days": int, "limit": int, "max_users": int}),
    "/v1/social/top": ("social_top", "GET", {"platform": str, "days": int, "metric": str, "limit": int, "authors": _list, "communities": _list}),
}
MAX_BODY = 4 * 1024 * 1024
SSE_TICKS, SSE_TICK_SECONDS = 24, 25


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clen(v):
    try:
        return max(0, int(v or 0))
    except ValueError:
        return 0


def make_server(scout, key, host="127.0.0.1", port=8799, mcp_secret=""):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            line = fmt % a
            if mcp_secret:                      # the connector URL is a key: never write it to logs
                line = line.replace(mcp_secret, "<secret>")
            sys.stderr.write("%s %s\n" % (self.address_string(), line))

        def _send(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode("utf8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _authed(self):
            got = self.headers.get("X-Scout-Key") or ""
            if not got:
                a = self.headers.get("Authorization") or ""
                got = a[7:] if a.lower().startswith("bearer ") else ""
            return bool(got) and hmac.compare_digest(got.encode(), key.encode())

        def _handle(self, verb):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/health":
                return self._send(200, {"ok": True})
            if u.path in ("/connect", "/connect/") and verb == "GET":
                base = scout_connect.public_url()
                mcp_url = base + "/mcp/" + mcp_secret if mcp_secret else ""
                if not mcp_url:
                    return self._send(503, {"error": "Kestrel remote MCP is not configured"})
                b = scout_connect.page(mcp_url).encode("utf8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if u.path in scout_legal.PAGES and verb == "GET":
                b = scout_legal.PAGES[u.path]().encode("utf8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if u.path.startswith("/.well-known/") or u.path.rstrip("/") == "/mcp":   # 14 Nov: 404, not 401, so claude.ai never starts an OAuth sign-in (the secret URL is the auth)
                return self._send(404, {"error": "not found"})
            if mcp_secret and u.path.startswith("/mcp/"):
                if not hmac.compare_digest(u.path[5:].strip("/").encode(), mcp_secret.encode()):
                    return self._send(404, {"error": "not found"})
                if verb == "GET" and "text/event-stream" in (self.headers.get("Accept") or ""):
                    return self._sse_list_changed()
                if verb != "POST":
                    return self._send(405, {"error": "use POST"})
                n = _clen(self.headers.get("Content-Length"))
                if n > MAX_BODY:
                    return self._send(413, {"error": "body too large"})
                try:
                    msg = json.loads(self.rfile.read(n).decode("utf8") or "{}")
                except ValueError:
                    return self._send(400, {"error": "body must be JSON"})
                scout_core.CALLER.name = "mcp"
                out = [scout_remote_mcp.handle(scout, ROUTES, _conv, m) for m in (msg if isinstance(msg, list) else [msg])]
                out = [o for o in out if o is not None]
                if not out:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                return self._send(200, out if isinstance(msg, list) else out[0])
            if u.path == "/breakdown" and verb == "GET":
                cf = os.environ.get("SCOUT_DASHBOARD_TRUST_CF") == "1" and self.headers.get("Cf-Access-Authenticated-User-Email")
                if not (cf or self._authed()):
                    return self._send(401, {"error": "needs Cloudflare Access login or the key"})
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                if not q.get("user"):
                    return self._send(400, {"error": "add ?user=<instagram handle>&me=<your account>"})
                scout_core.CALLER.name = "showcase"
                try:
                    import scout_showcase
                    data = scout.creator_breakdown(q["user"], me=q.get("me"),
                                                   platform=q.get("platform") or "instagram")
                    b = scout_showcase.render(data, cta=q.get("cta") or os.environ.get("SCOUT_SHOWCASE_CTA", ""),
                                              speed=_num(q.get("speed"), 1)).encode("utf8")
                except ScoutError as e:
                    return self._send(e.status, {"error": str(e)})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if u.path == "/dashboard" and verb == "GET":
                cf = os.environ.get("SCOUT_DASHBOARD_TRUST_CF") == "1" and self.headers.get("Cf-Access-Authenticated-User-Email")
                if not (cf or self._authed()):
                    return self._send(401, {"error": "dashboard needs Cloudflare Access login or the key"})
                scout_core.CALLER.name = "dashboard"
                q = urllib.parse.parse_qs(u.query)
                try:
                    b = scout.dashboard_html((q.get("niche") or [None])[0]).encode("utf8")
                except Exception as e:
                    sys.stderr.write("dashboard error: %r\n" % e)
                    return self._send(500, {"error": "internal error"})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if not self._authed():
                return self._send(401, {"error": "missing or wrong key"})
            if u.path == "/v1/tools":
                import scout_mcp
                return self._send(200, {"tools": scout_mcp.catalog(), "note": scout_mcp.GATEWAY_NOTE})
            if u.path not in ROUTES:
                return self._send(404, {"error": "unknown route", "routes": sorted(ROUTES)})
            name, want, spec = ROUTES[u.path]
            if verb != want:
                return self._send(405, {"error": "use %s for %s" % (want, u.path)})
            params = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if verb == "POST":
                n = _clen(self.headers.get("Content-Length"))
                if n > MAX_BODY:
                    return self._send(413, {"error": "body too large"})
                if n:
                    try:
                        body = json.loads(self.rfile.read(n).decode("utf8") or "{}")
                    except ValueError:
                        return self._send(400, {"error": "body must be JSON"})
                    if not isinstance(body, dict):
                        return self._send(400, {"error": "body must be a JSON object"})
                    params.update({k: v for k, v in body.items() if v is not None})
            kwargs = {}
            try:
                for k, v in params.items():
                    if k not in spec:
                        return self._send(400, {"error": "unknown parameter: " + k, "allowed": sorted(spec)})
                    kwargs[k] = _conv(spec[k], v)
            except (ValueError, TypeError):
                return self._send(400, {"error": "bad value for a parameter"})
            scout_core.CALLER.name = (self.headers.get("X-Scout-Caller") or u.path)[:60]   # per-caller YouTube share
            try:
                return self._send(200, getattr(scout, name)(**kwargs))
            except ScoutError as e:
                return self._send(e.status, {"error": str(e)})
            except TypeError as e:
                return self._send(400, {"error": "missing or bad parameters: " + str(e)})
            except Exception as e:  # never leak a stack trace or a token to the caller
                sys.stderr.write("internal error on %s: %r\n" % (u.path, e))
                return self._send(500, {"error": "internal error"})

        def _sse_list_changed(self):
            """Streamable-HTTP GET stream: on every (re)connect, tell the client the tool list may have changed, so it
            re-reads tools/list by itself after a deploy. Kept open with comments for up to 10 minutes."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                msg = json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                self.wfile.write(("event: message\ndata: %s\n\n" % msg).encode("utf8"))
                self.wfile.flush()
                for _ in range(SSE_TICKS):
                    time.sleep(SSE_TICK_SECONDS)
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True   # open event streams must not block a restart
    return srv


def main():
    key = os.environ.get("SCOUT_API_KEY", "")
    if len(key) < 24:
        sys.exit("Set SCOUT_API_KEY to a random string of at least 24 characters, for example: python3 -c \"import secrets;print(secrets.token_urlsafe(32))\"")
    host, port = os.environ.get("SCOUT_HOST", "127.0.0.1"), int(os.environ.get("SCOUT_PORT", "8799"))
    scout = Kestrel(Config())
    secret = os.environ.get("SCOUT_MCP_SECRET", "")
    if secret and len(secret) < 24:
        sys.exit("SCOUT_MCP_SECRET must be at least 24 characters")
    srv = make_server(scout, key, host, port, secret)
    print("Kestrel listening on http://%s:%d  mode: %s" % (host, port, scout.mode()), flush=True)
    try:
        try:
            import scout_growth
            scout_growth.warm_whisper()   # load speech-to-text now, not on the first breakdown
        except Exception:
            pass
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
