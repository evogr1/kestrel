"""Per-user Kestrel credentials and Instagram Business Login.

The MCP URL is a revocable, per-user bearer credential. Instagram tokens are encrypted
at rest and are never returned by this module. Configure:
  SCOUT_IG_CLIENT_ID, SCOUT_IG_CLIENT_SECRET, SCOUT_IG_REDIRECT_URI
  SCOUT_TOKEN_ENCRYPTION_KEY (Fernet key; generate with the command in README)
"""
import hashlib
import html
import json
import os
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_insights",
    "instagram_business_content_publish",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
)


def _db_path():
    return os.environ.get("SCOUT_AUTH_DB") or (os.environ.get("SCOUT_DB", "/data/.scout/cache.sqlite") + ".auth")


def _connect():
    path = _db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    db = sqlite3.connect(path, timeout=20)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=WAL")
    db.execute("""create table if not exists auth_users (
        user_id text primary key, ig_user_id text unique not null, username text not null,
        encrypted_token text not null, expires_at real not null, scopes text not null,
        created_at real not null, updated_at real not null, revoked_at real
    )""")
    db.execute("""create table if not exists auth_keys (
        key_hash text primary key, user_id text not null, created_at real not null,
        last_used real, revoked_at real, foreign key(user_id) references auth_users(user_id)
    )""")
    db.execute("""create table if not exists auth_states (
        state_hash text primary key, created_at real not null
    )""")
    db.commit()
    return db


def _fernet():
    key = os.environ.get("SCOUT_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("SCOUT_TOKEN_ENCRYPTION_KEY is not configured")
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode("ascii"))
    except Exception as e:
        raise RuntimeError("SCOUT_TOKEN_ENCRYPTION_KEY must be a valid Fernet key and cryptography must be installed") from e


def _enc(token):
    return _fernet().encrypt(token.encode("utf8")).decode("ascii")


def _dec(token):
    return _fernet().decrypt(token.encode("ascii")).decode("utf8")


def _config():
    cid = os.environ.get("SCOUT_IG_CLIENT_ID", "").strip()
    secret = os.environ.get("SCOUT_IG_CLIENT_SECRET", "").strip()
    redirect = os.environ.get("SCOUT_IG_REDIRECT_URI", "").strip()
    if not (cid and secret and redirect):
        raise RuntimeError("Set SCOUT_IG_CLIENT_ID, SCOUT_IG_CLIENT_SECRET and SCOUT_IG_REDIRECT_URI")
    if not redirect.startswith("https://"):
        raise RuntimeError("SCOUT_IG_REDIRECT_URI must use HTTPS")
    return cid, secret, redirect


def _request(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Kestrel/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf8"))
    except urllib.error.HTTPError as e:
        # Do not include request URLs or tokens in surfaced errors.
        raise RuntimeError("Meta OAuth request failed (HTTP %s)" % e.code)
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Could not reach Meta OAuth")


def start_url():
    _fernet()  # fail before redirect if durable token encryption is not configured
    cid, secret, redirect = _config()
    state = secrets.token_urlsafe(32)
    db = _connect()
    try:
        db.execute("delete from auth_states where created_at < ?", (time.time() - 900,))
        db.execute("insert into auth_states(state_hash,created_at) values(?,?)",
                   (hashlib.sha256(state.encode()).hexdigest(), time.time()))
        db.commit()
    finally:
        db.close()
    q = urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code",
        "scope": ",".join(SCOPES), "state": state,
    })
    return "https://www.instagram.com/oauth/authorize?" + q


def _consume_state(state):
    if not state or len(state) > 256:
        return False
    db = _connect()
    try:
        h = hashlib.sha256(state.encode()).hexdigest()
        row = db.execute("select created_at from auth_states where state_hash=?", (h,)).fetchone()
        db.execute("delete from auth_states where state_hash=?", (h,))
        db.commit()
        return bool(row and time.time() - row["created_at"] <= 900)
    finally:
        db.close()


def finish(code, state):
    if not _consume_state(state):
        raise RuntimeError("This Instagram login link expired or was already used. Start again.")
    cid, secret, redirect = _config()
    form = urllib.parse.urlencode({
        "client_id": cid, "client_secret": secret, "grant_type": "authorization_code",
        "redirect_uri": redirect, "code": code,
    }).encode()
    short = _request("https://api.instagram.com/oauth/access_token", form)
    short_token = short.get("access_token")
    if not short_token:
        raise RuntimeError("Meta did not return an Instagram access token")
    exchange = urllib.parse.urlencode({
        "grant_type": "ig_exchange_token", "client_secret": secret, "access_token": short_token,
    })
    long = _request("https://graph.instagram.com/access_token?" + exchange)
    token = long.get("access_token")
    if not token:
        raise RuntimeError("Meta did not return a long-lived Instagram token")
    profile = _request("https://graph.instagram.com/%s/me?" % os.environ.get("SCOUT_IG_GRAPH_VERSION", "v27.0") + urllib.parse.urlencode({
        "fields": "user_id,username", "access_token": token,
    }))
    ig_id = str(profile.get("user_id") or short.get("user_id") or "")
    username = str(profile.get("username") or "").strip().lstrip("@")
    if not ig_id or not username:
        raise RuntimeError("Could not verify the Instagram professional account")
    expires = time.time() + int(long.get("expires_in") or 5184000)
    scopes = short.get("permissions") or SCOPES
    db = _connect()
    try:
        old = db.execute("select user_id from auth_users where ig_user_id=?", (ig_id,)).fetchone()
        user_id = old["user_id"] if old else str(uuid.uuid4())
        now = time.time()
        db.execute("""insert into auth_users(user_id,ig_user_id,username,encrypted_token,expires_at,scopes,created_at,updated_at,revoked_at)
            values(?,?,?,?,?,?,?,?,null)
            on conflict(ig_user_id) do update set username=excluded.username, encrypted_token=excluded.encrypted_token,
            expires_at=excluded.expires_at, scopes=excluded.scopes, updated_at=excluded.updated_at, revoked_at=null""",
            (user_id, ig_id, username, _enc(token), expires, json.dumps(scopes), now, now))
        # Rotate the user's Kestrel key on every successful fresh Instagram authorization.
        db.execute("update auth_keys set revoked_at=? where user_id=? and revoked_at is null", (now, user_id))
        raw_key = secrets.token_urlsafe(40)
        db.execute("insert into auth_keys(key_hash,user_id,created_at) values(?,?,?)",
                   (hashlib.sha256(raw_key.encode()).hexdigest(), user_id, now))
        db.commit()
    finally:
        db.close()
    return {"user_id": user_id, "ig_user_id": ig_id, "username": username,
            "expires_at": expires, "mcp_key": raw_key}


def authenticate_key(raw_key):
    if not raw_key or len(raw_key) > 256:
        return None
    db = _connect()
    try:
        h = hashlib.sha256(raw_key.encode()).hexdigest()
        row = db.execute("""select k.user_id from auth_keys k join auth_users u on u.user_id=k.user_id
            where k.key_hash=? and k.revoked_at is null and u.revoked_at is null""", (h,)).fetchone()
        if not row:
            return None
        db.execute("update auth_keys set last_used=? where key_hash=?", (time.time(), h))
        db.commit()
        return row["user_id"]
    finally:
        db.close()


def instagram_account(user_id):
    db = _connect()
    try:
        row = db.execute("select * from auth_users where user_id=? and revoked_at is null", (user_id,)).fetchone()
        if not row:
            return None
        token = _dec(row["encrypted_token"])
        # Refresh long-lived tokens once they are 7 days from expiry. Meta requires
        # a valid token at least 24 hours old; refreshing well before day 60 avoids expiry.
        if row["expires_at"] - time.time() < 7 * 86400:
            _, secret, _ = _config()
            q = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": token})
            refreshed = _request("https://graph.instagram.com/refresh_access_token?" + q)
            token = refreshed.get("access_token") or token
            expires = time.time() + int(refreshed.get("expires_in") or 5184000)
            db.execute("update auth_users set encrypted_token=?,expires_at=?,updated_at=? where user_id=?",
                       (_enc(token), expires, time.time(), user_id))
            db.commit()
            row = dict(row)
            row["expires_at"] = expires
        return {"label": "user:" + user_id, "token": token, "ig_user_id": row["ig_user_id"],
                "username": row["username"], "api_host": "graph.instagram.com", "api_version": os.environ.get("SCOUT_IG_GRAPH_VERSION", "v27.0"),
                "expires_at": row["expires_at"], "scopes": json.loads(row["scopes"])}
    finally:
        db.close()


def connection_status(user_id):
    db = _connect()
    try:
        row = db.execute("select username,expires_at,scopes,updated_at from auth_users where user_id=? and revoked_at is null",
                         (user_id,)).fetchone()
        if not row:
            return {"connected": False}
        return {"connected": True, "instagram": row["username"], "token_expires_at": row["expires_at"],
                "scopes": json.loads(row["scopes"]), "last_connected_at": row["updated_at"]}
    finally:
        db.close()


def disconnect(user_id):
    db = _connect()
    try:
        row = db.execute("select ig_user_id,username from auth_users where user_id=?", (user_id,)).fetchone()
        now = time.time()
        db.execute("update auth_users set revoked_at=?,encrypted_token='',updated_at=? where user_id=?",
                   (now, now, user_id))
        db.execute("update auth_keys set revoked_at=? where user_id=? and revoked_at is null", (now, user_id))
        db.commit()
    finally:
        db.close()
    # Remove the account-specific cache and saved Kestrel profile as part of local disconnect.
    cache_path = os.environ.get("SCOUT_DB", "/data/.scout/cache.sqlite")
    if row and os.path.exists(cache_path):
        cache = sqlite3.connect(cache_path, timeout=20)
        try:
            cache.execute("delete from cache where k like ?", ("mine:igme:" + row["ig_user_id"],))
            cache.execute("delete from cache where k like ?", ("mine:igins:" + row["ig_user_id"] + ":%",))
            cache.execute("delete from cache where k=?", ("myins:" + row["username"].lower(),))
            cache.execute("delete from cache where k=?", ("myinsfresh:" + row["username"].lower(),))
            cache.execute("delete from my_profile_users where user_id=?", (user_id,))
        except sqlite3.OperationalError:
            pass
        finally:
            cache.commit()
            cache.close()
    return {"disconnected": True}


def success_page(result, public_url):
    key = result["mcp_key"]
    endpoint = html.escape(public_url.rstrip("/") + "/mcp/u/" + key, quote=True)
    username = html.escape(result["username"])
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kestrel connected</title><style>body{{margin:0;background:#0b1018;color:#f3f5f8;font:16px/1.6 system-ui}}main{{max-width:680px;margin:64px auto;padding:24px}}.panel{{background:#141d2a;border:1px solid #29384d;border-radius:14px;padding:22px}}code{{overflow-wrap:anywhere;color:#a9ceff}}.url{{background:#080d14;padding:14px;border-radius:8px}}button{{background:#4d9cff;border:0;border-radius:8px;padding:12px 18px;font-weight:700;cursor:pointer}}</style></head>
<body><main><h1>Instagram connected</h1><p>@{username} is now connected to your Kestrel account.</p>
<section class="panel"><h2>Your private Kestrel connector URL</h2><p>Copy this URL into your ChatGPT or Claude custom MCP connector. It is unique to you and can be revoked.</p>
<div class="url"><code id="url">{endpoint}</code></div><button onclick="navigator.clipboard.writeText(document.getElementById('url').textContent).then(()=>this.textContent='Copied')">Copy connector URL</button>
<p><small>This URL is your personal Kestrel credential. Do not share it. You will only see it once. Reconnect Instagram to rotate it.</small></p></section>
<p>Use this connector to access your own Instagram insights. Public creator research continues to use Kestrel's separate discovery configuration.</p>
<p><a href="{html.escape(public_url.rstrip('/') + '/auth/disconnect/' + key, quote=True)}">Disconnect this Instagram account</a></p></main></body></html>"""


def disconnect_page(key):
    safe = html.escape(key, quote=True)
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Disconnect Kestrel</title></head>
<body style="font:16px system-ui;max-width:640px;margin:60px auto;padding:24px"><h1>Disconnect Instagram from Kestrel?</h1>
<p>This immediately revokes your private Kestrel connector URL and deletes the encrypted Instagram token from Kestrel.</p>
<form method="post" action="/auth/disconnect/{safe}"><button type="submit">Disconnect my account</button></form></body></html>"""


def error_page(message, public_url):
    safe_message = html.escape(str(message))
    retry = html.escape(public_url.rstrip("/") + "/connect/", quote=True)
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Kestrel connection error</title></head>
<body style="font:16px system-ui;max-width:680px;margin:60px auto;padding:24px"><h1>Instagram connection failed</h1><p>{safe_message}</p><p><a href="{retry}">Start again</a></p></body></html>"""

def error_page(message, public_url):
    return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Kestrel connection error</title></head>
<body style="font:16px system-ui;max-width:680px;margin:60px auto;padding:24px"><h1>Instagram connection failed</h1><p>""" + html.escape(str(message)) + """</p><p><a href="""" + html.escape(public_url.rstrip("/") + "/connect/", quote=True) + """">Start again</a></p></body></html>"""
