import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auth import authentication_required, authorized, load_users, password_record


def basic(username, password):
    return "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = password_record("correct horse battery staple")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "users.json"
        self.path.write_text(json.dumps({"version": 1, "users": {"alice": self.record}}), encoding="utf-8")
        self.env = patch.dict(os.environ, {"ADMIN_USERS_FILE": str(self.path), "ADMIN_PASSWORD": "legacy-pass", "ADMIN_USERNAME": "legacy"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_hashes_are_salted_and_not_plaintext(self):
        self.assertNotIn("correct horse battery staple", json.dumps(self.record))
        self.assertNotEqual(self.record, password_record("correct horse battery staple"))

    def test_accepts_correct_password_and_rejects_others(self):
        self.assertTrue(authorized(basic("alice", "correct horse battery staple")))
        self.assertFalse(authorized(basic("alice", "wrong")))
        self.assertFalse(authorized(basic("unknown", "correct horse battery staple")))
        self.assertFalse(authorized(basic("legacy", "legacy-pass")))

    def test_missing_or_corrupt_account_file_fails_closed(self):
        self.path.unlink()
        self.assertTrue(authentication_required())
        self.assertFalse(authorized(basic("legacy", "legacy-pass")))
        self.path.write_text("not json", encoding="utf-8")
        self.assertFalse(authorized(basic("alice", "correct horse battery staple")))

    def test_invalid_headers_are_rejected(self):
        for header in ("", "Bearer token", "Basic !!!", "Basic " + "a" * 2100, "Basic /w=="):
            with self.subTest(header=header[:30]):
                self.assertFalse(authorized(header))

    def test_deleting_user_revokes_access_on_next_request(self):
        self.path.write_text(json.dumps({"version": 1, "users": {"bob": self.record}}), encoding="utf-8")
        self.assertFalse(authorized(basic("alice", "correct horse battery staple")))
        self.assertTrue(authorized(basic("bob", "correct horse battery staple")))

    def test_legacy_authentication_still_works(self):
        with patch("auth.DEFAULT_USERS_PATH", Path(self.temp.name) / "absent.json"), patch.dict(os.environ, {"ADMIN_USERS_FILE": ""}):
            self.assertTrue(authorized(basic("legacy", "legacy-pass")))
            self.assertFalse(authorized(basic("legacy", "wrong")))
            with patch.dict(os.environ, {"ADMIN_PASSWORD": ""}):
                self.assertFalse(authentication_required())
                self.assertTrue(authorized(""))

    def test_rejects_empty_users_and_extreme_hash_cost(self):
        for users in ({}, {"alice": {**self.record, "iterations": 999999999}}):
            self.path.write_text(json.dumps({"version": 1, "users": users}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_users(self.path)


if __name__ == "__main__":
    unittest.main()
