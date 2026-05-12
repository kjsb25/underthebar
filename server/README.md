# Strava → Hevy Always-On Import Service

A small FastAPI service that polls Strava every N minutes and auto-imports
matching activities into Hevy. Mirrors the desktop app's Strava import
controls in a web UI; intended as a complementary deployment, not a
replacement for the desktop client.

## What it does

- Polls Strava on a configurable schedule (default 10 min).
- Filters activities by type (Run, Ride, Walk, Hike — same as desktop).
- Posts each new activity as a Hevy workout (`POST /v2/workout`, falls back
  to `PUT` on 409, matching `strava_api.import_activity`).
- Persists rotating Hevy refresh tokens, imported activity IDs, and an event
  log in SQLite under `/data/state.db`.
- Web UI for status, manual sync, manual activity picker, and all settings.

## Architecture

```
server/
├── app.py               FastAPI app + routes
├── state.py             SQLite schema and accessors
├── strava_client.py     Strava OAuth + activity fetch/import (extracted from strava_api.py)
├── hevy_client.py       Hevy token refresh + workout submission (extracted from hevy_api.py)
├── poller.py            asyncio polling loop
├── templates/           Jinja2 templates (dashboard, settings, auth)
├── Dockerfile           non-root, read-only rootfs, dropped caps
└── docker-compose.yml   Traefik-fronted; basic-auth middleware
```

The desktop app is untouched. Logic shared between the two is duplicated
intentionally — the server runs in a slim container without PySide6, dotenv,
or browser-based OAuth callbacks.

## Configuration

Environment variables:

| Var | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `/data` | Where SQLite lives. Should be a volume. |
| `PUBLIC_BASE_URL` | (request host) | Used to build the Strava OAuth callback URL. Set this in compose. |
| `LOG_LEVEL` | `INFO` | Standard logging level. |

Runtime settings (managed via the web UI, stored in SQLite):

- Polling enabled / interval seconds / lookback hours
- Activity type filters (Run/Ride/Walk/Hike + per-user VirtualRide)
- Import private toggle
- Strava client ID / secret
- Strava + Hevy auth tokens

## Bootstrap (one-time)

### 1. Strava

1. Create a Strava API app at <https://www.strava.com/settings/api>.
2. Set the **Authorization Callback Domain** to your `PUBLIC_HOST`
   (e.g. `strava.example.com`, no scheme/path).
3. Note the Client ID and Secret.

### 2. Deploy

From the Docker host:

```sh
cd server/
# create .env next to docker-compose.yml with:
#   PUBLIC_HOST=strava.example.com
#   PUBLIC_BASE_URL=https://strava.example.com
#   TRAEFIK_AUTH_USER=keenan:$$2y$$05$$...  (htpasswd -nbB, doubled $)
./nova.sh up strava-hevy   # or however you bring up stacks
```

### 3. Web UI bootstrap

Open `https://<PUBLIC_HOST>/` (Traefik basic-auth will gate access).

1. **Settings → Strava API credentials**: paste Client ID/Secret, Save.
2. **Auth → Authorize Strava**: completes OAuth; refresh token is stored.
3. **Auth → Hevy**: paste `access_token` and `refresh_token` from your
   desktop's `~/.underthebar/session.json`. The service verifies by
   fetching `/account`.
4. **Settings → Activity type filters**: pick types to auto-import.
5. **Settings → Privacy**: choose whether imports should be private.
6. **Settings → Polling**: enable auto-import and set interval.

The poller will tick within `TICK_SECONDS` (10s) and run a fetch+import
cycle when the interval has elapsed.

## Web UI routes

| Route | Purpose |
|---|---|
| `GET /` | Dashboard: status, manual sync, manual import picker, recent imports, recent log |
| `GET /settings` | All toggles + credentials |
| `GET /auth` | Strava OAuth and Hevy token paste |
| `GET /auth/strava` → `GET /auth/strava/callback` | OAuth flow |
| `POST /api/sync` | Trigger a poll immediately |
| `GET /api/activities` | Recent matching Strava activities (JSON) |
| `POST /api/import/{id}` | Import a specific activity |
| `GET /api/status` | JSON status |
| `GET /healthz` | Liveness |

## Failure modes & recovery

- **Strava refresh token revoked**: visit Auth → Authorize Strava again.
- **Hevy refresh token revoked / rotated externally**: the service will log
  `Token refresh failed`. Paste fresh tokens from desktop session.json.
- **Hevy returns non-200 on submit**: imported_activities is not updated, so
  the next poll retries automatically.
- **DB corruption**: nuke `strava-hevy-data` volume and re-bootstrap. Already
  imported workouts on Hevy won't be duplicated because Hevy 409s and the
  service then PUTs to the same deterministic workout ID.

## Security notes

- Container runs as non-root (uid 1000), read-only rootfs, `cap_drop: ALL`,
  `no-new-privileges`. Only `/data` and `/tmp` are writable.
- Auth is enforced by Traefik basic-auth middleware. The app itself has no
  user accounts.
- Tokens are stored in plaintext SQLite. If you need at-rest encryption,
  mount the volume on a LUKS-encrypted disk or layer SOPS on the bootstrap.
- The dependency on the `with_great_power` Hevy API key matches the desktop
  app (hard-coded in their client) — same trust boundary.

## Differences from the desktop import

| Aspect | Desktop | Service |
|---|---|---|
| Trigger | Manual button | Polling (configurable) + manual button |
| Auth state | `~/.underthebar/session.json` + `.env` | SQLite at `/data/state.db` |
| OAuth callback | localhost HTTP server | Public `/auth/strava/callback` |
| `_is_production()` | Git branch / PyInstaller check | Removed — `import_private` is explicit |
| Hevy auth | Firefox cookies or token paste | Token paste only |
| Multi-user | N/A (single user) | N/A (single user — Traefik basic-auth) |
