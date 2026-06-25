"""login.py — einmaliger, interaktiver Login bei Garmin Connect.

Wozu eine separate Datei?
    Der MCP-Server (server.py) läuft "headless": Er redet über stdin/stdout
    mit Claude Desktop und kann dich NICHT nach einem 2FA-Code fragen. Dieses
    Skript ist die Stelle, an der du dich *einmal* im Terminal einloggst.
    Garmin liefert dabei OAuth-Tokens zurück, die lokal (Standard:
    ~/.garminconnect) gespeichert werden. Danach nutzt der Server nur noch
    diese Tokens und erneuert sie automatisch — du musst dich erst wieder
    einloggen, wenn die Tokens irgendwann ablaufen (typisch nach ~1 Jahr).

Aufruf:
    export GARMIN_EMAIL="deine@email.com"
    export GARMIN_PASSWORD="deinpasswort"
    python3 login.py

Falls 2FA aktiv ist, wirst du nach dem Code aus deiner Authenticator-App /
SMS gefragt.
"""

from __future__ import annotations

import sys

from garmin_client import GarminAuthRequired, GarminClient, GarminClientError


def _prompt_mfa() -> str:
    """Fragt den 2FA-Code interaktiv ab.

    Anders als im Server haben wir hier ein echtes Terminal, also dürfen wir
    input() benutzen. .strip() entfernt versehentliche Leerzeichen.

    Wichtig: Die Library ruft diesen Callback manchmal auch dann auf, wenn der
    eigentliche Grund ein IP-Rate-Limit (429) war — nicht echtes 2FA. Wer kein
    2FA hat, drückt dann einfach Enter; wir brechen mit klarer Erklärung ab,
    statt einen leeren Code an Garmin zu schicken.
    """
    code = input(
        "2FA-Code eingeben (oder Enter, falls du KEIN 2FA hast): "
    ).strip()
    if not code:
        raise GarminAuthRequired(
            "Kein 2FA-Code eingegeben. Wenn dein Account kein 2FA hat, lag das "
            "vorherige Problem fast sicher an einem IP-Rate-Limit (429). Bitte "
            "30–60 Minuten warten und 'python3 login.py' erneut ausführen; "
            "ein Netzwechsel (Handy-Hotspot) hilft oft sofort."
        )
    return code


def main() -> int:
    print("Garmin Connect — einmaliger Login\n")

    # Wir übergeben den interaktiven MFA-Callback. Credentials kommen wie
    # üblich aus der Umgebung (GARMIN_EMAIL / GARMIN_PASSWORD).
    client = GarminClient(prompt_mfa=_prompt_mfa)

    try:
        # Ein beliebiger Datenabruf erzwingt den Login (lazy). Wir nehmen einen
        # kleinen: die letzte Aktivität bzw. eine einzige Aktivität.
        client.get_activities(0, 1)
    except GarminClientError as exc:
        print(f"\n❌ Login fehlgeschlagen: {exc}", file=sys.stderr)
        print(
            "\nTipps:\n"
            "  • Sind GARMIN_EMAIL und GARMIN_PASSWORD gesetzt und korrekt?\n"
            "  • Bei 2FA: stimmt der eingegebene Code (er ist nur kurz gültig)?\n",
            file=sys.stderr,
        )
        return 1

    print(
        "\n✅ Login erfolgreich! Tokens wurden lokal gespeichert.\n"
        "   Der MCP-Server kann sich ab jetzt ohne erneuten Login verbinden."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
