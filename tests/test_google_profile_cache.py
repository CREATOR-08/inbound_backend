import unittest
from datetime import datetime, timedelta, timezone
from time import sleep
from unittest.mock import patch

from app.api.google_profile_cache import GoogleProfileVerificationCache


class GoogleProfileVerificationCacheTests(unittest.TestCase):
    def test_cache_hit_is_bound_to_the_expected_account(self):
        cache = GoogleProfileVerificationCache(ttl_seconds=30)
        cache.set("opaque-access-token", "person@example.com")

        self.assertEqual(
            cache.get("opaque-access-token", "PERSON@example.com"),
            "person@example.com",
        )
        self.assertIsNone(cache.get("opaque-access-token", "other@example.com"))
        self.assertIsNone(
            cache.get("opaque-access-token", "person@example.com")
        )

    def test_cache_entry_expires_after_configured_ttl(self):
        cache = GoogleProfileVerificationCache(ttl_seconds=0.01)
        cache.set("opaque-access-token", "person@example.com")
        sleep(0.02)

        self.assertIsNone(cache.get("opaque-access-token", "person@example.com"))

    def test_cache_entry_expires_with_google_token(self):
        cache = GoogleProfileVerificationCache(ttl_seconds=30)
        cache.set(
            "opaque-access-token",
            "person@example.com",
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )

        self.assertIsNone(cache.get("opaque-access-token", "person@example.com"))

    def test_cache_keys_do_not_contain_the_raw_access_token(self):
        cache = GoogleProfileVerificationCache(ttl_seconds=30)
        access_token = "opaque-access-token"
        cache.set(access_token, "person@example.com")

        self.assertNotIn(access_token, repr(cache._entries))
        with patch("app.api.google_profile_cache.monotonic", return_value=1):
            cache.invalidate(access_token)
        self.assertEqual(len(cache._entries), 0)


if __name__ == "__main__":
    unittest.main()
