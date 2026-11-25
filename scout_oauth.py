"""OAuth 2.1 authorization server for Kestrel's remote MCP endpoint.

The first-release identity flow uses Instagram Business Login. A user authorizes
their own professional account during /authorize; Kestrel then issues OAuth tokens
to the MCP client. Meta access tokens never leave Kestrel.
"""
import base64
import hashlib
import html
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import uuid

import scout_auth

ACCESS_TTL = 3600
REFRESH_TTL = 60 * 86400
AUTH_CODE_TTL = 180
ALLOWED_SCOPES = {"kestrel", "offline_access"}
# Hosted AI clients that may receive authorization codes. Anything else must be listed by the
# operator in SCOUT_OAUTH_EXTRA_REDIRECTS (space separated, exact URLs).
KNOWN_REDIRECTS = {
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
}
CHATGPT_CALLBACK = re.compile(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]+")
BROWSER_COOKIE = "__Host-kestrel_oauth"
RATE_LIMIT, RATE_WINDOW = 30, 600
_HITS = {}
_HITS_LOCK = threading.Lock()


def allow(key, limit=RATE_LIMIT, window=RATE_WINDOW, now=None):
    """In-memory sliding window for the unauthenticated sign-up endpoints."""
    now = time.time() if now is None else now
    with _HITS_LOCK:
        if len(_HITS) > 10000:
            for old in [k for k, v in _HITS.items() if v[-1] <= now - window]:
                del _HITS[old]
        hits = [t for t in _HITS.get(key, []) if t > now - window]
        ok = len(hits) < limit
        if ok:
            hits.append(now)
        _HITS[key] = hits
        return ok


def issuer():
    value = os.environ.get("SCOUT_PUBLIC_URL", "https://scout.solodac.com").rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("SCOUT_PUBLIC_URL must be a canonical HTTPS origin")
    return value


def resource_uri():
    return issuer() + "/mcp"


def _db():
    db = scout_auth._connect()
    db.execute("""create table if not exists oauth_clients (
        client_id text primary key, client_name text not null, redirect_uris text not null,
        created_at real not null, token_endpoint_auth_method text not null
    )""")
    db.execute("""create table if not exists oauth_pending (
        csrf_hash text primary key, client_id text not null, redirect_uri text not null,
        client_state text not null, code_challenge text not null, resource text not null,
        scope text not null, created_at real not null
    )""")
    db.execute("""create table if not exists oauth_transactions (
        state_hash text primary key, client_id text not null, redirect_uri text not null,
        client_state text not null, code_challenge text not null, resource text not null,
        scope text not null, created_at real not null, browser_hash text not null
    )""")
    if "browser_hash" not in [r[1] for r in db.execute("pragma table_info(oauth_transactions)")]:
        # Rows without a binding can never match, so any sign-in in flight simply restarts.
        db.execute("alter table oauth_transactions add column browser_hash text not null default ''")
    db.execute("""create table if not exists oauth_codes (
        code_hash text primary key, client_id text not null, redirect_uri text not null,
        code_challenge text not null, resource text not null, user_id text not null,
        scope text not null, expires_at real not null, used_at real
    )""")
    db.execute("""create table if not exists oauth_access_tokens (
        token_hash text primary key, family_id text not null, client_id text not null,
        user_id text not null, resource text not null, scope text not null,
        expires_at real not null, revoked_at real
    )""")
    db.execute("""create table if not exists oauth_refresh_tokens (
        token_hash text primary key, family_id text not null, client_id text not null,
        user_id text not null, resource text not null, scope text not null,
        expires_at real not null, created_at real not null, used_at real, revoked_at real
    )""")
    db.commit()
    return db


def authorization_server_metadata():
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": base + "/authorize",
        "token_endpoint": base + "/token",
        "registration_endpoint": base + "/register",
        "revocation_endpoint": base + "/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["kestrel", "offline_access"],
        "authorization_response_iss_parameter_supported": True,
    }


def protected_resource_metadata():
    return {
        "resource": resource_uri(),
        "authorization_servers": [issuer()],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["kestrel"],
    }


def _valid_redirect(uri):
    if not isinstance(uri, str) or len(uri) > 2048:
        return False
    p = urllib.parse.urlparse(uri)
    if p.fragment or p.username or p.password:
        return False
    if p.scheme == "https":
        extra = os.environ.get("SCOUT_OAUTH_EXTRA_REDIRECTS", "").split()
        return uri in KNOWN_REDIRECTS or uri in extra or bool(CHATGPT_CALLBACK.fullmatch(uri))
    # OAuth native-app loopback redirects are allowed only on literal loopback addresses.
    if p.scheme == "http" and p.hostname in ("127.0.0.1", "::1", "[::1]", "localhost"):
        return bool(p.port)
    return False


def register(payload):
    if not isinstance(payload, dict):
        raise ValueError("Registration body must be a JSON object")
    redirects = payload.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects or len(redirects) > 20:
        raise ValueError("redirect_uris must contain one or more exact callback URLs")
    if not all(_valid_redirect(x) for x in redirects):
        raise ValueError("Every redirect URI must be a known AI client callback, a URL the operator allowed, "
                         "or a loopback HTTP URL with a port")
    auth_method = payload.get("token_endpoint_auth_method", "none")
    grants = payload.get("grant_types", ["authorization_code", "refresh_token"])
    response_types = payload.get("response_types", ["code"])
    if auth_method != "none" or "authorization_code" not in grants or "code" not in response_types:
        raise ValueError("Kestrel supports public OAuth clients using authorization_code and PKCE")
    client_id = secrets.token_urlsafe(24)
    client_name = str(payload.get("client_name") or "MCP client")[:120]
    if not client_name.strip():
        client_name = "MCP client"
    db = _db()
    try:
        db.execute(
            "insert into oauth_clients values(?,?,?,?,?)",
            (client_id, client_name, json.dumps(sorted(set(redirects))), time.time(), "none"),
        )
        db.commit()
    finally:
        db.close()
    return {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "client_name": client_name,
        "redirect_uris": redirects,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce(verifier):
    if not isinstance(verifier, str) or not (43 <= len(verifier) <= 128):
        return None
    return _b64url(hashlib.sha256(verifier.encode("ascii", "strict")).digest())


def prepare_authorization(params):
    """Validate an authorization request and return a one-time consent form."""
    client_id = params.get("client_id")
    redirect_uri = params.get("redirect_uri")
    client_state = params.get("state")
    challenge = params.get("code_challenge")
    method = params.get("code_challenge_method")
    response_type = params.get("response_type")
    resource = params.get("resource")
    if response_type != "code":
        raise ValueError("response_type must be code")
    if not client_state or len(client_state) > 1024:
        raise ValueError("state is required")
    if method != "S256" or not challenge or len(challenge) > 128:
        raise ValueError("PKCE with S256 is required")
    if resource != resource_uri():
        raise ValueError("The resource parameter must identify Kestrel's MCP endpoint")
    db = _db()
    try:
        client = db.execute(
            "select client_name,redirect_uris from oauth_clients where client_id=?",
            (client_id,),
        ).fetchone()
    finally:
        db.close()
    if not client:
        raise ValueError("Unknown OAuth client. Register the MCP client first.")
    if redirect_uri not in json.loads(client["redirect_uris"]) or not _valid_redirect(redirect_uri):
        raise ValueError("redirect_uri is not registered for this client")
    requested = set((params.get("scope") or "kestrel offline_access").split())
    if not requested or not requested.issubset(ALLOWED_SCOPES) or "kestrel" not in requested:
        raise ValueError("Unsupported OAuth scope")
    csrf_token = secrets.token_urlsafe(32)
    db = _db()
    try:
        now = time.time()
        db.execute(
            "insert into oauth_pending values(?,?,?,?,?,?,?,?)",
            (hashlib.sha256(csrf_token.encode()).hexdigest(), client_id, redirect_uri,
             client_state, challenge, resource, " ".join(sorted(requested)), now),
        )
        db.execute("delete from oauth_pending where created_at < ?", (now - 900,))
        db.commit()
    finally:
        db.close()
    return {
        "consent_token": csrf_token,
        "client_name": client["client_name"],
        "redirect_uri": redirect_uri,
        "scopes": sorted(requested),
        "instagram_scopes": list(scout_auth.SCOPES),
    }


def consent_page(consent):
    client = html.escape(consent["client_name"])
    token = html.escape(consent["consent_token"], quote=True)
    names = ", ".join(html.escape(x) for x in consent["instagram_scopes"])
    scope = ", ".join(html.escape(x) for x in consent["scopes"])
    target = urllib.parse.urlparse(consent["redirect_uri"])
    destination = html.escape(target.scheme + "://" + target.netloc)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="same-origin">
<title>Authorize {client} with Kestrel</title>
<style>body{{margin:0;background:#0b1018;color:#f3f5f8;font:16px/1.6 system-ui}}main{{max-width:620px;margin:8vh auto;padding:24px}}
section{{background:#141d2a;border:1px solid #29384d;border-radius:14px;padding:24px}}h1{{line-height:1.1}}
p,li{{color:#c5cedb}}button{{background:#4d9cff;color:#06101d;border:0;border-radius:9px;padding:13px 18px;font-weight:700;font-size:16px;cursor:pointer}}
small{{color:#8996a8}}</style></head><body><main><p>K E S T R E L</p><section>
<h1>Connect your Instagram account?</h1>
<p>The unverified application-provided name is <b>{client}</b>. This name is not independently verified by Kestrel.</p>
<p>Access will be sent to: <b>{destination}</b></p>
<p>When you continue, Kestrel will let this AI client use data from the Instagram professional account you authorize, including your profile, media and available account/content insights.</p>
<p><b>Instagram permissions requested:</b> {names}</p>
<p><b>Kestrel access:</b> {scope}</p>
<p>Your Instagram password and Meta access token are not shared with the AI client. The token stays encrypted on Kestrel's server. You can revoke this connection at any time from your connector settings or Instagram's app permissions.</p>
<form method="post" action="/authorize">
<input type="hidden" name="consent_token" value="{token}">
<button type="submit">Continue to Instagram</button>
</form>
<p><small>Only continue if you recognize this AI client and expect it to analyse your account.</small></p>
</section></main></body></html>"""


def accept_consent(csrf_token):
    """Returns (Instagram URL, browser binding). The caller must set the binding as a cookie."""
    if not csrf_token or len(csrf_token) > 256:
        raise ValueError("Consent expired. Start the connector sign-in again.")
    digest = hashlib.sha256(csrf_token.encode()).hexdigest()
    db = _db()
    try:
        db.execute("begin immediate")
        row = db.execute(
            "select * from oauth_pending where csrf_hash=?", (digest,)
        ).fetchone()
        db.execute("delete from oauth_pending where csrf_hash=?", (digest,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    if not row or time.time() - row["created_at"] > 900:
        raise ValueError("Consent expired. Start the connector sign-in again.")
    state = secrets.token_urlsafe(32)
    binding = secrets.token_urlsafe(32)
    db = _db()
    try:
        db.execute(
            "insert into oauth_transactions values(?,?,?,?,?,?,?,?,?)",
            (hashlib.sha256(state.encode()).hexdigest(), row["client_id"],
             row["redirect_uri"], row["client_state"], row["code_challenge"],
             row["resource"], row["scope"], time.time(),
             hashlib.sha256(binding.encode()).hexdigest()),
        )
        db.commit()
    finally:
        db.close()
    return scout_auth.start_url(state), binding

def _append_query(uri, values):
    parsed = urllib.parse.urlsplit(uri)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((k, v) for k, v in values.items() if v is not None)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                    urllib.parse.urlencode(query), parsed.fragment))


def meta_callback(code, state, error=None, binding=None):
    if not state or len(state) > 256:
        raise ValueError("Invalid OAuth state")
    digest = hashlib.sha256(state.encode()).hexdigest()
    db = _db()
    try:
        db.execute("begin immediate")
        row = db.execute(
            "select * from oauth_transactions where state_hash=?", (digest,)
        ).fetchone()
        db.execute("delete from oauth_transactions where state_hash=?", (digest,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    if not row or time.time() - row["created_at"] > 900:
        raise ValueError("This sign-in expired. Start again from your MCP client.")
    # Only the browser that approved the consent page may finish the sign-in.
    if not binding or not hmac.compare_digest(
            hashlib.sha256(binding.encode()).hexdigest(), row["browser_hash"]):
        raise ValueError("This sign-in was started in a different browser. Start again from your MCP client.")
    if error:
        return _append_query(row["redirect_uri"], {
            "error": "access_denied",
            "error_description": "Instagram authorization was declined",
            "state": row["client_state"],
            "iss": issuer(),
        })
    identity = scout_auth.finish(code, state)
    authorization_code = secrets.token_urlsafe(40)
    db = _db()
    try:
        db.execute(
            "insert into oauth_codes values(?,?,?,?,?,?,?,?,null)",
            (hashlib.sha256(authorization_code.encode()).hexdigest(),
             row["client_id"], row["redirect_uri"], row["code_challenge"],
             row["resource"], identity["user_id"], row["scope"],
             time.time() + AUTH_CODE_TTL),
        )
        db.commit()
    finally:
        db.close()
    return _append_query(row["redirect_uri"], {
        "code": authorization_code,
        "state": row["client_state"],
        "iss": issuer(),
    })


def _issue_tokens(client_id, user_id, resource, scope, family_id=None):
    access = secrets.token_urlsafe(48)
    refresh = secrets.token_urlsafe(48) if "offline_access" in scope.split() else None
    family = family_id or str(uuid.uuid4())
    now = time.time()
    db = _db()
    try:
        db.execute(
            "insert into oauth_access_tokens values(?,?,?,?,?,?,?,null)",
            (hashlib.sha256(access.encode()).hexdigest(), family, client_id,
             user_id, resource, scope, now + ACCESS_TTL),
        )
        if refresh:
            db.execute(
                "insert into oauth_refresh_tokens values(?,?,?,?,?,?,?,?,null,null)",
                (hashlib.sha256(refresh.encode()).hexdigest(), family, client_id,
                 user_id, resource, scope, now + REFRESH_TTL, now),
            )
        db.commit()
    finally:
        db.close()
    result = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "scope": scope,
    }
    if refresh:
        result["refresh_token"] = refresh
    return result


def token(payload):
    if not isinstance(payload, dict):
        raise ValueError("Token request must be form encoded")
    grant_type = payload.get("grant_type")
    client_id = payload.get("client_id")
    resource = payload.get("resource")
    if not client_id or resource != resource_uri():
        raise ValueError("client_id and the exact Kestrel resource are required")
    if grant_type == "authorization_code":
        code = payload.get("code") or ""
        redirect_uri = payload.get("redirect_uri") or ""
        verifier = payload.get("code_verifier") or ""
        if not code or not redirect_uri:
            raise ValueError("code and redirect_uri are required")
        db = _db()
        try:
            db.execute("begin immediate")
            row = db.execute(
                "select * from oauth_codes where code_hash=?",
                (hashlib.sha256(code.encode()).hexdigest(),),
            ).fetchone()
            if (not row or row["used_at"] is not None or row["expires_at"] <= time.time()
                or row["client_id"] != client_id or row["redirect_uri"] != redirect_uri
                or row["resource"] != resource):
                db.rollback()
                raise ValueError("Invalid or expired authorization code")
            computed = _pkce(verifier)
            if not computed or not hmac.compare_digest(computed, row["code_challenge"]):
                db.rollback()
                raise ValueError("PKCE verification failed")
            changed = db.execute(
                "update oauth_codes set used_at=? where code_hash=? and used_at is null",
                (time.time(), hashlib.sha256(code.encode()).hexdigest()),
            ).rowcount
            if changed != 1:
                db.rollback()
                raise ValueError("Authorization code has already been used")
            user_id, scope = row["user_id"], row["scope"]
            db.commit()
        finally:
            db.close()
        return _issue_tokens(client_id, user_id, resource, scope)

    if grant_type == "refresh_token":
        refresh = payload.get("refresh_token") or ""
        digest = hashlib.sha256(refresh.encode()).hexdigest()
        db = _db()
        try:
            db.execute("begin immediate")
            row = db.execute(
                "select * from oauth_refresh_tokens where token_hash=?", (digest,)
            ).fetchone()
            if not row or row["client_id"] != client_id or row["resource"] != resource:
                db.rollback()
                raise ValueError("Invalid refresh token")
            if row["used_at"] is not None:
                # Reuse indicates a stolen token: revoke the entire refresh family and access tokens.
                db.execute(
                    "update oauth_refresh_tokens set revoked_at=? where family_id=?",
                    (time.time(), row["family_id"]),
                )
                db.execute(
                    "update oauth_access_tokens set revoked_at=? where family_id=?",
                    (time.time(), row["family_id"]),
                )
                db.commit()
                raise ValueError("Refresh token reuse detected; sign in again")
            if row["revoked_at"] is not None or row["expires_at"] <= time.time():
                db.rollback()
                raise ValueError("Refresh token expired or revoked")
            db.execute(
                "update oauth_refresh_tokens set used_at=? where token_hash=? and used_at is null",
                (time.time(), digest),
            )
            identity = (row["user_id"], row["scope"], row["family_id"])
            db.commit()
        finally:
            db.close()
        return _issue_tokens(client_id, identity[0], resource, identity[1], identity[2])

    raise ValueError("Unsupported grant_type")


def validate_access(raw_token):
    if not raw_token or len(raw_token) > 512:
        return None
    db = _db()
    try:
        row = db.execute(
            """select user_id from oauth_access_tokens
               where token_hash=? and resource=? and revoked_at is null and expires_at>?""",
            (hashlib.sha256(raw_token.encode()).hexdigest(), resource_uri(), time.time()),
        ).fetchone()
        return row["user_id"] if row else None
    finally:
        db.close()


def revoke(payload):
    raw = (payload or {}).get("token") or ""
    if not raw or len(raw) > 512:
        return {"revoked": True}
    digest = hashlib.sha256(raw.encode()).hexdigest()
    db = _db()
    try:
        now = time.time()
        access = db.execute(
            "select family_id from oauth_access_tokens where token_hash=?", (digest,)
        ).fetchone()
        refresh = db.execute(
            "select family_id from oauth_refresh_tokens where token_hash=?", (digest,)
        ).fetchone()
        if access:
            db.execute("update oauth_access_tokens set revoked_at=? where token_hash=?", (now, digest))
        if refresh:
            db.execute("update oauth_refresh_tokens set revoked_at=? where family_id=?", (now, refresh["family_id"]))
            db.execute("update oauth_access_tokens set revoked_at=? where family_id=?", (now, refresh["family_id"]))
        db.commit()
    finally:
        db.close()
    return {"revoked": True}


def revoke_user(user_id):
    db = _db()
    try:
        now = time.time()
        db.execute("update oauth_access_tokens set revoked_at=? where user_id=?", (now, user_id))
        db.execute("update oauth_refresh_tokens set revoked_at=? where user_id=?", (now, user_id))
        db.commit()
    finally:
        db.close()
