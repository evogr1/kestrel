"""Offline protocol and tenant-isolation tests for Kestrel OAuth."""
import base64
import hashlib
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from unittest import mock

from cryptography.fernet import Fernet

import scout_auth
import scout_oauth
import scout_remote_mcp
from scout_core import CALLER


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class KestrelOAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in (
            "SCOUT_AUTH_DB", "SCOUT_DB", "SCOUT_PUBLIC_URL",
            "SCOUT_IG_CLIENT_ID", "SCOUT_IG_CLIENT_SECRET",
            "SCOUT_IG_REDIRECT_URI", "SCOUT_TOKEN_ENCRYPTION_KEY",
        )}
        os.environ["SCOUT_AUTH_DB"] = os.path.join(self.tmp.name, "auth.sqlite")
        os.environ["SCOUT_DB"] = os.path.join(self.tmp.name, "cache.sqlite")
        os.environ["SCOUT_PUBLIC_URL"] = "https://kestrel.test"
        os.environ["SCOUT_IG_CLIENT_ID"] = "test-app-id"
        os.environ["SCOUT_IG_CLIENT_SECRET"] = "test-app-secret"
        os.environ["SCOUT_IG_REDIRECT_URI"] = "https://kestrel.test/auth/instagram/callback"
        os.environ["SCOUT_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

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
        meta_url = scout_oauth.accept_consent(consent["consent_token"])
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
            redirect_back = scout_oauth.meta_callback("meta-code-" + ig_user_id, meta_state)
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


if __name__ == "__main__":
    unittest.main()
