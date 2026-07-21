"""server.py — der eigentliche MCP-Server für Garmin Connect.

Was ist hier die Idee?
    Wir nutzen **FastMCP** aus dem offiziellen MCP-SDK. FastMCP übernimmt den
    ganzen JSON-RPC-/stdio-Boilerplate: Wir schreiben einfach Python-Funktionen
    und dekorieren sie mit `@mcp.tool()`. FastMCP liest dann automatisch die
    Funktionssignatur + Docstring aus und macht daraus ein Tool, das Claude
    Desktop aufrufen kann.

Aufgabenteilung:
    • garmin_client.py  → Login, Tokens, Retry, rohe Datenabrufe.
    • server.py (hier)  → Tools definieren und Rohdaten in kompakte,
                          sprechende Felder formatieren (token-sparend).

Starten tut diesen Server normalerweise Claude Desktop automatisch (als
Subprozess). Zum Testen kann man ihn auch direkt starten: `python3 server.py`.
"""

from __future__ import annotations

import functools
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from garmin_client import GarminClient, GarminClientError

# ---------------------------------------------------------------------------
# Logging: ALLES nach stderr.
#
# Warum so wichtig? stdout ist beim MCP-stdio-Transport ausschließlich für die
# JSON-RPC-Nachrichten reserviert. Ein versehentliches print() oder ein Log auf
# stdout würde den Datenstrom zerstören und Claude Desktop verwirren. Deshalb
# leiten wir den Logger explizit auf stderr.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("garmin_mcp.server")

# Der MCP-Server. Der Name taucht u.a. in Claude Desktop auf.
mcp = FastMCP("garmin")

# Genau EIN Client für den ganzen Prozess (Login wird so nur einmal gemacht).
client = GarminClient()


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def _today() -> str:
    """Heutiges Datum als 'YYYY-MM-DD' (lokale Zeit des Mac)."""
    return date.today().isoformat()


def _normalize_date(value: str | None) -> str:
    """Validiert ein Datum und gibt es als 'YYYY-MM-DD' zurück.

    None/leer  → heute. Ungültiges Format → klarer ValueError, damit der
    Nutzer nicht später einen kryptischen Garmin-Fehler bekommt.
    """
    if not value:
        return _today()
    try:
        # strptime wirft ValueError bei falschem Format — genau das wollen wir.
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(
            f"Ungültiges Datum '{value}'. Erwartet wird das Format "
            f"YYYY-MM-DD, z.B. {_today()}."
        ) from exc


def safe_tool(func: Callable[..., Any]) -> Callable[..., Any]:
    """Dekorator: fängt Fehler ab und gibt sie als sauberes Ergebnis zurück.

    Ohne das würde eine geworfene Exception als MCP-Protokollfehler bei Claude
    landen. Mit diesem Wrapper bekommt Claude stattdessen ein {"error": ...},
    kann das dem Nutzer erklären und ggf. etwas anderes versuchen. Genau das
    meint "graceful degradation".

    `functools.wraps` ist wichtig: FastMCP liest Name, Signatur und Docstring
    der Funktion aus, um das Tool-Schema zu bauen — die müssen erhalten bleiben.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (GarminClientError, ValueError) as exc:
            # Erwartete Fehler (Login fehlt, Rate-Limit, falsches Datum): kurz.
            logger.warning("%s: %s", func.__name__, exc)
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - bewusster Auffang ganz außen
            # Unerwartete Fehler: trotzdem nicht den Server abschießen.
            logger.exception("Unerwarteter Fehler in %s", func.__name__)
            return {"error": f"Unerwarteter Fehler: {exc}"}

    return wrapper


def _g(obj: Any, *keys: str, default: Any = None) -> Any:
    """Holt verschachtelte dict-Werte sicher: _g(d, 'a', 'b') == d['a']['b'].

    Gibt `default` zurück, sobald irgendein Zwischenschritt fehlt oder kein
    dict ist. So bleiben die Formatierer robust, falls Garmin mal ein Feld
    weglässt oder umbenennt.
    """
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _round(value: Any, digits: int = 1) -> Any:
    """Rundet Zahlen hübsch; lässt Nicht-Zahlen unverändert durch."""
    if isinstance(value, (int, float)):
        return round(value, digits)
    return value


def _meters_to_km(meters: Any) -> Any:
    if isinstance(meters, (int, float)):
        return round(meters / 1000.0, 2)
    return None


def _seconds_to_hms(seconds: Any) -> str | None:
    """Sekunden → 'H:MM:SS' (oder 'M:SS' bei <1h). Gibt None bei Unsinn."""
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _pace_per_km(distance_m: Any, duration_s: Any) -> str | None:
    """Berechnet Pace als 'M:SS /km' aus Distanz (m) und Dauer (s)."""
    if not isinstance(distance_m, (int, float)) or distance_m <= 0:
        return None
    if not isinstance(duration_s, (int, float)) or duration_s <= 0:
        return None
    sec_per_km = duration_s / (distance_m / 1000.0)
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d} /km"


def _format_activity(act: dict) -> dict:
    """Eine Aktivität auf die für Coaching relevanten Felder eindampfen."""
    distance_m = act.get("distance")
    duration_s = act.get("duration")
    return {
        "activity_id": act.get("activityId"),
        "name": act.get("activityName"),
        "type": _g(act, "activityType", "typeKey"),
        "start": act.get("startTimeLocal"),
        "distance_km": _meters_to_km(distance_m),
        "duration": _seconds_to_hms(duration_s),
        "pace": _pace_per_km(distance_m, duration_s),
        "avg_hr": act.get("averageHR"),
        "max_hr": act.get("maxHR"),
        "elevation_gain_m": _round(act.get("elevationGain")),
        "calories": act.get("calories"),
    }


# ---------------------------------------------------------------------------
# MCP-Tools — Aktivitäten
# ---------------------------------------------------------------------------
@mcp.tool()
@safe_tool
def get_recent_activities(limit: int = 10) -> dict:
    """Gibt die letzten N Aktivitäten zurück (Typ, Distanz, Pace, HR, Höhe).

    Args:
        limit: Wie viele Aktivitäten (1–50). Standard 10.
    """
    # Eingaben begrenzen: schützt vor versehentlich riesigen Abfragen.
    limit = max(1, min(int(limit), 50))
    raw = client.get_activities(0, limit)
    activities = raw if isinstance(raw, list) else []
    return {
        "count": len(activities),
        "activities": [_format_activity(a) for a in activities],
    }


@mcp.tool()
@safe_tool
def get_activity_detail(activity_id: str) -> dict:
    """Details zu einer Aktivität (per activity_id aus get_recent_activities).

    Args:
        activity_id: Die ID der Aktivität, z.B. '1234567890'.
    """
    raw = client.get_activity_details(activity_id)
    if not isinstance(raw, dict):
        return {"error": "Keine Detaildaten erhalten."}

    # get_activity_details liefert eine sehr große Struktur. Wir picken die
    # nützliche Zusammenfassung (summaryDTO) heraus statt alles durchzureichen.
    # `or {}` statt Default-Argument: Garmin kann "summaryDTO": null liefern,
    # und .get(key, default) greift dann NICHT — summary.get(...) würde crashen.
    summary = raw.get("summaryDTO") or {}
    distance_m = summary.get("distance")
    duration_s = summary.get("duration")
    return {
        "activity_id": _g(raw, "activityId") or activity_id,
        "name": _g(raw, "activityName"),
        "type": _g(raw, "activityTypeDTO", "typeKey"),
        "start": summary.get("startTimeLocal"),
        "distance_km": _meters_to_km(distance_m),
        "duration": _seconds_to_hms(duration_s),
        "moving_duration": _seconds_to_hms(summary.get("movingDuration")),
        "pace": _pace_per_km(distance_m, duration_s),
        "avg_hr": summary.get("averageHR"),
        "max_hr": summary.get("maxHR"),
        "avg_speed_mps": _round(summary.get("averageSpeed"), 2),
        "elevation_gain_m": _round(summary.get("elevationGain")),
        "elevation_loss_m": _round(summary.get("elevationLoss")),
        "calories": summary.get("calories"),
        "avg_power": summary.get("averagePower"),
        "training_effect_aerobic": summary.get("trainingEffect"),
        "training_effect_anaerobic": summary.get("anaerobicTrainingEffect"),
    }


# ---------------------------------------------------------------------------
# MCP-Tools — Erholung & Wellness
# ---------------------------------------------------------------------------
@mcp.tool()
@safe_tool
def get_sleep_data(date: str | None = None) -> dict:
    """Schlafphasen und Schlafqualität für einen Tag.

    Args:
        date: 'YYYY-MM-DD'. Standard: heute.
    """
    day = _normalize_date(date)
    raw = client.get_sleep_data(day)
    dto = _g(raw, "dailySleepDTO", default={}) if isinstance(raw, dict) else {}
    return {
        "date": day,
        "total_sleep": _seconds_to_hms(dto.get("sleepTimeSeconds")),
        "deep": _seconds_to_hms(dto.get("deepSleepSeconds")),
        "light": _seconds_to_hms(dto.get("lightSleepSeconds")),
        "rem": _seconds_to_hms(dto.get("remSleepSeconds")),
        "awake": _seconds_to_hms(dto.get("awakeSleepSeconds")),
        "sleep_score": _g(dto, "sleepScores", "overall", "value"),
        "sleep_quality": _g(dto, "sleepScores", "overall", "qualifierKey"),
        "resting_hr": raw.get("restingHeartRate") if isinstance(raw, dict) else None,
        # HRV wird bewusst NICHT hier ausgegeben — die Schlafantwort enthält
        # keinen verlässlichen HRV-Wert. Dafür gibt es get_hrv_data.
    }


@mcp.tool()
@safe_tool
def get_hrv_data(date: str | None = None) -> dict:
    """HRV-Werte (Herzratenvariabilität) für einen Tag.

    Args:
        date: 'YYYY-MM-DD'. Standard: heute.
    """
    day = _normalize_date(date)
    raw = client.get_hrv_data(day)
    summary = _g(raw, "hrvSummary", default={}) if isinstance(raw, dict) else {}
    return {
        "date": day,
        "last_night_avg": summary.get("lastNightAvg"),
        "last_night_5min_high": summary.get("lastNight5MinHigh"),
        "status": summary.get("status"),
        "baseline_low": _g(summary, "baseline", "lowUpper"),
        "baseline_balanced_low": _g(summary, "baseline", "balancedLow"),
        "baseline_balanced_high": _g(summary, "baseline", "balancedUpper"),
    }


@mcp.tool()
@safe_tool
def get_body_battery(date: str | None = None) -> dict:
    """Body-Battery-Verlauf (Energiereserven) für einen Tag.

    Args:
        date: 'YYYY-MM-DD'. Standard: heute.
    """
    day = _normalize_date(date)
    # Diese API erwartet einen Zeitraum; wir fragen genau einen Tag ab.
    raw = client.get_body_battery(day, day)
    # Antwort ist eine Liste (ein Eintrag pro Tag). Wir nehmen den ersten.
    entry = raw[0] if isinstance(raw, list) and raw else {}
    values = entry.get("bodyBatteryValuesArray") or []
    # Aktueller Stand = letzter Messwert. Format je Punkt: [timestamp, level].
    current = None
    if values and isinstance(values[-1], (list, tuple)) and len(values[-1]) >= 2:
        current = values[-1][1]
    return {
        "date": day,
        "current_level": current,
        "charged": entry.get("charged"),
        "drained": entry.get("drained"),
        "measurements": len(values),
    }


@mcp.tool()
@safe_tool
def get_stress_data(date: str | None = None) -> dict:
    """Stresswerte für einen Tag (Durchschnitt, Maximum).

    Args:
        date: 'YYYY-MM-DD'. Standard: heute.
    """
    day = _normalize_date(date)
    raw = client.get_stress_data(day)
    if not isinstance(raw, dict):
        return {"date": day, "error": "Keine Stressdaten erhalten."}
    # Hinweis: get_stress_data liefert nur Durchschnitt/Maximum plus den rohen
    # Verlauf (stressValuesArray) — KEINE aggregierten Zonendauern. Wir geben
    # daher nur die tatsächlich vorhandenen Kennzahlen zurück.
    return {
        "date": day,
        "avg_stress": raw.get("avgStressLevel"),
        "max_stress": raw.get("maxStressLevel"),
    }


# ---------------------------------------------------------------------------
# MCP-Tools — Training Performance
# ---------------------------------------------------------------------------
@mcp.tool()
@safe_tool
def get_training_status(date: str | None = None) -> dict:
    """Aktueller Trainingsstatus und Trainingsbelastung (Load).

    Args:
        date: 'YYYY-MM-DD'. Standard: heute.
    """
    day = _normalize_date(date)
    raw = client.get_training_status(day)
    if not isinstance(raw, dict):
        return {"date": day, "error": "Keine Trainingsstatus-Daten erhalten."}

    # Die Struktur ist nach Geräten geschachtelt. Wir greifen den jüngsten
    # Eintrag ab; fehlt er, geben wir wenigstens die obersten Felder zurück.
    most_recent = raw.get("mostRecentTrainingStatus") or {}
    latest_map = most_recent.get("latestTrainingStatusData") or {}
    # latestTrainingStatusData ist ein dict {deviceId: {...}} — ersten Wert nehmen.
    device_data = next(iter(latest_map.values()), {}) if isinstance(latest_map, dict) else {}

    # Echte Trainingslast steckt im acuteTrainingLoadDTO (acute = letzte ~7
    # Tage, chronic = letzte ~4 Wochen, ACWR = Verhältnis der beiden).
    acute = device_data.get("acuteTrainingLoadDTO") or {}

    return {
        "date": day,
        # Lesbare Phrase bevorzugen (z.B. 'PRODUCTIVE_2'); der nackte Zahlencode
        # ist nur Fallback.
        "training_status": device_data.get("trainingStatusFeedbackPhrase")
        or device_data.get("trainingStatus"),
        "sport": device_data.get("sport"),
        "acute_load": acute.get("dailyTrainingLoadAcute"),
        "chronic_load": acute.get("dailyTrainingLoadChronic"),
        "acwr_ratio": _round(acute.get("dailyAcuteChronicWorkloadRatio"), 2),
        "acwr_status": acute.get("acwrStatus"),
        "load_tunnel_min": _round(acute.get("minTrainingLoadChronic"), 0),
        "load_tunnel_max": _round(acute.get("maxTrainingLoadChronic"), 0),
        "fitness_trend": device_data.get("fitnessTrend"),
    }


@mcp.tool()
@safe_tool
def get_vo2max(date: str | None = None) -> dict:
    """VO2-Max-Schätzung (aerobe Fitness) — letzter bekannter Wert ab einem Datum.

    Garmin aktualisiert VO2max nur nach passenden Aktivitäten. Dieser Tool sucht
    daher rückwärts bis zu 7 Tage und gibt den jüngsten vorhandenen Wert zurück
    (plus das echte Datum, an dem er gemessen wurde). Worst case: 8 API-Calls.

    Args:
        date: Startdatum der Rückwärtssuche als 'YYYY-MM-DD'. Standard: heute.
    """
    day = _normalize_date(date)
    # Garmin aktualisiert VO2max nur an Tagen mit passenden Aktivitäten; an
    # anderen Tagen ist die Antwort leer. Damit "Wie ist mein VO2max?" trotzdem
    # eine Zahl liefert, suchen wir bis zu 7 Tage rückwärts nach dem jüngsten
    # vorhandenen Wert und geben dessen echtes Datum mit zurück.
    # Kosten-Hinweis: das sind im schlechtesten Fall 8 sequentielle API-Calls.
    # Akzeptabel, weil gedeckelt und VO2max selten abgefragt wird; sobald ein
    # Wert gefunden ist, brechen wir sofort ab (meist nach 1–3 Calls).
    target = datetime.strptime(day, "%Y-%m-%d").date()
    for back in range(0, 8):
        probe = (target - timedelta(days=back)).isoformat()
        raw = client.get_max_metrics(probe)
        entry = raw[0] if isinstance(raw, list) and raw else None
        if entry and _g(entry, "generic", "vo2MaxValue") is not None:
            return {
                "date": probe,
                "requested_date": day,
                "vo2max_running": _g(entry, "generic", "vo2MaxValue"),
                "vo2max_cycling": _g(entry, "cycling", "vo2MaxValue"),
                "fitness_age": _g(entry, "generic", "fitnessAge"),
            }

    return {
        "date": day,
        "vo2max_running": None,
        "vo2max_cycling": None,
        "note": "Kein VO2max-Wert in den letzten 7 Tagen gefunden.",
    }


@mcp.tool()
@safe_tool
def get_training_readiness(date: str | None = None) -> dict:
    """Training-Readiness-Score (bist du heute bereit für ein hartes Training?).

    Args:
        date: 'YYYY-MM-DD'. Standard: heute.
    """
    day = _normalize_date(date)
    raw = client.get_training_readiness(day)
    entry = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else {})
    return {
        "date": day,
        "score": entry.get("score"),
        "level": entry.get("level"),
        "feedback": entry.get("feedbackLong") or entry.get("feedbackShort"),
        "sleep_score": entry.get("sleepScore"),
        "hrv_factor": entry.get("hrvFactorPercent"),
        "recovery_time_hours": entry.get("recoveryTime"),
        "acute_load": entry.get("acuteLoad"),
    }


# %LTHR-Schema (Garmins LTHR-verankerte Standardzonen): Anteil der
# Laktatschwellen-HF, bei dem die jeweilige Zone BEGINNT. Bewusst hier
# dupliziert statt aus sport_trainer_bot importiert — die beiden Repos sind
# absichtlich unabhängig (app/hr_zones.py dort nutzt dieselben Werte).
_LTHR_ZONE_STARTS = {2: 0.80, 3: 0.89, 4: 0.94, 5: 0.99}


def _suggest_zones_from_lthr(lthr: int) -> dict[str, str]:
    """Fünf Zonen aus der Laktatschwellen-HF, fertig formatiert in bpm."""
    s = {z: round(lthr * f) for z, f in _LTHR_ZONE_STARTS.items()}
    return {
        "zone1": f"<{s[2]}",
        "zone2": f"{s[2]}–{s[3]}",
        "zone3": f"{s[3]}–{s[4]}",
        "zone4": f"{s[4]}–{s[5]}",
        "zone5": f"≥{s[5]}",
    }


@mcp.tool()
@safe_tool
def get_lactate_threshold() -> dict:
    """Laktatschwelle (Laufen): von der Uhr erkannte Schwellen-HF und -Pace,
    plus daraus abgeleiteter Zonenvorschlag (%LTHR-Schema).

    Hinweis: Zonen lassen sich per API nicht in Garmin schreiben — die
    vorgeschlagenen Grenzen müssen manuell in Garmin Connect eingetragen
    werden (Einstellungen → Nutzerprofil → Herzfrequenzbereiche → Laufen).
    """
    raw = client.get_lactate_threshold()
    lt = _g(raw, "speed_and_heart_rate", default={}) or {}
    lthr = lt.get("heartRate")
    speed = lt.get("speed")
    power = _g(raw, "power", default={}) or {}

    if lthr is None:
        return {
            "lthr_bpm": None,
            "note": "Garmin hat noch keine Laktatschwelle erkannt. Dafür braucht "
                    "es Läufe mit höherer Intensität und gutem HF-Signal "
                    "(idealerweise Brustgurt).",
        }

    # Schwellen-Pace aus m/s: Sekunden pro km → 'M:SS /km' (wie _pace_per_km).
    pace = _pace_per_km(1000.0, 1000.0 / speed) if isinstance(speed, (int, float)) and speed > 0 else None
    return {
        "lthr_bpm": lthr,
        "threshold_pace": pace,
        "detected_on": lt.get("calendarDate"),
        "ftp_watts": power.get("functionalThresholdPower"),
        "suggested_zones_pct_lthr": _suggest_zones_from_lthr(lthr),
        "transfer": "Nur manuell übertragbar: Garmin Connect → Einstellungen → "
                    "Nutzerprofil → Herzfrequenz- und Leistungsbereiche → Laufen.",
    }


@mcp.tool()
@safe_tool
def get_hr_zones(activity_id: str | None = None) -> dict:
    """Konfigurierte HF-Zonen + Zeit-in-Zonen einer Aktivität.

    Die Zonengrenzen in der Antwort sind die aktuell im Garmin-Konto
    konfigurierten (die Zeit-in-Zonen-Daten einer Aktivität sind der einzige
    Weg, sie per API auszulesen).

    Args:
        activity_id: Aktivitäts-ID (aus get_recent_activities). Ohne Angabe
            wird die jüngste Lauf-Aktivität verwendet.
    """
    if not activity_id:
        raw_acts = client.get_activities(0, 20)
        acts = raw_acts if isinstance(raw_acts, list) else []
        run = next(
            (a for a in acts
             if "running" in str(_g(a, "activityType", "typeKey") or "")),
            None,
        ) or (acts[0] if acts else None)
        if not run:
            return {"error": "Keine Aktivität gefunden, aus der sich Zonen lesen ließen."}
        activity_id = run.get("activityId")

    raw = client.get_activity_hr_in_timezones(activity_id)
    entries = raw if isinstance(raw, list) else _g(raw, "zones", default=[]) or []
    zones = []
    for entry in sorted(
        (e for e in entries if isinstance(e, dict)),
        key=lambda e: e.get("zoneNumber") or 0,
    ):
        secs = entry.get("secsInZone")
        zones.append({
            "zone": entry.get("zoneNumber"),
            "low_boundary_bpm": entry.get("zoneLowBoundary"),
            "time_in_zone": _seconds_to_hms(secs),
        })
    if not zones:
        return {"activity_id": activity_id,
                "error": "Keine Zonendaten für diese Aktivität erhalten."}
    return {"activity_id": activity_id, "configured_zones": zones}


# ---------------------------------------------------------------------------
# MCP-Tools — Zusammenfassungen
# ---------------------------------------------------------------------------
@mcp.tool()
@safe_tool
def get_weekly_summary(week_offset: int = 0) -> dict:
    """Wochenzusammenfassung aus den Aktivitäten (km, Zeit, Höhenmeter).

    Args:
        week_offset: 0 = aktuelle Woche, 1 = letzte Woche, usw. Standard 0.
    """
    week_offset = max(0, int(week_offset))
    today = date.today()
    # Montag dieser (bzw. der versetzten) Woche. weekday(): Mo=0 ... So=6.
    monday = today - timedelta(days=today.weekday()) - timedelta(weeks=week_offset)
    sunday = monday + timedelta(days=6)

    # Exakt die Aktivitäten dieser Woche per Datumsbereich holen — kein
    # 50er-Limit, keine clientseitige Filterung, keine stille Truncation bei
    # weit zurückliegenden Wochen oder hohem Trainingsvolumen.
    raw = client.get_activities_by_date(monday.isoformat(), sunday.isoformat())
    activities = raw if isinstance(raw, list) else []

    total_distance_m = 0.0
    total_duration_s = 0.0
    total_elevation_m = 0.0
    included: list[dict] = []

    for act in activities:
        total_distance_m += act.get("distance") or 0
        total_duration_s += act.get("duration") or 0
        total_elevation_m += act.get("elevationGain") or 0
        included.append(_format_activity(act))

    return {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "activity_count": len(included),
        "total_distance_km": round(total_distance_m / 1000.0, 2),
        "total_duration": _seconds_to_hms(total_duration_s),
        "total_elevation_gain_m": round(total_elevation_m),
        "activities": included,
    }


@mcp.tool()
@safe_tool
def get_health_snapshot(date: str | None = None) -> dict:
    """Tages-Snapshot: Schritte, Kalorien, HR, Stress, Schlaf in einem Aufruf.

    Args:
        date: 'YYYY-MM-DD'. Standard: heute.
    """
    day = _normalize_date(date)
    summary = client.get_user_summary(day)
    if not isinstance(summary, dict):
        summary = {}
    return {
        "date": day,
        "steps": summary.get("totalSteps"),
        "step_goal": summary.get("dailyStepGoal"),
        "distance_km": _meters_to_km(summary.get("totalDistanceMeters")),
        "calories_total": summary.get("totalKilocalories"),
        "calories_active": summary.get("activeKilocalories"),
        "floors_climbed": summary.get("floorsAscended"),
        "resting_hr": summary.get("restingHeartRate"),
        "min_hr": summary.get("minHeartRate"),
        "max_hr": summary.get("maxHeartRate"),
        "avg_stress": summary.get("averageStressLevel"),
        "body_battery_high": summary.get("bodyBatteryHighestValue"),
        "body_battery_low": summary.get("bodyBatteryLowestValue"),
        "intensity_minutes_moderate": summary.get("moderateIntensityMinutes"),
        "intensity_minutes_vigorous": summary.get("vigorousIntensityMinutes"),
    }


@mcp.tool()
@safe_tool
def get_current_heart_rate(date: str | None = None) -> dict:
    """Aktueller/letzter gemessener Puls + Ruhepuls-Kennzahlen für den Tag.

    Args:
        date: 'YYYY-MM-DD'. Standard: heute.
    """
    day = _normalize_date(date)
    raw = client.get_heart_rates(day)
    if not isinstance(raw, dict):
        return {"date": day, "error": "Keine Herzfrequenzdaten erhalten."}

    # heartRateValues: Liste von [timestamp_ms, bpm]. Der letzte gültige Wert
    # ist der "aktuellste" Puls des Tages.
    values = raw.get("heartRateValues") or []
    latest_bpm = None
    latest_ts = None
    for point in reversed(values):
        if isinstance(point, (list, tuple)) and len(point) >= 2 and point[1] is not None:
            latest_ts, latest_bpm = point[0], point[1]
            break

    latest_time = None
    if isinstance(latest_ts, (int, float)):
        # Garmin liefert Millisekunden seit Epoch.
        latest_time = datetime.fromtimestamp(latest_ts / 1000).isoformat(timespec="minutes")

    return {
        "date": day,
        "current_hr": latest_bpm,
        "current_hr_time": latest_time,
        "resting_hr": raw.get("restingHeartRate"),
        "min_hr": raw.get("minHeartRate"),
        "max_hr": raw.get("maxHeartRate"),
        "last_7_days_avg_resting_hr": raw.get("lastSevenDaysAvgRestingHeartRate"),
    }


if __name__ == "__main__":
    # Startet den MCP-Server über stdio (Standard für Claude Desktop).
    logger.info("Starte Garmin MCP-Server (stdio) ...")
    mcp.run()
