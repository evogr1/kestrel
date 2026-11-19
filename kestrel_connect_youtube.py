#!/usr/bin/env python3
"""Connect one of your own YouTube channels to Kestrel (read-only).

Needs a Google OAuth client of type "Desktop app" saved as youtube_client.json in your OAuth folder
(default ~/.scout/youtube_oauth, or SCOUT_YT_OAUTH_DIR). Then:

    python3 kestrel_connect_youtube.py mychannel

A browser window opens, you pick the channel and allow read-only access, and Kestrel saves
youtube_token_mychannel.json next to the client file. Use the same name as `channel=` in the tools.
Scopes: YouTube read-only and YouTube Analytics read-only. Kestrel never uploads, edits or deletes anything.
"""
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

SCOPES = "https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/yt-analytics.readonly"


def oauth_dir():
    return os.path.expanduser(os.environ.get("SCOUT_YT_OAUTH_DIR") or os.path.join("~", ".scout", "youtube_oauth"))


def main():
    if len(sys.argv) != 2 or not sys.argv[1].replace("-", "").replace("_", "").isalnum():
        sys.exit("Usage: python3 kestrel_connect_youtube.py <short-channel-name>   (letters, digits, - and _)")
    name, d = sys.argv[1], oauth_dir()
    path = os.path.join(d, "youtube_client.json")
    if not os.path.exists(path):
        sys.exit("Put your Google OAuth client (Desktop app) at %s first. See the README, section 'Your own YouTube channel'." % path)
    with open(path, encoding="utf8") as f:
        c = json.load(f)
    c = c.get("installed") or c.get("web") or c
    state, got = secrets.token_urlsafe(16), {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<p>%s You can close this tab.</p>" % ("Connected." if q.get("code") else "Something went wrong.")).encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = "http://127.0.0.1:%d/" % srv.server_address[1]
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state})
    print("Opening your browser. If it doesn't open, visit:\n\n%s\n" % url)
    webbrowser.open(url)
    t = threading.Thread(target=srv.handle_request)
    t.start()
    t.join(timeout=300)
    if not got.get("code"):
        sys.exit("No sign-in received (%s)." % (got.get("error") or "timed out after 5 minutes"))
    data = urllib.parse.urlencode({"code": got["code"], "client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                                   "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data, timeout=30) as r:
        tok = json.loads(r.read().decode())
    if not tok.get("refresh_token"):
        sys.exit("Google returned no refresh token. Remove Kestrel's access at myaccount.google.com/permissions and run this again.")
    out = os.path.join(d, "youtube_token_%s.json" % name)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": tok["refresh_token"], "scope": tok.get("scope", SCOPES)}, f)
    print("Saved %s. Ask Kestrel for channel=%s." % (out, name))


if __name__ == "__main__":
    main()
