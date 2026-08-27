# Melbourne Agent Village — Visualisation Client

A React + TypeScript + Vite single-page app that renders the Melbourne Agent
Village simulation on a Leaflet map: agent markers, travel paths, conversation
indicators, detail panels, live controls, and a WCAG 2.1 AA text-equivalent list
view. It satisfies Requirement 15 (Map Visualisation) and the asset-fallback
parts of Requirement 16 (16.6 / 16.7).

The app polls the **Simulation_API** (`/v1/sim/{simId}/…`, DESIGN.md §5) over an
API Gateway HTTP API secured with Cognito JWT, and is intended to be hosted as a
static bundle on S3 + CloudFront.

## Features

- **Map** bounded to Melbourne (lat `[-38.00, -37.70]`, lon `[144.85, 145.10]`).
- **Agent markers** — one per agent, each individually selectable even when two
  agents share a position; portrait art with a cute placeholder fallback.
- **Location markers** — artwork + display name, placeholder fallback.
- **Travel paths** — polyline along the remaining route while travelling; removed
  within 2 s of completion/interruption.
- **Conversation indicators** — linking line + 💬 bubble; removed within 2 s of
  the conversation ending.
- **Agent panel** — persona, four need bars, cash, employment, legal status,
  current action, and the 10 most recent event-log entries.
- **Location panel** — name, category, opening hours, capacity, live status, and
  the persona name of every agent present.
- **Controls** — start / pause / resume / stop; only the controls valid for the
  current status are enabled; rejected commands surface the API's returned status
  and message without changing the displayed status.
- **List view** — keyboard-accessible text equivalent of the map, listing every
  agent's name, location/position, and current action.
- **Connection banner** — after 3 consecutive failures or 10 s with no success,
  shows a notice with the sim-time of the last update, keeps the last positions,
  retries every 5 s, and clears within 2 s on recovery.
- **Pause behaviour** — holds the displayed sim-time and keeps the last
  positions, routes, and indicators.
- **Freshness** — never renders an agent position older than 4 s while running.
- **Accessibility** — keyboard operable, focus-visible rings, alt text on every
  image, AA-contrast pastel storybook theme.

## Prerequisites

- Node.js 18+ and npm.

## Install

```bash
npm install
```

## Develop

```bash
npm run dev
```

Vite serves the app on `http://localhost:5173` by default.

### Mock mode (no backend)

The app ships with a built-in fake backend (`src/mock.ts`) that generates a small,
lively Melbourne — wandering agents, one traveller following a route, and a
conversation that starts/stops — so every UI feature is demonstrable **without**
a real Simulation_API.

Copy the example env and enable mock mode:

```bash
cp .env.example .env.local
# .env.local
VITE_MOCK=1
VITE_SIM_ID=melb
```

Then `npm run dev`. No network calls are made.

### Live backend mode

Set `VITE_MOCK=0` (or remove it) and point the client at your API Gateway HTTP
API:

```bash
# .env.local
VITE_MOCK=0
VITE_API_BASE_URL=https://xxxxxxxx.execute-api.ap-southeast-2.amazonaws.com
VITE_SIM_ID=melb
VITE_API_TOKEN=<Cognito JWT>   # sent as Authorization: Bearer <token>
# VITE_POLL_MS=1500            # optional; clamped to <=2000 for Req15.3
```

`VITE_API_BASE_URL` must have **no trailing slash**; the client appends `/v1/…`.
When `VITE_API_TOKEN` is present it is sent as `Authorization: Bearer <token>`
on API requests, and as an `access_token` query param on `<img>` asset requests
(headers can't ride an image request). A no-auth backend simply ignores it.

## Build

```bash
npm run build
```

Runs `tsc -b` (type-check) then `vite build`, emitting a static bundle to
`dist/`. Deploy `dist/` to S3 + CloudFront.

```bash
npm run preview   # serve the production build locally
```

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `VITE_MOCK` | `1` runs the built-in fake backend with no network | unset (live) |
| `VITE_SIM_ID` | Simulation id used in `/v1/sim/{simId}/…` paths | `melb` |
| `VITE_API_BASE_URL` | Base URL of the API (no trailing slash) | empty |
| `VITE_API_TOKEN` | Cognito JWT; sent as `Authorization: Bearer …` | empty |
| `VITE_POLL_MS` | Poll interval override (ms), clamped ≤ 2000 | `1500` |

## Project structure

```
src/
  main.tsx                 React 18 entry
  App.tsx                  Top-level state: polling, selection, status, connection
  usePolling.ts            Poll + connection-health hook (Req15.3/15.11/15.13)
  api.ts                   Typed Simulation_API client (mock + live)
  mock.ts                  Built-in fake backend (VITE_MOCK=1)
  types.ts                 Shared types mirroring DESIGN.md §4/§5
  placeholders.ts          Cute inline-SVG placeholder art (Req16.7)
  styles.css               Pastel storybook theme (WCAG AA contrast)
  components/
    Hud.tsx                Header: sim-time, acceleration, status
    ControlBar.tsx         Start/pause/resume/stop with validity + rejection toast
    MapView.tsx            Leaflet map bounded to Melbourne
    AgentMarker.tsx        Selectable agent portrait marker
    LocationMarker.tsx     Location artwork + name marker
    TravelPath.tsx         Remaining-route polyline
    ConversationIndicator.tsx  Linking line + bubble
    AgentPanel.tsx         Agent detail panel
    LocationPanel.tsx      Location detail panel
    ListView.tsx           WCAG text-equivalent agent list
    ConnectionBanner.tsx   Connection-lost notice
```

## Notes

- `react-leaflet` 4.2.1 is pinned against `leaflet` 1.9.4 and React 18.3.1 — a
  compatible peer set (react-leaflet 4.x targets React 18).
- Leaflet's CSS is loaded from the CDN in `index.html`; the map tiles come from
  OpenStreetMap.
