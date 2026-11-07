"""Tests für die Login-/Selbstheilungslogik in garmin_client.py.

Kein Netzwerk: Statt der echten Library bekommt GarminClient eine Fake-
Factory. Die Fakes schreiben die Token-Datei so, wie die Library es tut.

Aufruf:  .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from garmin_client import (
    AUTH_COOLDOWN_S,
    GarminAuthRequired,
    GarminClient,
    GarminRateLimited,
)


def _fake_jwt(exp: int) -> str:
    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'RS256'})}.{enc({'exp': exp})}.sig"


class FakeGarmin:
    """Minimaler Stand-in für garminconnect.Garmin."""

    def __init__(self, world: "World", **_: object) -> None:
        self.world = world
        self.tokens: str | None = None

    def login(self, tokenstore: str) -> None:
        self.world.logins += 1
        if self.world.login_error is not None:
            raise self.world.login_error
        self.tokens = (Path(tokenstore) / "garmin_tokens.json").read_text()

    def get_sleep_data(self, day: str) -> dict:
        self.world.calls += 1
        if self.tokens != self.world.valid_tokens:
            raise GarminConnectAuthenticationError("Authentication failed: API Error 401")
        return {"day": day}


class World:
    """Was 'Garmin' gerade für gültig hält, plus Zähler."""

    def __init__(self, token_dir: Path) -> None:
        self.token_dir = token_dir
        self.logins = 0
        self.calls = 0
        self.login_error: Exception | None = None
        self.valid_tokens = ""
        self.now = 1_000_000.0

    def write_tokens(self, label: str) -> None:
        """Simuliert einen Prozess/login.py, der neue Tokens speichert."""
        payload = json.dumps(
            {"di_token": _fake_jwt(int(self.now) + 3600), "di_refresh_token": label}
        )
        tmp = self.token_dir / ".tmp"
        tmp.write_text(payload)
        tmp.replace(self.token_dir / "garmin_tokens.json")  # atomar, neuer Inode
        self.valid_tokens = payload

    def factory(self, **kwargs: object) -> FakeGarmin:
        return FakeGarmin(self, **kwargs)


class GarminClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.world = World(self.dir)
        self.world.write_tokens("rt-1")
        self.client = GarminClient(
            email="", password="", tokenstore=str(self.dir),
            garmin_factory=self.world.factory, clock=lambda: self.world.now,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_logs_in_once_and_reuses_session(self) -> None:
        self.client.get_sleep_data("2026-11-07")
        self.client.get_sleep_data("2026-11-07")
        self.assertEqual(self.world.logins, 1)
        self.assertEqual(self.world.calls, 2)

    def test_reloads_when_another_process_rotated_tokens(self) -> None:
        self.client.get_sleep_data("2026-11-07")
        # Ein anderer Prozess erneuert: alter Refresh-Token ist nun ungültig.
        self.world.write_tokens("rt-2")
        self.assertEqual(self.client.get_sleep_data("2026-11-07"), {"day": "2026-11-07"})
        self.assertEqual(self.world.logins, 2)
        # Kein fehlgeschlagener Aufruf nötig — der Wechsel wurde vorher erkannt.
        self.assertEqual(self.world.calls, 2)

    def test_401_reloads_from_disk_and_retries_once(self) -> None:
        self.client.get_sleep_data("2026-11-07")
        # Garmin widerruft die Tokens im Speicher; auf Platte liegen gültige,
        # aber die Datei-Änderung wurde (z.B. gleicher Stand) nicht bemerkt.
        self.client._api.tokens = "stale"
        self.assertEqual(self.client.get_sleep_data("2026-11-07"), {"day": "2026-11-07"})
        self.assertEqual(self.world.logins, 2)

    def test_dead_tokens_raise_auth_required_then_pause_logins(self) -> None:
        self.client.get_sleep_data("2026-11-07")
        self.world.valid_tokens = "revoked-everything"
        with self.assertRaises(GarminAuthRequired) as ctx:
            self.client.get_sleep_data("2026-11-07")
        self.assertIn("login.py", str(ctx.exception))
        self.assertIn("Ursache", str(ctx.exception))

        logins_before = self.world.logins
        with self.assertRaises(GarminAuthRequired) as ctx:
            self.client.get_sleep_data("2026-11-07")
        self.assertIn("Kein neuer Login-Versuch", str(ctx.exception))
        self.assertEqual(self.world.logins, logins_before)  # fail fast, kein Netz

        # Nach Ablauf der Pause wird wieder probiert.
        self.world.now += AUTH_COOLDOWN_S + 1
        with self.assertRaises(GarminAuthRequired):
            self.client.get_sleep_data("2026-11-07")
        self.assertGreater(self.world.logins, logins_before)

    def test_running_login_py_lifts_pause_immediately(self) -> None:
        self.world.login_error = GarminConnectAuthenticationError(
            "Username and password are required"
        )
        with self.assertRaises(GarminAuthRequired) as ctx:
            self.client.get_sleep_data("2026-11-07")
        self.assertIn("Username and password are required", str(ctx.exception))

        self.world.login_error = None
        self.world.write_tokens("rt-fresh")  # login.py lief
        self.assertEqual(self.client.get_sleep_data("2026-11-07"), {"day": "2026-11-07"})

    def test_rate_limit_at_login_is_reported_and_paused(self) -> None:
        self.world.login_error = GarminConnectTooManyRequestsError("429")
        with self.assertRaises(GarminRateLimited):
            self.client.get_sleep_data("2026-11-07")
        self.world.login_error = None
        with self.assertRaises(GarminRateLimited):
            self.client.get_sleep_data("2026-11-07")
        self.assertEqual(self.world.logins, 1)

    def test_auth_status_never_contains_token_values(self) -> None:
        self.client.get_sleep_data("2026-11-07")
        status = self.client.auth_status()
        dumped = json.dumps(status)
        self.assertNotIn("rt-1", dumped)
        self.assertNotIn(self.world.valid_tokens, dumped)
        self.assertTrue(status["token_file_exists"])
        self.assertTrue(status["has_refresh_token"])
        self.assertFalse(status["access_token_expired"])
        self.assertTrue(status["session_active_in_this_process"])

    def test_lock_file_is_private(self) -> None:
        self.client.get_sleep_data("2026-11-07")
        mode = os.stat(self.dir / ".lock").st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
