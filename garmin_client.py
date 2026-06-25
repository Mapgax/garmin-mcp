"""garmin_client.py — dünner, robuster Wrapper um die `garminconnect`-Library.

Warum eine eigene Wrapper-Klasse statt `garminconnect` direkt im Server zu nutzen?

1.  **Trennung der Verantwortung.** Der MCP-Server (server.py) soll nur
    "Tools anbieten und Antworten formatieren". Wie der Login funktioniert,
    wo Tokens liegen und wie auf Rate-Limits reagiert wird, gehört hierher.
2.  **Sicherheit an einem Ort.** Credentials und Token-Pfad werden nur hier
    angefasst — so gibt es genau eine Stelle, die man prüfen muss.
3.  **Robustheit an einem Ort.** Login passiert nur einmal (lazy + gecacht),
    und Lese-Aufrufe werden bei vorübergehenden Fehlern automatisch wiederholt.

Designentscheidung zum Login:
    Der MCP-Server kommuniziert mit Claude Desktop über *stdin/stdout*
    (JSON-RPC). Wir können dort also NICHT per `input()` einen 2FA-Code
    abfragen — das würde das Protokoll zerstören. Deshalb:
      • Der erste Login (ggf. mit 2FA) passiert interaktiv über `login.py`.
        Dabei werden OAuth-Tokens lokal zwischengespeichert.
      • Der Server selbst lädt nur diese Tokens und meldet einen klaren
        Fehler, falls noch kein gültiges Token existiert.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# Eigener Logger. WICHTIG: Logs gehen nach stderr (siehe server.py-Konfig),
# NICHT nach stdout — stdout ist für das MCP-JSON-RPC-Protokoll reserviert.
logger = logging.getLogger("garmin_mcp.client")

# Standard-Ablageort für die OAuth-Tokens. `garminconnect` legt hier mehrere
# Token-Dateien ab und erneuert sie selbstständig, solange sie gültig sind.
# Überschreibbar per Umgebungsvariable GARMINTOKENS (so heißt sie auch in der
# Library selbst).
DEFAULT_TOKENSTORE = "~/.garminconnect"


class GarminClientError(Exception):
    """Basisklasse für alle Fehler, die dieser Wrapper bewusst wirft."""


class GarminAuthRequired(GarminClientError):
    """Wird geworfen, wenn kein gültiger Login möglich ist ohne Interaktion.

    Typische Ursachen: Es gibt noch keine gecachten Tokens, die Tokens sind
    abgelaufen, oder der Account verlangt 2FA. Die Lösung ist immer dieselbe:
    einmal interaktiv `python3 login.py` ausführen.
    """


class GarminRateLimited(GarminClientError):
    """Garmin hat die IP vorübergehend gesperrt (HTTP 429).

    Eigene Klasse, weil die Lösung eine andere ist als bei Auth-Fehlern:
    NICHT erneut sofort einloggen (das verlängert die Sperre nur), sondern
    abwarten. Ohne diese klare Unterscheidung landet ein 429 sonst
    irreführend im 2FA-Prompt der Library.
    """


def _mfa_not_supported() -> str:
    """prompt_mfa-Callback für den Server-Kontext.

    Falls die Library mitten im Login einen 2FA-Code braucht, können wir ihn
    hier nicht beschaffen (kein Terminal). Wir werfen einen sprechenden Fehler,
    statt auf stdin zu blockieren.
    """
    raise GarminAuthRequired(
        "Garmin verlangt einen 2FA-Code, aber der MCP-Server kann ihn nicht "
        "abfragen. Bitte einmalig im Terminal 'python3 login.py' ausführen, "
        "um dich einzuloggen und die Tokens zu cachen."
    )


class GarminClient:
    """Verbindet sich (lazy) mit Garmin Connect und kapselt alle Datenabrufe.

    "Lazy" heißt: Der Login passiert erst beim ersten echten Datenabruf, nicht
    schon beim Erzeugen des Objekts. Das macht den Serverstart schnell und
    robust — Claude Desktop startet den Server, auch wenn Garmin gerade nicht
    erreichbar ist; der Fehler kommt dann erst beim konkreten Tool-Aufruf.
    """

    # Nur ECHTE vorübergehende Fehler werden mit kurzem Backoff wiederholt:
    # Verbindungsabbrüche. Ein 429 (Rate-Limit) gehört NICHT hierher — gegen
    # eine IP-Sperre helfen 1–2s Warten nicht, das wird gesondert behandelt.
    _RETRYABLE = (GarminConnectConnectionError,)

    def __init__(
        self,
        *,
        email: str | None = None,
        password: str | None = None,
        tokenstore: str | None = None,
        prompt_mfa: Callable[[], str] | None = None,
        max_retries: int = 3,
    ) -> None:
        # Credentials kommen standardmäßig aus der Umgebung — niemals
        # hartkodiert. So landen sie nicht im Code/Repo.
        self._email = email if email is not None else os.getenv("GARMIN_EMAIL")
        self._password = (
            password if password is not None else os.getenv("GARMIN_PASSWORD")
        )

        # Token-Pfad ausrechnen und ~ auflösen.
        raw_path = tokenstore or os.getenv("GARMINTOKENS") or DEFAULT_TOKENSTORE
        self._tokenstore = str(Path(raw_path).expanduser())

        # Im Server-Kontext wird kein interaktiver MFA-Callback übergeben →
        # wir nutzen den, der einen klaren Fehler wirft. login.py übergibt
        # dagegen einen echten input()-Callback.
        self._prompt_mfa = prompt_mfa or _mfa_not_supported

        self._max_retries = max(1, max_retries)

        # Die eigentliche Library-Instanz. None, bis der erste Login lief.
        self._api: Garmin | None = None

        # FastMCP führt synchrone Tool-Funktionen in einem Threadpool aus,
        # d.h. mehrere Tool-Aufrufe können *gleichzeitig* hier ankommen. Ohne
        # Lock könnten zwei Threads parallel einen Login starten. Der Lock
        # stellt sicher, dass genau einmal eingeloggt wird.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Login / Verbindung
    # ------------------------------------------------------------------ #
    def _ensure_login(self) -> Garmin:
        """Sorgt dafür, dass `self._api` eingeloggt ist, und gibt sie zurück.

        Strategie (von der Library umgesetzt, hier nur angestoßen):
          1. Gecachte Tokens aus `self._tokenstore` laden und ggf. erneuern.
          2. Nur falls das fehlschlägt: mit Credentials frisch einloggen.
        Nach erfolgreichem Login wird die Instanz behalten und wiederverwendet.
        """
        # Schneller Pfad ohne Lock: schon eingeloggt.
        if self._api is not None:
            return self._api

        with self._lock:
            # Doppelte Prüfung: Ein anderer Thread könnte den Login gemacht
            # haben, während wir auf den Lock gewartet haben.
            if self._api is not None:
                return self._api

            # Token-Verzeichnis vorbereiten und restriktiv schützen (nur der
            # Besitzer darf lesen/schreiben). Tokens sind so sensibel wie ein
            # Passwort — sie gehören nicht in ein welt-lesbares Verzeichnis.
            self._prepare_tokenstore_dir()

            api = Garmin(
                email=self._email,
                password=self._password,
                prompt_mfa=self._prompt_mfa,
                # garminconnect wiederholt den Login selbst bei Fehlern.
                retry_attempts=3,
            )

            try:
                # login() lädt zuerst gecachte Tokens; nur wenn keine gültigen
                # da sind, werden Credentials gebraucht.
                api.login(self._tokenstore)
            except GarminConnectTooManyRequestsError as exc:
                # 429: Garmin sperrt die IP kurzzeitig. Klarer Hinweis, dass
                # Warten (nicht erneutes Probieren) die Lösung ist.
                raise GarminRateLimited(
                    "Garmin hat die IP vorübergehend gesperrt (HTTP 429). "
                    "Bitte 30–60 Minuten warten und es erneut versuchen — "
                    "schnelle Wiederholungen verlängern die Sperre. Ein "
                    "Netzwechsel (z.B. Handy-Hotspot) gibt eine neue IP."
                ) from exc
            except GarminConnectAuthenticationError as exc:
                # Häufigster Fall: keine/abgelaufene Tokens UND fehlende oder
                # falsche Credentials. Wir übersetzen das in eine klare,
                # handlungsorientierte Meldung — ohne je das Passwort zu loggen.
                raise GarminAuthRequired(
                    "Login bei Garmin Connect fehlgeschlagen. Prüfe, ob "
                    "GARMIN_EMAIL/GARMIN_PASSWORD korrekt gesetzt sind, und "
                    "führe bei aktivem 2FA einmalig 'python3 login.py' aus."
                ) from exc

            logger.info("Erfolgreich bei Garmin Connect eingeloggt.")
            self._api = api
            return api

    def _prepare_tokenstore_dir(self) -> None:
        """Legt das Token-Verzeichnis an und setzt sichere Rechte (0700)."""
        token_dir = Path(self._tokenstore)
        # mode=0o700 legt das Verzeichnis von Anfang an eng an — so gibt es
        # kein Fenster, in dem die Tokens welt-lesbar wären. (Hinweis: mkdir
        # wendet die umask auf mode an; deshalb unten zusätzlich chmod.)
        token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Gürtel-und-Hosenträger: erzwingt 0700 auch bei bereits existierendem
        # Verzeichnis bzw. wenn die umask die mkdir-Rechte aufgeweicht hat.
        # 0o700 = nur der Besitzer darf rein.
        try:
            os.chmod(token_dir, 0o700)
        except OSError as exc:  # pragma: no cover - plattformabhängig
            logger.warning("Konnte Rechte auf %s nicht setzen: %s", token_dir, exc)

    # ------------------------------------------------------------------ #
    # Generischer Aufruf mit Retry
    # ------------------------------------------------------------------ #
    def _call(self, method_name: str, *args: Any) -> Any:
        """Ruft eine garminconnect-Methode auf und wiederholt bei Bedarf.

        Wir wiederholen nur bei *vorübergehenden* Fehlern (Rate-Limit,
        Verbindungsproblem) mit exponentiell wachsender Wartezeit. Echte
        Fehler (z.B. Auth) werden sofort nach oben gereicht.
        """
        api = self._ensure_login()
        method = getattr(api, method_name)

        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return method(*args)
            except GarminConnectTooManyRequestsError as exc:
                # 429 mitten im Betrieb: gleiche klare Botschaft wie beim Login.
                # Kein Retry — das würde die Sperre nur verlängern.
                raise GarminRateLimited(
                    "Garmin hat die IP vorübergehend gesperrt (HTTP 429). "
                    "Bitte 30–60 Minuten warten und es erneut versuchen — "
                    "schnelle Wiederholungen verlängern die Sperre. Ein "
                    "Netzwechsel (z.B. Handy-Hotspot) gibt eine neue IP."
                ) from exc
            except self._RETRYABLE as exc:
                last_exc = exc
                if attempt == self._max_retries:
                    break
                # 1s, 2s, 4s, ... — gibt Garmin Zeit, sich zu erholen.
                wait = 2 ** (attempt - 1)
                logger.warning(
                    "Garmin-Aufruf %s fehlgeschlagen (Versuch %d/%d): %s — "
                    "neuer Versuch in %ds",
                    method_name, attempt, self._max_retries, exc, wait,
                )
                time.sleep(wait)

        # Alle Versuche aufgebraucht.
        raise GarminClientError(
            f"Garmin-Aufruf '{method_name}' nach {self._max_retries} Versuchen "
            f"fehlgeschlagen: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------ #
    # Öffentliche Datenmethoden — bewusst dünn, eine pro MCP-Tool.
    # Sie geben die rohen Garmin-Strukturen zurück; das Formatieren in
    # kompakte, sprechende Felder passiert in server.py.
    # ------------------------------------------------------------------ #
    def get_activities(self, start: int, limit: int) -> Any:
        return self._call("get_activities", start, limit)

    def get_activities_by_date(self, start_date: str, end_date: str) -> Any:
        # Holt ALLE Aktivitäten in einem Datumsbereich (kein 50er-Limit).
        # Genau richtig für exakte Wochen-/Zeitraum-Auswertungen.
        return self._call("get_activities_by_date", start_date, end_date)

    def get_activity_details(self, activity_id: str | int) -> Any:
        return self._call("get_activity_details", activity_id)

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

    def get_steps_data(self, date: str) -> Any:
        return self._call("get_steps_data", date)

    def get_heart_rates(self, date: str) -> Any:
        return self._call("get_heart_rates", date)
