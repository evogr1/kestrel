"""Offline tests for Instagram identity/token storage."""
import hashlib
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from unittest import mock

from cryptography.fernet import Fernet

import scout_auth


class InstagramAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {k: os.environ.get(k) for k in (
            "SCOUT_AUTH_DB", "SCOUT_DB", "SCOUT_IG_CLIENT_ID",
            "SCOUT_IG_CLIENT_SECRET", "SCOUT_IG_REDIRECT_URI",
            "SCOUT_TOKEN_ENCRYPTION_KEY", "SCOUT_IG_GRAPH_VERSION",
        )}
        os.environ["SCOUT_AUTH_DB"] = os.path.join(self.tmp.name, "auth.sqlite")
        os.environ["SCOUT_DB"] = os.path.join(self.tmp.name, "cache.sqlite")
        os.environ["SCOUT_IG_CLIENT_ID"] = "test-app-id"
        os.environ["SCOUT_IG_CLIENT_SECRET"] = "test-app-secret"
        os.environ["SCOUT_IG_REDIRECT_URI"] = "https://kestrel.test/auth/instagram/callback"
        os.environ["SCOUT_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        os.environ["SCOUT_IG_GRAPH_VERSION"] = "v27.0"

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_meta_login_stores_encrypted_token_and_disconnects(self):
        state = "test-state-123456789"
        url = scout_auth.start_url(state)
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["state"][0], state)
        self.assertIn("instagram_business_basic", query["scope"][0])
        self.assertIn("instagram_business_manage_insights", query["scope"][0])

        responses = [
            {"access_token": "short-secret-token", "user_id": "ig-111",
             "permissions": list(scout_auth.SCOPES)},
            {"access_token": "long-secret-token", "expires_in": 5184000},
            {"user_id": "ig-111", "username": "creator_one"},
        ]
        with mock.patch.object(scout_auth, "_request", side_effect=responses):
            account = scout_auth.finish("one-time-meta-code", state)

        self.assertEqual(account["username"], "creator_one")
        stored = scout_auth.instagram_account(account["user_id"])
        self.assertEqual(stored["token"], "long-secret-token")
        self.assertEqual(stored["api_host"], "graph.instagram.com")
        self.assertEqual(stored["api_version"], "v27.0")

        with open(os.environ["SCOUT_AUTH_DB"], "rb") as handle:
            raw_db = handle.read()
        self.assertNotIn(b"long-secret-token", raw_db)
        self.assertNotIn(b"short-secret-token", raw_db)

        with self.assertRaises(RuntimeError):
            scout_auth.finish("replay-code", state)

        scout_auth.disconnect(account["user_id"])
        self.assertIsNone(scout_auth.instagram_account(account["user_id"]))
        self.assertFalse(scout_auth.connection_status(account["user_id"])["connected"])

    def test_user_cache_files_are_separate(self):
        import scout_core
        cache = scout_core.Cache(os.path.join(self.tmp.name, "state.sqlite"))
        try:
            # The main Kestrel process registers workspace schemas during startup.
            # Tenant DB creation must clone only those definitions, never the rows.
            cache._base_db.execute("create table if not exists tracked (channel_id text primary key, title text)")
            cache._base_db.execute("insert or replace into tracked values('operator-channel','Operator private channel')")
            cache._base_db.commit()

            scout_core.CALLER.user_id = "tenant-A"
            cache.put("private", {"value": "A"}, 60)
            self.assertTrue(cache.db.execute(
                "select 1 from sqlite_master where type='table' and name='tracked'"
            ).fetchone())
            cache.db.execute("insert into tracked values('tenant-A-channel','Tenant A')")
            cache.db.commit()
            path_a = cache.path + ".users/" + hashlib.sha256(b"tenant-A").hexdigest() + ".sqlite"
            self.assertTrue(os.path.exists(path_a))

            scout_core.CALLER.user_id = "tenant-B"
            self.assertIsNone(cache.get("private"))
            cache.put("private", {"value": "B"}, 60)
            self.assertEqual(cache.get("private"), {"value": "B"})
            self.assertEqual(cache.db.execute("select count(*) from tracked").fetchone()[0], 0)

            scout_core.CALLER.user_id = "tenant-A"
            self.assertEqual(cache.get("private"), {"value": "A"})
            self.assertEqual(cache.db.execute("select title from tracked").fetchone()[0], "Tenant A")
        finally:
            scout_core.CALLER.user_id = None


if __name__ == "__main__":
    unittest.main()
