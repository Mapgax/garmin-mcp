# garmin_mcp — CLAUDE.md

Local MCP server (stdio) that exposes Andreas's Garmin Connect data to Claude Desktop
and Claude Code. Built on the unofficial `garminconnect` library — Garmin's official
Health API is partner-only, so there is no supported alternative.

- Git root: this folder. Remote: `github.com/Mapgax/garmin-mcp`, branch `main`.
  No CI, no deploy target — "shipped" means pushed.
- Docs and code comments are in **German**; keep that style.

## Layout

| Path | What |
|---|---|
| `server.py` | FastMCP server: tool definitions + formatting raw Garmin data into compact fields. Entry point. |
| `garmin_client.py` | Wrapper around `garminconnect.Garmin`: login, token store, cross-process lock, reload/retry on 401, login cooldown, `auth_status()`. |
| `login.py` | Interactive one-off login (2FA prompt). Writes the token cache. |
| `tests/` | `unittest` tests for the login logic, using a fake Garmin factory (no network). |
| `.venv/` | Project virtualenv (git-ignored). Claude Desktop runs the server with `.venv/bin/python`. |

## Runbook

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # setup
.venv/bin/python -m unittest discover -s tests -t .                   # tests
.venv/bin/python login.py                                             # re-login (needs 2FA code)
```

- Claude Desktop config: `~/Library/Application Support/Claude/claude_desktop_config.json`,
  entry `mcpServers.garmin` → command `.venv/bin/python`, arg `server.py` (absolute paths).
  Restart Claude Desktop after code changes; old server processes keep running old code.
- Server log: `~/Library/Logs/Claude/mcp-server-garmin.log`. Library debug output
  (refresh failures etc.) is on by default; `GARMIN_MCP_LIB_DEBUG=0` turns it off.

## Auth model — read before touching login code

- Token cache: `~/.garminconnect/garmin_tokens.json` (0600, dir 0700) holds a DI access
  token (~27 h) and a refresh token. The library refreshes automatically; Garmin
  rotates the refresh token on each refresh.
- Claude Desktop starts **2–3 server processes** sharing that file. Hence every call
  holds an `flock` on `~/.garminconnect/.lock` and reloads the token file when its
  mtime/inode changed. Don't remove this.
- Andreas's account has **2FA by email**. A fresh credential login therefore always
  needs a human with the code → a dead refresh token can only be fixed by running
  `login.py`. Storing the password (env/Keychain) does not enable headless recovery.
- `garminconnect` is pinned exactly (`==0.3.20`). It changes often and restructured
  its package between 0.3.6 and 0.3.20. Upgrade deliberately: bump, run tests, live-check
  with a real call. When live-testing, avoid forcing a token refresh while other
  server processes are running on old code.

## Secrets

Names only — none are set in the Claude Desktop config by default:
`GARMIN_EMAIL`, `GARMIN_PASSWORD` (only needed for `login.py`), `GARMINTOKENS`
(token dir override). Never print token file contents; `auth_status()` reports
expiry and presence only.
