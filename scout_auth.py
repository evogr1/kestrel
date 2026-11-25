"""Instagram Login token storage for Kestrel's OAuth authorization server.

Instagram is the identity provider for the first Kestrel release. Instagram tokens
are encrypted at rest. The OAuth client tokens that protect /mcp live in
scout_oauth.py and are deliberately separate from Meta access tokens.
"""
import hashlib
import html
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

SCOPES = ("instagram_business_basic", "instagram_business_manage_insights")
_REFRESH_LOCK = threading.Lock()


def _db_path():
    return os.environ.get("SCOUT_AUTH_DB") or (
        os.environ.get("SCOUT_DB", "/data/.scout/cache.sqlite") + ".auth"
    )


def _connect():
    path = _db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("pragma busy_timeout=30000")
    db.execute("pragma journal_mode=WAL")
    db.execute("""create table if not exists auth_users (
        user_id text primary key, ig_user_id text unique not null, username text not null,
        encrypted_token text not null, expires_at real not null, scopes text not null,
        created_at real not null, updated_at real not null, revoked_at real
    )""")
    db.execute("""create table if not exists auth_states (
        state_hash text primary key, created_at real not null
    )""")
    db.commit()
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return db


def _fernet():
    key = os.environ.get("SCOUT_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("SCOUT_TOKEN_ENCRYPTION_KEY is not configured")
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("SCOUT_TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc


def _enc(token):
    return _fernet().encrypt(token.encode("utf8")).decode("ascii")


def _dec(token):
    return _fernet().decrypt(token.encode("ascii")).decode("utf8")


def _config():
    client_id = os.environ.get("SCOUT_IG_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SCOUT_IG_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("SCOUT_IG_REDIRECT_URI", "").strip()
    if not (client_id and client_secret and redirect_uri):
        raise RuntimeError("Set SCOUT_IG_CLIENT_ID, SCOUT_IG_CLIENT_SECRET and SCOUT_IG_REDIRECT_URI")
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise RuntimeError("SCOUT_IG_REDIRECT_URI must be a valid HTTPS URL")
    return client_id, client_secret, redirect_uri


def _version():
    value = os.environ.get("SCOUT_IG_GRAPH_VERSION", "v27.0").strip()
    return value if value.startswith("v") else "v" + value


def _request(url, data=None):
    req = urllib.request.Request(
        url, data=data, headers={"User-Agent": "Kestrel/1.0"},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Meta OAuth request failed (HTTP %s)" % exc.code) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Could not reach Meta OAuth") from None


def is_invited(ig_user_id):
    """Closed by default. SCOUT_ALLOWED_IG_IDS lists numeric Instagram user IDs, or * to open sign-up."""
    allowed = os.environ.get("SCOUT_ALLOWED_IG_IDS", "").replace(",", " ").split()
    return "*" in allowed or str(ig_user_id) in allowed


def start_url(state):
    """Start Meta Instagram Login using an OAuth-AS transaction state supplied by Kestrel."""
    _fernet()
    client_id, _, redirect_uri = _config()
    if not state or len(state) > 256:
        raise RuntimeError("Invalid OAuth transaction state")
    db = _connect()
    try:
        now = time.time()
        db.execute("delete from auth_states where created_at < ?", (now - 900,))
        db.execute(
            "insert into auth_states(state_hash,created_at) values(?,?)",
            (hashlib.sha256(state.encode()).hexdigest(), now),
        )
        db.commit()
    finally:
        db.close()
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "state": state,
    })
    return "https://www.instagram.com/oauth/authorize?" + query


def _consume_state(state):
    if not state or len(state) > 256:
        return False
    db = _connect()
    try:
        digest = hashlib.sha256(state.encode()).hexdigest()
        db.execute("begin immediate")
        row = db.execute(
            "select created_at from auth_states where state_hash=?", (digest,)
        ).fetchone()
        db.execute("delete from auth_states where state_hash=?", (digest,))
        db.commit()
        return bool(row and 0 <= time.time() - row["created_at"] <= 900)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def finish(code, state):
    """Exchange the Meta authorization code and attach the professional account to a Kestrel user."""
    if not code or len(code) > 4096 or not _consume_state(state):
        raise RuntimeError("This Instagram authorization expired or was already used. Start again.")
    client_id, client_secret, redirect_uri = _config()
    form = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": code,
    }).encode()
    short = _request("https://api.instagram.com/oauth/access_token", form)
    short_token = short.get("access_token")
    if not short_token:
        raise RuntimeError("Meta did not return an Instagram access token")

    exchange = urllib.parse.urlencode({
        "grant_type": "ig_exchange_token",
        "client_secret": client_secret,
        "access_token": short_token,
    })
    long = _request("https://graph.instagram.com/access_token?" + exchange)
    token = long.get("access_token")
    if not token:
        raise RuntimeError("Meta did not return a long-lived Instagram token")

    profile_query = urllib.parse.urlencode({
        "fields": "user_id,username",
        "access_token": token,
    })
    profile = _request(
        "https://graph.instagram.com/%s/me?%s" % (_version(), profile_query)
    )
    ig_user_id = str(profile.get("user_id") or short.get("user_id") or "")
    username = str(profile.get("username") or "").strip().lstrip("@")
    if not ig_user_id or not username:
        raise RuntimeError("Could not verify the Instagram professional account")

    if not is_invited(ig_user_id):
        raise RuntimeError("This Kestrel server is invite only. Ask the operator to add Instagram account ID %s." % ig_user_id)

    expires_at = time.time() + int(long.get("expires_in") or 5184000)
    granted = short.get("permissions")
    scopes = sorted(set(granted if isinstance(granted, list) else SCOPES))
    now = time.time()
    db = _connect()
    try:
        # The Instagram professional account is the stable first-release Kestrel identity.
        existing = db.execute(
            "select user_id from auth_users where ig_user_id=?", (ig_user_id,)
        ).fetchone()
        user_id = existing["user_id"] if existing else str(uuid.uuid4())
        db.execute("""insert into auth_users
            (user_id,ig_user_id,username,encrypted_token,expires_at,scopes,created_at,updated_at,revoked_at)
            values(?,?,?,?,?,?,?,?,null)
            on conflict(ig_user_id) do update set
              username=excluded.username, encrypted_token=excluded.encrypted_token,
              expires_at=excluded.expires_at, scopes=excluded.scopes,
              updated_at=excluded.updated_at, revoked_at=null""",
            (user_id, ig_user_id, username, _enc(token), expires_at,
             json.dumps(scopes), now, now))
        db.commit()
    finally:
        db.close()
    return {
        "user_id": user_id,
        "ig_user_id": ig_user_id,
        "username": username,
        "expires_at": expires_at,
        "scopes": scopes,
    }


def instagram_account(user_id):
    db = _connect()
    try:
        row = db.execute(
            "select * from auth_users where user_id=? and revoked_at is null",
            (user_id,),
        ).fetchone()
        if not row or not is_invited(row["ig_user_id"]):
            return None
        token = _dec(row["encrypted_token"])
        expires_at = row["expires_at"]
        if expires_at - time.time() < 7 * 86400:
            with _REFRESH_LOCK:
                # Re-read after acquiring the lock: another request may have refreshed it.
                row = db.execute(
                    "select * from auth_users where user_id=? and revoked_at is null",
                    (user_id,),
                ).fetchone()
                if not row:
                    return None
                token = _dec(row["encrypted_token"])
                expires_at = row["expires_at"]
                if expires_at - time.time() < 7 * 86400:
                    query = urllib.parse.urlencode({
                        "grant_type": "ig_refresh_token",
                        "access_token": token,
                    })
                    refreshed = _request(
                        "https://graph.instagram.com/refresh_access_token?" + query
                    )
                    token = refreshed.get("access_token") or token
                    expires_at = time.time() + int(refreshed.get("expires_in") or 5184000)
                    db.execute(
                        "update auth_users set encrypted_token=?,expires_at=?,updated_at=? where user_id=?",
                        (_enc(token), expires_at, time.time(), user_id),
                    )
                    db.commit()
        return {
            "label": "user:" + user_id,
            "token": token,
            "ig_user_id": row["ig_user_id"],
            "username": row["username"],
            "api_host": "graph.instagram.com",
            "api_version": _version(),
            "expires_at": expires_at,
            "scopes": json.loads(row["scopes"]),
        }
    finally:
        db.close()


def connection_status(user_id):
    db = _connect()
    try:
        row = db.execute(
            "select username,expires_at,scopes,updated_at from auth_users where user_id=? and revoked_at is null",
            (user_id,),
        ).fetchone()
        if not row:
            return {"connected": False}
        return {
            "connected": True,
            "instagram": row["username"],
            "token_expires_at": row["expires_at"],
            "scopes": json.loads(row["scopes"]),
            "last_connected_at": row["updated_at"],
        }
    finally:
        db.close()



def error_page(message, public_url):
    safe = html.escape(str(message))
    retry = html.escape(public_url.rstrip("/") + "/connect/", quote=True)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Kestrel sign-in error</title></head><body style=\"font:16px system-ui;max-width:680px;margin:60px auto;padding:24px\">"
        "<h1>Instagram connection failed</h1><p>" + safe + "</p><p><a href=\"" + retry + "\">Start again</a></p>"
        "</body></html>"
    )


def disconnect(user_id):
    db = _connect()
    try:
        now = time.time()
        db.execute(
            "update auth_users set revoked_at=?,encrypted_token='',updated_at=? where user_id=?",
            (now, now, user_id),
        )
        db.commit()
    finally:
        db.close()
    # Remove private Instagram response caches from this user's isolated DB.
    cache_path = os.environ.get("SCOUT_DB", "/data/.scout/cache.sqlite")
    cache_dir = cache_path + ".users"
    digest = hashlib.sha256(user_id.encode()).hexdigest()
    user_path = os.path.join(cache_dir, digest + ".sqlite")
    for path in (user_path, cache_path):
        if not os.path.exists(path):
            continue
        try:
            cache = sqlite3.connect(path, timeout=20)
            cache.execute("delete from cache where k like 'mine:igme:%'")
            cache.execute("delete from cache where k like 'mine:igins:%'")
            cache.execute("delete from cache where k like 'myins:%'")
            cache.execute("delete from cache where k like 'myinsfresh:%'")
            cache.commit()
            cache.close()
        except sqlite3.Error:
            try:
                cache.close()
            except Exception:
                pass
    return {"disconnected": True}
