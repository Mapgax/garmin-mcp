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
gecachten Tokens bei Garmin ein und stellt 15 Tools bereit (Aktivitäten, Schlaf,
HRV, Stress, Body Battery, Trainingsstatus, VO2max, Readiness, Laktatschwelle,
HF-Zonen, Wochen- und Tagesübersichten, aktueller Puls, Login-Status).

## Dateien

| Datei | Zweck |
|---|---|
| `server.py` | MCP-Server: definiert die Tools und formatiert die Antworten |
| `garmin_client.py` | Wrapper um `garminconnect`: Login, Tokens, Selbstheilung, Datei-Lock |
| `login.py` | Einmaliger interaktiver Login (für 2FA) |
| `requirements.txt` | Abhängigkeiten (exakt gepinnt) |
| `tests/` | Tests für die Login-Logik (ohne Netzwerk) |

## Setup

### 1. Virtuelle Umgebung anlegen und Abhängigkeiten installieren
```bash
cd ~/Projects/Personal/garmin_mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
Die projekteigene `.venv` sorgt dafür, dass ein `pip install` anderswo nie die
Library-Version ändert, mit der der Server läuft.

### 2. Credentials als Umgebungsvariablen setzen
```bash
export GARMIN_EMAIL="deine@email.com"
export GARMIN_PASSWORD="deinpasswort"
```

### 3. Einmalig einloggen (cached die Tokens, behandelt 2FA)
```bash
.venv/bin/python login.py
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
      "command": "/Users/andreas/Projects/Personal/garmin_mcp/.venv/bin/python",
      "args": ["/Users/andreas/Projects/Personal/garmin_mcp/server.py"]
    }
  }
}
```
Hinweise:
- **Absoluter** Pfad zum Python der `.venv` — im Subprozess ist `PATH` oft
  anders als im Terminal.
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
| `GARMIN_MCP_LIB_DEBUG` | `0` schaltet die Debug-Logs der Library ab | an |

## Wenn der Login hakt

1. In Claude fragen: *„Wie ist mein Garmin-Login-Status?"* — das Tool
   `get_auth_status` zeigt Token-Datei, Ablaufzeit, letzten Fehler und ob ein
   Login-Versuch gerade pausiert ist.
2. Ist der Login tot: `.venv/bin/python login.py` im Terminal. Laufende Server
   merken die neue Token-Datei beim nächsten Aufruf von selbst, kein Neustart
   von Claude nötig.
3. Die genaue Ursache steht im Log:
   `~/Library/Logs/Claude/mcp-server-garmin.log` (z.B. `DI token refresh failed: 400`).

Was der Server selbst abfängt:
- **Mehrere Server-Prozesse** (Claude startet oft 2–3) teilen sich eine
  Token-Datei. Ein Datei-Lock sorgt dafür, dass immer nur einer die Tokens
  erneuert, und jeder Prozess lädt die Datei neu, sobald ein anderer sie
  geändert hat — sonst würde er mit einem schon verbrauchten Refresh-Token
  weitermachen.
- **401 mitten im Betrieb:** Tokens neu von der Platte laden, Aufruf einmal
  wiederholen.
- **Login-Pause:** Nach einem fehlgeschlagenen Login wird 5 Minuten (bei
  HTTP 429: 30 Minuten) nicht erneut probiert, damit Garmins SSO nicht
  dauernd angefragt wird. Die Pause endet sofort, wenn sich die Token-Datei
  ändert (z.B. nach `login.py`).

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

## Hinweise

- `garminconnect` ist eine **inoffizielle** Library. Garmin kann die interne
  API jederzeit ändern. Upgrade bewusst: Version in `requirements.txt`
  erhöhen, `.venv/bin/pip install -r requirements.txt`, Tests laufen lassen,
  live prüfen.
- Garmin hat Rate-Limits. Die Library wiederholt vorübergehende Server- und
  Netzwerkfehler automatisch mit wachsender Wartezeit.
