"""garmin_client.py — dünner, robuster Wrapper um die `garminconnect`-Library.

Warum eine eigene Wrapper-Klasse statt `garminconnect` direkt im Server zu nutzen?

1.  **Trennung der Verantwortung.** Der MCP-Server (server.py) soll nur
    "Tools anbieten und Antworten formatieren". Wie der Login funktioniert,
    wo Tokens liegen und wie auf Fehler reagiert wird, gehört hierher.
2.  **Sicherheit an einem Ort.** Credentials und Token-Pfad werden nur hier
    angefasst — so gibt es genau eine Stelle, die man prüfen muss.
3.  **Robustheit an einem Ort.** Login, Token-Erneuerung zwischen mehreren
    Prozessen, Selbstheilung nach Auth-Fehlern und Login-Pausen nach Fehlern.

Designentscheidung zum Login:
    Der MCP-Server kommuniziert mit Claude Desktop über *stdin/stdout*
    (JSON-RPC). Wir können dort also NICHT per `input()` einen 2FA-Code
    abfragen — das würde das Protokoll zerstören. Deshalb:
      • Der erste Login (ggf. mit 2FA) passiert interaktiv über `login.py`.
        Dabei werden OAuth-Tokens lokal zwischengespeichert.
      • Der Server selbst lädt nur diese Tokens und meldet einen klaren
        Fehler, falls noch kein gültiges Token existiert.

Mehrere Prozesse, eine Token-Datei:
    Claude Desktop startet den Server oft mehrfach (Chat, Code-Tab, ...). Alle
    teilen sich `garmin_tokens.json`. Garmin gibt bei jeder Erneuerung einen
    NEUEN Refresh-Token aus; ein Prozess, der noch den alten im Speicher hält,
    scheitert dann beim nächsten Erneuern. Darum:
      • Jeder Garmin-Aufruf läuft unter einem prozessübergreifenden Datei-Lock
        (`.lock` im Token-Verzeichnis) — es erneuert immer nur einer.
      • Vor jedem Aufruf prüfen wir, ob die Token-Datei sich seit unserem
        letzten Stand geändert hat (anderer Prozess oder `login.py`). Wenn ja,
        laden wir sie neu, statt mit veralteten Tokens weiterzumachen.
      • Lehnt Garmin trotzdem ab (401), laden wir einmal neu von der Platte und
        wiederholen den Aufruf genau einmal.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# Eigener Logger. WICHTIG: Logs gehen nach stderr (siehe server.py-Konfig),
# NICHT nach stdout — stdout ist für das MCP-JSON-RPC-Protokoll reserviert.
logger = logging.getLogger("garmin_mcp.client")

# Standard-Ablageort für die OAuth-Tokens. Überschreibbar per Umgebungsvariable
# GARMINTOKENS (so heißt sie auch in der Library selbst).
DEFAULT_TOKENSTORE = "~/.garminconnect"
TOKEN_FILENAME = "garmin_tokens.json"
LOCK_FILENAME = ".lock"

# Nach einem fehlgeschlagenen Login probieren wir es nicht bei jedem
# Tool-Aufruf sofort wieder — das hämmert auf Garmins SSO und verlängert
# Sperren. Ausnahme: Die Token-Datei hat sich geändert (z.B. login.py lief).
AUTH_COOLDOWN_S = 5 * 60
RATE_LIMIT_COOLDOWN_S = 30 * 60

# Garmin's temporary IP ban message — used in two places, kept in one.
_RATE_LIMIT_MSG = (
    "Garmin hat die IP vorübergehend gesperrt (HTTP 429). "
    "Bitte 30–60 Minuten warten und es erneut versuchen — "
    "schnelle Wiederholungen verlängern die Sperre. Ein "
    "Netzwechsel (z.B. Handy-Hotspot) gibt eine neue IP."
)

_RELOGIN_HINT = (
    "Bitte im Terminal im Projektordner '.venv/bin/python login.py' ausführen."
)


class GarminClientError(Exception):
    """Basisklasse für alle Fehler, die dieser Wrapper bewusst wirft."""


class GarminAuthRequired(GarminClientError):
    """Wird geworfen, wenn kein gültiger Login möglich ist ohne Interaktion.

    Typische Ursachen: Es gibt noch keine gecachten Tokens, die Tokens sind
    abgelaufen/widerrufen, oder der Account verlangt 2FA. Die Lösung ist immer
    dieselbe: einmal interaktiv `login.py` ausführen.
    """


class GarminRateLimited(GarminClientError):
    """Garmin hat die IP vorübergehend gesperrt (HTTP 429).

    Eigene Klasse, weil die Lösung eine andere ist als bei Auth-Fehlern:
    NICHT erneut sofort einloggen (das verlängert die Sperre nur), sondern
    abwarten.
    """


def _describe(exc: BaseException) -> str:
    """Kurze, einzeilige Beschreibung einer Exception inkl. ihrer Ursache.

    Die Library-Meldungen sind bereits bereinigt (keine Tokens/Passwörter).
    Wir hängen die Ursachenkette an, damit im Log steht, WARUM es scheiterte
    (z.B. 'DI token refresh failed: 400'), nicht nur DASS.
    """
    parts = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(parts) < 4:
        seen.add(id(cur))
        text = " ".join(str(cur).split())  # mehrzeilige Meldungen glätten
        parts.append(f"{type(cur).__name__}: {text}" if text else type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
    return " ← ".join(parts)


def _jwt_exp(token: Any) -> float | None:
    """Liest den 'exp'-Claim eines JWT (ohne Signaturprüfung), sonst None."""
    if not isinstance(token, str) or token.count(".") < 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:  # noqa: BLE001 - kaputtes Token = unbekanntes Ablaufdatum
        return None


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec="minutes") if ts else None


def _mfa_not_supported() -> str:
    """prompt_mfa-Callback für den Server-Kontext (kein Terminal vorhanden)."""
    raise GarminAuthRequired(
        "Garmin verlangt einen 2FA-Code, aber der MCP-Server kann ihn nicht "
        "abfragen. " + _RELOGIN_HINT
    )


class GarminClient:
    """Verbindet sich (lazy) mit Garmin Connect und kapselt alle Datenabrufe.

    "Lazy" heißt: Der Login passiert erst beim ersten echten Datenabruf, nicht
    schon beim Erzeugen des Objekts. Das macht den Serverstart schnell und
    robust — der Fehler kommt dann erst beim konkreten Tool-Aufruf.
    """

    def __init__(
        self,
        *,
        email: str | None = None,
        password: str | None = None,
        tokenstore: str | None = None,
        prompt_mfa: Callable[[], str] | None = None,
        max_retries: int = 3,
        garmin_factory: Callable[..., Garmin] = Garmin,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Credentials kommen standardmäßig aus der Umgebung — niemals
        # hartkodiert. So landen sie nicht im Code/Repo.
        self._email = email if email is not None else os.getenv("GARMIN_EMAIL")
        self._password = (
            password if password is not None else os.getenv("GARMIN_PASSWORD")
        )

        raw_path = tokenstore or os.getenv("GARMINTOKENS") or DEFAULT_TOKENSTORE
        self._tokenstore = str(Path(raw_path).expanduser())
        self._token_file = Path(self._tokenstore) / TOKEN_FILENAME
        self._lock_file = Path(self._tokenstore) / LOCK_FILENAME

        self._prompt_mfa = prompt_mfa or _mfa_not_supported
        # Die Library wiederholt 5xx- und Netzwerkfehler selbst (mit Backoff);
        # 401/429/4xx gibt sie sofort zurück. Wir reichen nur die Anzahl durch.
        self._retry_attempts = max(0, max_retries)
        self._garmin_factory = garmin_factory
        self._clock = clock

        self._api: Garmin | None = None
        # Stand der Token-Datei, den die aktuelle `_api`-Instanz kennt.
        self._known_token_stamp: tuple[int, int] | None = None

        # Letzter Login-Fehler + bis wann wir keinen neuen Versuch starten.
        self._last_error: str | None = None
        self._last_error_at: float | None = None
        self._blocked_until: float | None = None
        self._blocked_exc: GarminClientError | None = None
        self._blocked_token_stamp: tuple[int, int] | None = None

        # FastMCP führt synchrone Tools in einem Threadpool aus → Threads im
        # selben Prozess serialisieren wir hiermit; flock() erledigt das
        # prozessübergreifend.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Token-Datei & Locking
    # ------------------------------------------------------------------ #
    def _prepare_tokenstore_dir(self) -> None:
        """Legt das Token-Verzeichnis an und setzt sichere Rechte (0700)."""
        token_dir = Path(self._tokenstore)
        token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(token_dir, 0o700)
        except OSError as exc:  # pragma: no cover - plattformabhängig
            logger.warning("Konnte Rechte auf %s nicht setzen: %s", token_dir, exc)

    def _token_stamp(self) -> tuple[int, int] | None:
        """(mtime_ns, inode) der Token-Datei, oder None falls sie fehlt.

        Die Library schreibt atomar per Rename → neue Datei = neuer Inode.
        Zusammen mit mtime erkennen wir so jede Änderung zuverlässig.
        """
        try:
            st = self._token_file.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_ino)

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Thread- UND prozessweiter exklusiver Zugriff auf die Tokens."""
        with self._lock:
            self._prepare_tokenstore_dir()
            fd = os.open(self._lock_file, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # ------------------------------------------------------------------ #
    # Login
    # ------------------------------------------------------------------ #
    def _drop_session(self) -> None:
        self._api = None
        self._known_token_stamp = None

    def _record_failure(self, exc: GarminClientError, cooldown_s: float) -> None:
        now = self._clock()
        self._last_error = str(exc)
        self._last_error_at = now
        self._blocked_until = now + cooldown_s
        self._blocked_exc = exc
        self._blocked_token_stamp = self._token_stamp()

    def _check_cooldown(self) -> None:
        """Wirft den letzten Login-Fehler erneut, solange die Pause läuft.

        Die Pause endet sofort, wenn sich die Token-Datei geändert hat — dann
        hat vermutlich jemand `login.py` ausgeführt oder ein anderer Prozess
        die Tokens erneuert.
        """
        if self._blocked_until is None or self._blocked_exc is None:
            return
        if self._token_stamp() != self._blocked_token_stamp:
            logger.info("Token-Datei hat sich geändert — Login-Pause aufgehoben.")
            self._blocked_until = None
            return
        if self._clock() < self._blocked_until:
            retry_at = _iso(self._blocked_until)
            raise type(self._blocked_exc)(
                f"{self._blocked_exc} (Kein neuer Login-Versuch vor {retry_at}, "
                f"außer die Token-Datei ändert sich.)"
            )
        self._blocked_until = None

    def _login_locked(self) -> Garmin:
        """Frischer Login (Tokens von Platte, sonst Credentials). Lock gehalten."""
        self._check_cooldown()

        api = self._garmin_factory(
            email=self._email,
            password=self._password,
            prompt_mfa=self._prompt_mfa,
            retry_attempts=self._retry_attempts,
        )
        try:
            # login() lädt zuerst gecachte Tokens (und erneuert sie bei Bedarf);
            # nur wenn das scheitert, werden Credentials gebraucht.
            api.login(self._tokenstore)
        except GarminConnectTooManyRequestsError as exc:
            err = GarminRateLimited(_RATE_LIMIT_MSG)
            err.__cause__ = exc
            logger.warning("Login: Rate-Limit — %s", _describe(exc))
            self._record_failure(err, RATE_LIMIT_COOLDOWN_S)
            raise err from exc
        except GarminAuthRequired as exc:
            # Kommt aus _mfa_not_supported: Garmin will einen 2FA-Code.
            logger.warning("Login: %s", _describe(exc))
            self._record_failure(exc, AUTH_COOLDOWN_S)
            raise
        except (GarminConnectAuthenticationError, GarminConnectConnectionError) as exc:
            have_creds = bool(self._email and self._password)
            reason = _describe(exc)
            if self._token_stamp() is None:
                what = f"Keine gespeicherten Tokens in {self._token_file}."
            elif have_creds:
                what = "Gespeicherte Tokens ungültig und Login mit Credentials fehlgeschlagen."
            else:
                what = (
                    "Gespeicherte Tokens sind abgelaufen oder wurden widerrufen, "
                    "und es sind keine Credentials für einen neuen Login gesetzt."
                )
            err = GarminAuthRequired(f"{what} {_RELOGIN_HINT} Ursache: {reason}")
            logger.warning("Login fehlgeschlagen: %s", reason)
            self._record_failure(err, AUTH_COOLDOWN_S)
            raise err from exc

        self._api = api
        self._known_token_stamp = self._token_stamp()
        self._last_error = None
        self._blocked_until = None
        logger.info("Erfolgreich bei Garmin Connect eingeloggt.")
        return api

    def _session_locked(self) -> Garmin:
        """Aktuelle Sitzung; lädt neu, wenn die Token-Datei sich geändert hat."""
        if self._api is not None and self._token_stamp() != self._known_token_stamp:
            logger.info(
                "Token-Datei wurde extern geändert (anderer Prozess/login.py) — "
                "lade Tokens neu."
            )
            self._drop_session()
        if self._api is None:
            return self._login_locked()
        return self._api

    # ------------------------------------------------------------------ #
    # Generischer Aufruf
    # ------------------------------------------------------------------ #
    def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Ruft eine garminconnect-Methode unter Lock auf, mit Selbstheilung.

        Ablauf bei 401: Sitzung verwerfen, Tokens neu von Platte laden, den
        Aufruf GENAU EINMAL wiederholen. Scheitert auch das, ist der Login
        wirklich tot → klare Meldung mit 'login.py'-Hinweis.
        """
        with self._exclusive():
            for attempt in (1, 2):
                api = self._session_locked()
                try:
                    result = getattr(api, method_name)(*args, **kwargs)
                except GarminConnectTooManyRequestsError as exc:
                    logger.warning("%s: Rate-Limit — %s", method_name, _describe(exc))
                    raise GarminRateLimited(_RATE_LIMIT_MSG) from exc
                except GarminConnectAuthenticationError as exc:
                    logger.warning(
                        "%s: Auth-Fehler (Versuch %d/2) — %s",
                        method_name, attempt, _describe(exc),
                    )
                    self._drop_session()
                    if attempt == 1:
                        continue
                    err = GarminAuthRequired(
                        "Garmin lehnt die Tokens ab, auch nach Neuladen. "
                        f"{_RELOGIN_HINT} Ursache: {_describe(exc)}"
                    )
                    self._record_failure(err, AUTH_COOLDOWN_S)
                    raise err from exc
                except GarminConnectConnectionError as exc:
                    # Die Library hat 5xx/Netzwerkfehler bereits wiederholt.
                    logger.warning("%s: %s", method_name, _describe(exc))
                    raise GarminClientError(
                        f"Garmin-Aufruf '{method_name}' fehlgeschlagen: {_describe(exc)}"
                    ) from exc
                else:
                    # Die Library kann während des Aufrufs Tokens erneuert und
                    # gespeichert haben — das ist unser eigener neuer Stand.
                    self._known_token_stamp = self._token_stamp()
                    return result
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ #
    # Diagnose
    # ------------------------------------------------------------------ #
    def auth_status(self, *, verify: bool = False) -> dict[str, Any]:
        """Zustand des Logins — ohne je Token-Inhalte preiszugeben.

        verify=False: nur lokale Infos (Datei, Ablaufzeit, letzter Fehler).
        verify=True:  zusätzlich echter Login-Check gegen Garmin.
        """
        verified: bool | None = None
        verify_error: str | None = None
        if verify:
            try:
                with self._exclusive():
                    self._session_locked()
                verified = True
            except GarminClientError as exc:
                verified, verify_error = False, str(exc)

        stamp = self._token_stamp()
        access_exp = has_refresh = None
        if stamp is not None:
            try:
                data = json.loads(self._token_file.read_text(encoding="utf-8"))
                access_exp = _jwt_exp(data.get("di_token"))
                has_refresh = bool(data.get("di_refresh_token"))
            except (OSError, ValueError):
                has_refresh = False

        now = self._clock()
        blocked = self._blocked_until if (self._blocked_until or 0) > now else None
        return {
            "token_file": str(self._token_file),
            "token_file_exists": stamp is not None,
            "token_file_updated": _iso(stamp[0] / 1e9) if stamp else None,
            "access_token_expires": _iso(access_exp),
            "access_token_expired": (access_exp < now) if access_exp else None,
            "has_refresh_token": has_refresh,
            "credentials_configured": bool(self._email and self._password),
            "session_active_in_this_process": self._api is not None,
            "verified": verified,
            "verify_error": verify_error,
            "last_error": self._last_error,
            "last_error_at": _iso(self._last_error_at),
            "login_paused_until": _iso(blocked),
            "hint": (
                "Access-Tokens laufen nach ~1 Tag ab und werden automatisch per "
                "Refresh-Token erneuert — ein abgelaufenes Access-Token allein ist "
                "kein Problem. " + _RELOGIN_HINT.replace("Bitte", "Falls der Login tot ist:")
            ),
        }

    # ------------------------------------------------------------------ #
    # Öffentliche Datenmethoden — bewusst dünn, eine pro MCP-Tool.
    # Sie geben die rohen Garmin-Strukturen zurück; das Formatieren in
    # kompakte, sprechende Felder passiert in server.py.
    # ------------------------------------------------------------------ #
    def get_activities(self, start: int, limit: int) -> Any:
        return self._call("get_activities", start, limit)

    def get_activities_by_date(self, start_date: str, end_date: str) -> Any:
        # Holt ALLE Aktivitäten in einem Datumsbereich (kein 50er-Limit).
        return self._call("get_activities_by_date", start_date, end_date)

    def get_activity_details(self, activity_id: str | int) -> Any:
        return self._call("get_activity_details", str(activity_id))

    def get_sleep_data(self, date: str) -> Any:
        return self._call("get_sleep_data", date)

    def get_hrv_data(self, date: str) -> Any:
        return self._call("get_hrv_data", date)

    def get_body_battery(self, start_date: str, end_date: str) -> Any:
        return self._call("get_body_battery", start_date, end_date)

    def get_stress_data(self, date: str) -> Any:
        return self._call("get_stress_data", date)

    def get_training_status(self, date: str) -> Any:
        return self._call("get_training_status", date)

    def get_max_metrics(self, date: str) -> Any:
        return self._call("get_max_metrics", date)

    def get_training_readiness(self, date: str) -> Any:
        return self._call("get_training_readiness", date)

    def get_user_summary(self, date: str) -> Any:
        return self._call("get_user_summary", date)

    def get_heart_rates(self, date: str) -> Any:
        return self._call("get_heart_rates", date)

    def get_lactate_threshold(self) -> Any:
        # Ohne Argumente nutzt die Library latest=True — den jüngsten von der
        # Uhr automatisch erkannten Laktatschwellen-Wert (HF + Pace).
        return self._call("get_lactate_threshold")

    def get_activity_hr_in_timezones(self, activity_id: str | int) -> Any:
        # Zeit-in-Zonen einer Aktivität. Wichtig: Die Antwort enthält auch die
        # aktuell im Garmin-Konto KONFIGURIERTEN Zonengrenzen (zoneLowBoundary)
        # — der einzige Leseweg dafür; schreiben kann die API Zonen nicht.
        return self._call("get_activity_hr_in_timezones", str(activity_id))
