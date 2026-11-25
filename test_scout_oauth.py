"""Offline protocol and tenant-isolation tests for Kestrel OAuth."""
import base64
import hashlib
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse
from unittest import mock

from cryptography.fernet import Fernet

import scout_auth
import scout_oauth
import scout_extras
import scout_growth
import scout_mcp
import scout_remote_mcp
import scout_server
import scout_social
from scout_core import CALLER


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class KestrelOAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in (
            "SCOUT_AUTH_DB", "SCOUT_DB", "SCOUT_PUBLIC_URL",
            "SCOUT_IG_CLIENT_ID", "SCOUT_IG_CLIENT_SECRET",
            "SCOUT_IG_REDIRECT_URI", "SCOUT_TOKEN_ENCRYPTION_KEY", "SCOUT_OAUTH_EXTRA_REDIRECTS",
        )}
        os.environ["SCOUT_AUTH_DB"] = os.path.join(self.tmp.name, "auth.sqlite")
        os.environ["SCOUT_DB"] = os.path.join(self.tmp.name, "cache.sqlite")
        os.environ["SCOUT_PUBLIC_URL"] = "https://kestrel.test"
        os.environ["SCOUT_IG_CLIENT_ID"] = "test-app-id"
        os.environ["SCOUT_IG_CLIENT_SECRET"] = "test-app-secret"
        os.environ["SCOUT_IG_REDIRECT_URI"] = "https://kestrel.test/auth/instagram/callback"
        os.environ["SCOUT_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        os.environ["SCOUT_OAUTH_EXTRA_REDIRECTS"] = " ".join((
            "https://client.example/callback", "https://a.example/callback", "https://b.example/callback"))

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _authorize_and_meta_callback(self, ig_user_id, username, redirect):
        registered = scout_oauth.register({
            "client_name": "Test MCP client",
            "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
        self.assertIsInstance(registered["client_id_issued_at"], int)
        verifier = "x" * 48
        original_state = "client-state-" + ig_user_id
        params = {
            "client_id": registered["client_id"],
            "redirect_uri": redirect,
            "response_type": "code",
            "state": original_state,
            "code_challenge": challenge(verifier),
            "code_challenge_method": "S256",
            "resource": scout_oauth.resource_uri(),
            "scope": "kestrel offline_access",
        }
        consent = scout_oauth.prepare_authorization(params)
        self.assertEqual(consent["client_name"], "Test MCP client")
        page = scout_oauth.consent_page(consent)
        self.assertIn("unverified application-provided name", page)
        meta_url, binding = scout_oauth.accept_consent(consent["consent_token"])
        with self.assertRaises(ValueError):
            scout_oauth.accept_consent(consent["consent_token"])
        meta_state = parse_qs(urlparse(meta_url).query)["state"][0]
        replies = [
            {"access_token": "short-token-" + ig_user_id, "user_id": ig_user_id,
             "permissions": list(scout_auth.SCOPES)},
            {"access_token": "long-token-" + ig_user_id, "expires_in": 5184000},
            {"user_id": ig_user_id, "username": username},
        ]
        with mock.patch.object(scout_auth, "_request", side_effect=replies):
            redirect_back = scout_oauth.meta_callback("meta-code-" + ig_user_id, meta_state, binding=binding)
        q = parse_qs(urlparse(redirect_back).query)
        self.assertEqual(q["state"][0], original_state)
        self.assertEqual(q["iss"][0], scout_oauth.issuer())
        return registered["client_id"], redirect, verifier, q["code"][0]

    def test_pkce_audience_and_refresh_rotation(self):
        redirect = "https://client.example/callback"
        client_id, redirect_uri, verifier, code = self._authorize_and_meta_callback(
            "ig-user-a", "creator_a", redirect
        )
        wrong = {
            "grant_type": "authorization_code", "client_id": client_id,
            "code": code, "redirect_uri": redirect_uri,
            "code_verifier": "y" * 48, "resource": scout_oauth.resource_uri(),
        }
        with self.assertRaises(ValueError):
            scout_oauth.token(wrong)

        token = scout_oauth.token({**wrong, "code_verifier": verifier})
        self.assertEqual(token["token_type"], "Bearer")
        user_id = scout_oauth.validate_access(token["access_token"])
        self.assertTrue(user_id)
        self.assertIsNone(scout_oauth.validate_access("not-a-token"))

        with self.assertRaises(ValueError):
            scout_oauth.token({
                "grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": token["refresh_token"],
                "resource": "https://other.example/mcp",
            })

        rotated = scout_oauth.token({
            "grant_type": "refresh_token", "client_id": client_id,
            "refresh_token": token["refresh_token"],
            "resource": scout_oauth.resource_uri(),
        })
        self.assertTrue(scout_oauth.validate_access(rotated["access_token"]))

        # Reusing the previous refresh token revokes the entire token family.
        with self.assertRaises(ValueError):
            scout_oauth.token({
                "grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": token["refresh_token"],
                "resource": scout_oauth.resource_uri(),
            })
        self.assertIsNone(scout_oauth.validate_access(rotated["access_token"]))

    def test_tenants_get_different_identity_and_oauth_clients(self):
        client_a, redirect_a, verifier_a, code_a = self._authorize_and_meta_callback(
            "ig-user-a", "creator_a", "https://a.example/callback"
        )
        client_b, redirect_b, verifier_b, code_b = self._authorize_and_meta_callback(
            "ig-user-b", "creator_b", "https://b.example/callback"
        )
        self.assertNotEqual(client_a, client_b)
        token_a = scout_oauth.token({
            "grant_type": "authorization_code", "client_id": client_a,
            "code": code_a, "redirect_uri": redirect_a, "code_verifier": verifier_a,
            "resource": scout_oauth.resource_uri(),
        })
        token_b = scout_oauth.token({
            "grant_type": "authorization_code", "client_id": client_b,
            "code": code_b, "redirect_uri": redirect_b, "code_verifier": verifier_b,
            "resource": scout_oauth.resource_uri(),
        })
        self.assertNotEqual(
            scout_oauth.validate_access(token_a["access_token"]),
            scout_oauth.validate_access(token_b["access_token"]),
        )
        self.assertNotEqual(token_a["access_token"], token_b["access_token"])

    def test_tenant_tool_catalog_blocks_operator_owned_accounts(self):
        CALLER.user_id = "tenant-user"
        try:
            names = {row["name"] for row in scout_remote_mcp._catalog_for_caller()}
            self.assertIn("scout_my_ig", names)
            self.assertIn("scout_disconnect_instagram", names)
            self.assertNotIn("scout_my_audience", names)
            response = scout_remote_mcp.handle(
                None, {}, None,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "scout_my_audience", "arguments": {"channel": "operator-channel"}}},
            )
            self.assertTrue(response["result"]["isError"])
            self.assertIn("server operator", response["result"]["content"][0]["text"])
        finally:
            CALLER.user_id = None

    def test_dcr_rejects_insecure_redirects_and_metadata_advertises_pkce(self):
        with self.assertRaises(ValueError):
            scout_oauth.register({"redirect_uris": ["http://attacker.example/callback"]})
        metadata = scout_oauth.authorization_server_metadata()
        self.assertIn("S256", metadata["code_challenge_methods_supported"])
        self.assertIn("offline_access", metadata["scopes_supported"])
        resource = scout_oauth.protected_resource_metadata()
        self.assertEqual(resource["resource"], scout_oauth.resource_uri())
        self.assertEqual(resource["authorization_servers"], [scout_oauth.issuer()])

    def _consent(self, redirect="https://client.example/callback"):
        client = scout_oauth.register({"client_name": "Claude", "redirect_uris": [redirect]})
        return scout_oauth.prepare_authorization({
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
            "state": "s", "code_challenge": challenge("x" * 48), "code_challenge_method": "S256",
            "resource": scout_oauth.resource_uri()})

    def test_callback_requires_the_browser_that_gave_consent(self):
        for binding in (None, "", "someone-elses-cookie"):
            meta_url, _ = scout_oauth.accept_consent(self._consent()["consent_token"])
            state = parse_qs(urlparse(meta_url).query)["state"][0]
            with mock.patch.object(scout_auth, "_request") as meta:
                with self.assertRaises(ValueError):
                    scout_oauth.meta_callback("victim-code", state, binding=binding)
                meta.assert_not_called()

    def test_only_known_redirects_can_register(self):
        for good in ("https://claude.ai/api/mcp/auth_callback",
                     "https://chatgpt.com/connector_platform_oauth_redirect",
                     "https://chatgpt.com/connector/oauth/abc_123-x",
                     "http://127.0.0.1:6274/oauth/callback", "http://localhost:3334/callback"):
            scout_oauth.register({"redirect_uris": [good]})
        for bad in ("https://attacker.example/cb", "https://claude.ai.attacker.example/api/mcp/auth_callback",
                    "https://claude.ai/other", "https://claude.ai/api/mcp/auth_callback?x=1",
                    "https://chatgpt.com/connector/oauth/../x", "https://chatgpt.com/connector/oauth/a/b"):
            with self.assertRaises(ValueError, msg=bad):
                scout_oauth.register({"redirect_uris": [bad]})

    def test_removed_redirect_is_refused_and_consent_page_shows_destination(self):
        consent = self._consent()
        self.assertIn("client.example", scout_oauth.consent_page(consent))
        client = scout_oauth.register({"redirect_uris": ["https://a.example/callback"]})
        os.environ["SCOUT_OAUTH_EXTRA_REDIRECTS"] = ""
        with self.assertRaises(ValueError):
            scout_oauth.prepare_authorization({
                "client_id": client["client_id"], "redirect_uri": "https://a.example/callback",
                "response_type": "code", "state": "s", "code_challenge": challenge("x" * 48),
                "code_challenge_method": "S256", "resource": scout_oauth.resource_uri()})

    def test_old_transaction_table_is_upgraded(self):
        db = scout_auth._connect()
        db.execute("""create table oauth_transactions (state_hash text primary key, client_id text not null,
            redirect_uri text not null, client_state text not null, code_challenge text not null,
            resource text not null, scope text not null, created_at real not null)""")
        db.commit()
        db.close()
        self.assertTrue(scout_oauth.accept_consent(self._consent()["consent_token"])[1])

    def test_rate_limit(self):
        scout_oauth._HITS.clear()
        self.assertTrue(all(scout_oauth.allow("ip1", limit=3, window=60, now=100) for _ in range(3)))
        self.assertFalse(scout_oauth.allow("ip1", limit=3, window=60, now=101))
        self.assertTrue(scout_oauth.allow("ip2", limit=3, window=60, now=101))
        self.assertTrue(scout_oauth.allow("ip1", limit=3, window=60, now=200))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class KestrelHTTPSignInTests(unittest.TestCase):
    setUp_env = KestrelOAuthTests.setUp
    tearDown_env = KestrelOAuthTests.tearDown

    def setUp(self):
        self.setUp_env()
        scout_oauth._HITS.clear()
        self.srv = scout_server.make_server(None, "k" * 32, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.opener = urllib.request.build_opener(_NoRedirect)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tearDown_env()

    def _call(self, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            r = self.opener.open(req, timeout=10)
        except urllib.error.HTTPError as e:
            r = e
        with r:
            return r.code, r.headers, r.read().decode()

    def test_browser_flow_sets_cookie_and_callback_checks_it(self):
        redirect = "https://client.example/callback"
        client = scout_oauth.register({"redirect_uris": [redirect]})
        query = urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
            "state": "s", "code_challenge": challenge("x" * 48), "code_challenge_method": "S256",
            "resource": scout_oauth.resource_uri()})
        replies = [{"access_token": "a", "user_id": "1"}, {"access_token": "b"}, {"user_id": "1", "username": "u"}]
        for send_cookie, expected in ((False, 400), (True, 302)):
            code, headers, body = self._call("/authorize?" + query)
            self.assertEqual(code, 200)
            # no-referrer would make browsers send "Origin: null" and the POST would be refused.
            self.assertEqual(headers["Referrer-Policy"], "same-origin")
            self.assertNotIn('content="no-referrer"', body)
            # form-action also governs the redirects that follow the form post.
            csp = headers["Content-Security-Policy"]
            self.assertIn("https://www.instagram.com", csp)
            self.assertIn("https://client.example", csp)
            token = re.search(r'name="consent_token" value="([^"]+)"', body).group(1)
            code, headers, _ = self._call("/authorize", urlencode({"consent_token": token}).encode(),
                                          {"Origin": "https://kestrel.test"})
            self.assertEqual(code, 302)
            cookie = headers["Set-Cookie"]
            for part in ("__Host-kestrel_oauth=", "Path=/", "Secure", "HttpOnly", "SameSite=Lax"):
                self.assertIn(part, cookie)
            state = parse_qs(urlparse(headers["Location"]).query)["state"][0]
            sent = {"Cookie": "other=1; " + cookie.split(";")[0]} if send_cookie else {}
            with mock.patch.object(scout_auth, "_request", side_effect=list(replies)):
                code, headers, _ = self._call("/auth/instagram/callback?" + urlencode({"code": "c", "state": state}),
                                              headers=sent)
            self.assertEqual(code, expected)
            if send_cookie:
                self.assertTrue(headers["Location"].startswith(redirect + "?"))
                self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_register_is_rate_limited(self):
        body = b'{"redirect_uris":["https://claude.ai/api/mcp/auth_callback"]}'
        codes = [self._call("/register", body)[0] for _ in range(scout_oauth.RATE_LIMIT + 1)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(set(codes[:-1]), {201})


class KestrelTenantIsolationTests(unittest.TestCase):
    def setUp(self):
        CALLER.user_id, CALLER.name = "tenant", "mcp:tenant"

    def tearDown(self):
        CALLER.user_id = CALLER.name = None

    def test_operator_credential_tools_are_hidden_and_rejected(self):
        names = {row["name"] for row in scout_remote_mcp._catalog_for_caller()}
        routes = {t[0]: t[1] for t in scout_mcp.TOOLS}
        for name, route in routes.items():
            if route not in scout_remote_mcp.TENANT_BLOCKED_ROUTES:
                continue
            self.assertNotIn(name, names)
            reply = scout_remote_mcp.handle(None, {}, None, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": "kestrel_call", "arguments": {"tool": name}}})
            self.assertTrue(reply["result"]["isError"], name)
        for route in ("/v1/brief", "/v1/brief/push", "/v1/watch", "/v1/me/weekly", "/v1/jobs/daily",
                      "/v1/changes/scan", "/v1/hooks/extract", "/v1/x/profile", "/v1/x/outliers", "/v1/x/search",
                      "/v1/social/search", "/v1/setup", "/v1/usage", "/v1/ig/seed_outliers"):
            self.assertIn(route, scout_server.ROUTES, route)
            self.assertIn(route, scout_remote_mcp.TENANT_BLOCKED_ROUTES)

    def test_operator_credentials_are_unusable_even_indirectly(self):
        env = {"SCOUT_ALERT_WEBHOOK": "https://op.example/hook", "SCOUT_TRELLO_KEY": "k",
               "SCOUT_TRELLO_TOKEN": "t", "SCOUT_TRELLO_LISTS": '{"p":"l"}', "X_BEARER_TOKEN": "x"}
        with mock.patch.dict(os.environ, env):
            k = scout_extras.Kestrel.__new__(scout_extras.Kestrel)
            k._post = mock.Mock()
            self.assertFalse(k._notify("t", [1]))
            self.assertEqual(k._push_trello({"p": []})["pushed"], 0)
            k._post.assert_not_called()
            x = scout_social.X(None)
            self.assertFalse(x.has_api)
            self.assertEqual(x.bearer, "")
            CALLER.user_id = CALLER.name = None
            self.assertTrue(x.has_api)

    def test_background_thread_keeps_the_caller_identity(self):
        seen = {}
        for uid, name in (("tenant", "mcp:tenant"), (None, "mcp")):
            CALLER.user_id, CALLER.name = uid, name
            g = scout_growth.Growth.__new__(scout_growth.Growth)
            g.cache = mock.Mock()
            g.cache.cached.side_effect = lambda k, t, fn: seen.update(
                who=(getattr(CALLER, "user_id", None), getattr(CALLER, "name", None))) or {}
            g._my_insights_async("me")()
            self.assertEqual(seen["who"], (uid, name))


if __name__ == "__main__":
    unittest.main()
