# Web Main Page — Structure & Migration Plan

This document describes how the desktop PySide6 app's main window maps onto the
web interface, what the new landing page (`/`) contains, and how each Python
page will eventually be ported. The first pass implements the landing page and
groups the existing Strava-import functionality under its own section; all
other desktop pages are represented by placeholder sections.

## Desktop reference

`underthebar.py` defines a `QMainWindow` with a vertical button column on the
left and a `QStackedLayout` on the right. The buttons are, top-to-bottom:

| # | Icon | Class | Source file | Responsibility |
|---|---|---|---|---|
| 0 | `user-solid.svg` | `Profile` | `utb_page_profile.py` | Account snapshot, calendar heat map, recent feed, body-measures plot |
| 1 | `dumbbell-solid.svg` | `Routines` | `utb_page_routines.py` | Routine viewer / JSON editor; Garmin .fit → routine import |
| 2 | `chart-line-solid.svg` | `Analysis` | `utb_page_analysis.py` | List of available plots (1RM, volume, body parts, Wilks, …) and rendered output |
| 3 | `user-group-solid-full.svg` | `Social` | `utb_page_social.py` | Friends, social feed (forked from Profile) |
| 4 | `gear-solid.svg` | `Setting` | `utb_page_setting.py` | API key management, activity-type filters, Strava import controls, logout, PRs |

The Strava → Hevy import flow today lives inside the desktop **Settings** page.
On the web it has already been promoted to a first-class concern (its own
service), so the web treats it as a peer of the desktop pages rather than a
sub-feature of Settings.

## Current web routes (before this change)

| Route | Template | Notes |
|---|---|---|
| `GET /` | `dashboard.html` | Status + manual import picker + recent imports + log |
| `GET /settings` | `settings.html` | Polling, type filters, privacy, Strava creds |
| `GET /auth` | `auth.html` | Strava OAuth, Hevy token paste |

The pre-existing `/` page is import-centric — every section there is part of
the Strava → Hevy flow. It is *not* a counterpart to the desktop main window.

## New web layout

Goal: the web home page should be the equivalent of the desktop main window —
a hub that gives you a quick read on each major area and a way to jump in.

### Top-level navigation (header)

```
Home  |  Import  |  Profile  |  Routines  |  Analysis  |  Social  |  Settings  |  Auth
```

For the first pass only `Home`, `Import`, `Settings`, and `Auth` are wired up.
The placeholder pages (`Profile`, `Routines`, `Analysis`, `Social`) are
represented on the home page as cards with "coming soon" copy; clicking them
scrolls to or expands the placeholder section rather than navigating off
`/`. Once each is implemented, it gets its own route and a real nav entry.

### `/` — Home

The home page is a single column of cards:

1. **Hero / branding** — `═|██══ UNDER THE BAR ══██|═` lockup (matching the
   Qt-side `QLabel` HTML), tagline, and the global auth status pills
   (Strava / Hevy) from the existing context helper.
2. **At a glance** — small grid: polling on/off + interval, last poll, imports
   today, imports total. Identical to today's dashboard "Status" card but
   minus the action buttons (those live on `/import`).
3. **Profile** *(placeholder)* — describes the desktop Profile page (account
   snapshot, calendar heat map, recent feed, body-measures plot) and notes
   that it is not yet implemented on the web.
4. **Routines** *(placeholder)* — describes the routine viewer / Garmin .fit
   importer.
5. **Analysis** *(placeholder)* — describes the plots library.
6. **Social** *(placeholder)* — describes the friends / social feed page.
7. **Strava → Hevy import** *(section)* — short summary + a primary
   "Open import dashboard" link to `/import`, plus a peek at the most recent
   imports. This is the "group the current web import functionality under its
   own section" requirement.

Each placeholder card uses the same visual style as the real cards but with a
muted "Not yet ported" pill so it is obvious what is and isn't live.

### `/import` — Strava → Hevy

Hosts everything currently on `/` today: status grid with the "Sync now"
button, manual activity picker, recent imports table, recent log. No
functional changes — only the route and template name shift. JSON endpoints
(`/api/sync`, `/api/activities`, `/api/import/{id}`, `/api/status`) are
unchanged.

### `/settings` and `/auth`

Unchanged in scope. The settings page already covers Strava credentials,
polling cadence, activity-type filters, and the private toggle. The auth page
already covers Strava OAuth and the Hevy token paste. Both still need the
desktop's logout / API-key management eventually, but that is out of scope for
this pass.

## Mapping desktop → web (porting roadmap)

| Desktop page | Web counterpart | Status after this pass |
|---|---|---|
| Profile | `/profile` | Placeholder card on `/` |
| Routines | `/routines` | Placeholder card on `/` |
| Analysis | `/analysis` | Placeholder card on `/` |
| Social | `/social` | Placeholder card on `/` |
| Settings (API keys, filters, PRs) | `/settings` | Existing — covers Strava import subset |
| Settings (logout) | `/auth` clear-tokens forms | Existing |
| Strava import controls (currently inside Settings) | `/import` | New dedicated route — content moved from `/` |

When each placeholder is implemented:
1. Add a new route + template under `server/templates/`.
2. Promote its card on `/` from a placeholder (muted pill + "coming soon") to
   a live summary widget with a link to the dedicated page.
3. Add the page to the header nav in `base.html`.
4. Leave the placeholder fallback in place behind an `is_implemented` flag in
   the context so partial deploys don't regress.

## Implementation notes

- **Templates** — Jinja2 via `fastapi.templating.Jinja2Templates`. Reuse the
  card / pill / grid CSS already in `base.html`; no new global styles needed
  for the placeholder cards.
- **Context** — `_ctx()` in `app.py` already provides every status field the
  home page needs (`strava_authorized`, `hevy_authorized`, `polling_enabled`,
  `last_poll_at`, `counts`). The home route only needs to also pull
  `recent_imports(3)` for the import-summary section.
- **No new state** — placeholder sections do not read or write SQLite.
- **Backwards compatibility** — keep `/api/*` routes and `/healthz` exactly as
  they are. Anyone calling these (the dashboard JS, monitoring) is unaffected.
- **Active-nav highlighting** — `base.html` keys off `request.url.path`. The
  new `/import` and `/` routes must each highlight their own entry.

## Out of scope for this pass

- Implementing any of the placeholder pages.
- Multi-user support (the desktop app and the service are both single-user
  today; Traefik basic-auth gates the deployment).
- Restyling — the new home page reuses the existing dark palette and card
  system; no new CSS variables are introduced.
