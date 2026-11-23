"""Offline tests for per-user Instagram OAuth credential storage."""
import os
import tempfile
import unittest
from unittest import mock

from cryptography.fernet import Fernet

import scout_auth


class PerUserAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in (
            "SCOUT_AUTH_DB", "SCOUT_IG_CLIENT_ID", "SCOUT_IG_CLIENT_SECRET",
            "SCOUT_IG_REDIRECT_URI", "SCOUT_TOKEN_ENCRYPTION_KEY",
        )}
        os.environ["SCOUT_AUTH_DB"] = os.path.join(self.tmp.name, "auth.sqlite")
        os.environ["SCOUT_IG_CLIENT_ID"] = "test-instagram-app-id"
        os.environ["SCOUT_IG_CLIENT_SECRET"] = "test-instagram-app-secret"
        os.environ["SCOUT_IG_REDIRECT_URI"] = "https://example.test/auth/instagram/callback"
        os.environ["SCOUT_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_oauth_creates_encrypted_user_token_and_revocable_key(self):
        start = scout_auth.start_url()
        from urllib.parse import parse_qs, urlparse
        state = parse_qs(urlparse(start).query)["state"][0]
        responses = [
            {"access_token": "short-lived-test-token", "user_id": "ig-123",
             "permissions": list(scout_auth.SCOPES)},
            {"access_token": "long-lived-secret-token", "expires_in": 5184000},
            {"user_id": "ig-123", "username": "creator_test"},
        ]
        with mock.patch.object(scout_auth, "_request", side_effect=responses):
            result = scout_auth.finish("one-time-code", state)

        self.assertEqual(result["username"], "creator_test")
        self.assertTrue(scout_auth.authenticate_key(result["mcp_key"]))
        account = scout_auth.instagram_account(result["user_id"])
        self.assertEqual(account["token"], "long-lived-secret-token")
        self.assertEqual(account["api_host"], "graph.instagram.com")

        with open(os.environ["SCOUT_AUTH_DB"], "rb") as f:
            stored = f.read()
        self.assertNotIn(b"long-lived-secret-token", stored)
        self.assertNotIn(b"short-lived-test-token", stored)

        scout_auth.disconnect(result["user_id"])
        self.assertIsNone(scout_auth.authenticate_key(result["mcp_key"]))
        self.assertIsNone(scout_auth.instagram_account(result["user_id"]))

    def test_state_is_single_use(self):
        start = scout_auth.start_url()
        from urllib.parse import parse_qs, urlparse
        state = parse_qs(urlparse(start).query)["state"][0]
        self.assertTrue(scout_auth._consume_state(state))
        self.assertFalse(scout_auth._consume_state(state))


if __name__ == "__main__":
    unittest.main()
