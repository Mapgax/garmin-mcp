# Garmin MCP Server

Ein **lokaler** MCP-Server (Model Context Protocol), der deine Garmin-Connect-Daten
direkt in Claude Desktop verfügbar macht. Keine Cloud, kein Drittanbieter — deine
Credentials und Daten verlassen den Mac nicht.

## Wie es funktioniert

```
Garmin Watch → Garmin Connect (Cloud) → [dieser MCP-Server, lokal] → Claude Desktop
                         garminconnect-Library         JSON-RPC über stdio
```

Claude Desktop startet `server.py` als Subprozess. Der Server loggt sich mit
gecachten Tokens bei Garmin ein und stellt 12 Tools bereit (Aktivitäten, Schlaf,
HRV, Stress, Body Battery, Trainingsstatus, VO2max, Readiness, Wochen- und
Tagesübersichten, aktueller Puls).

## Dateien

| Datei | Zweck |
|---|---|
| `server.py` | MCP-Server: definiert die Tools und formatiert die Antworten |
| `garmin_client.py` | Wrapper um `garminconnect`: Login, Tokens, Retry |
| `login.py` | Einmaliger interaktiver Login (für 2FA) |
| `requirements.txt` | Abhängigkeiten |

## Setup

### 1. Abhängigkeiten installieren
```bash
cd ~/Projects/Personal/garmin_mcp
pip3 install -r requirements.txt --break-system-packages
```

### 2. Credentials als Umgebungsvariablen setzen
```bash
export GARMIN_EMAIL="deine@email.com"
export GARMIN_PASSWORD="deinpasswort"
```

### 3. Einmalig einloggen (cached die Tokens, behandelt 2FA)
```bash
python3 login.py
```
Bei aktivem 2FA wirst du nach dem Code gefragt. Danach liegen die Tokens in
`~/.garminconnect` (Rechte 0700) und werden automatisch erneuert.

### 4. Claude Desktop konfigurieren
In `~/Library/Application Support/Claude/claude_desktop_config.json` (im
`mcpServers`-Block):
```json
{
  "mcpServers": {
    "garmin": {
      "command": "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
      "args": ["/Users/andreas/Projects/Personal/garmin_mcp/server.py"]
    }
  }
}
```
Hinweise:
- **Absoluter** `python3`-Pfad (der, in dem `garminconnect`+`mcp` installiert sind) —
  im Subprozess ist `PATH` oft anders als im Terminal. Eigenen Pfad ermitteln mit
  `which python3`.
- **Kein `env` mit Credentials nötig**, solange der Token-Cache aus Schritt 3
  (`~/.garminconnect`) existiert. Wer Auto-Recovery bei abgelaufenen Tokens will,
  kann optional `"env": {"GARMIN_EMAIL": "...", "GARMIN_PASSWORD": "..."}` ergänzen —
  dann steht das Passwort allerdings im Klartext in der Config.

### 5. Claude Desktop neu starten
Der `garmin`-MCP erscheint dann in den Tools. Frag z.B.:
- *„Wie war mein letzter Lauf?"*
- *„Wie ist mein HRV-Trend der letzten 2 Wochen?"*
- *„Bin ich heute bereit für ein hartes Training?"*
- *„Zeig mir meine Wochenkilometer der letzten 4 Wochen"*

## Sicherheit & Datenschutz

- Credentials kommen **nur** aus Umgebungsvariablen, nie aus dem Code.
- OAuth-Tokens werden lokal in `~/.garminconnect` (Rechte 0700) gespeichert.
- Es werden **keine** Daten an externe Dienste geschickt — nur an Garmin
  Connect selbst (deine eigene Datenquelle).
- Passwörter/Tokens werden nie geloggt.

## Konfiguration (optional)

| Umgebungsvariable | Bedeutung | Standard |
|---|---|---|
| `GARMIN_EMAIL` | Garmin-Login-E-Mail | – (Pflicht) |
| `GARMIN_PASSWORD` | Garmin-Passwort | – (Pflicht) |
| `GARMINTOKENS` | Pfad für den Token-Cache | `~/.garminconnect` |

## Hinweise

- `garminconnect` ist eine **inoffizielle** Library. Garmin kann die interne
  API jederzeit ändern. Bei Problemen: `pip3 install -U garminconnect`.
- Garmin hat Rate-Limits. Der Server wiederholt vorübergehende Fehler
  automatisch mit wachsender Wartezeit.
