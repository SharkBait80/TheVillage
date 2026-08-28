# Web SPA & E2E Test — Deep Quality Review

Melbourne Agent Village — `web/` (React 18.3.1 + Vite 5.4.11 + TypeScript 5.6 +
Leaflet/react-leaflet + Playwright 1.48). Review date 2026-08-28.

Scope: `web/src/*.ts`, `web/src/*.tsx`, `web/src/components/`, Playwright
configs and specs (`e2e/`, `e2e-live/`, `e2e-prod/`). No files were modified.

All line references are against the files as read during this review.

## Verification performed

- Read every in-scope source and test file.
- Confirmed `.env.production` is git-ignored (`git check-ignore` → ignored;
  `git ls-files` shows it is **not** tracked; no commit history for it).
- **Built bundle inspection**: `grep -rl "Village!Melb2026" dist` →
  `dist/assets/index-CeWt5JLz.js` **FOUND**. The operator password, username,
  Cognito Client ID, and API base URL are all present as plain string literals
  in the compiled JS (verified by reading the minified bundle: `Ap="operator"`,
  `Pu="Village!Melb2026"`, `Ip="1bnjnsrup6lfb4ovqa9bmu8ig1"`,
  `Zp="https://x7h1b78j89.execute-api.ap-southeast-2.amazonaws.com"`).
- Cross-checked remediation against Vite docs (env/secrets) and Playwright docs
  (web-first assertions, avoiding conditional test branches) via Context7.

---

## Severity summary

| # | Severity | Finding | Location |
|---|----------|---------|----------|
| 1 | **Critical** | Real operator credentials baked into the public JS bundle | `.env.production`, `auth.ts`, built `dist` |
| 2 | High | Long-lived JWT stored in `localStorage` (XSS token theft) | `auth.ts:56,74–82` |
| 3 | High | Auto-login uses embedded creds → single shared operator identity, no per-user auth | `auth.ts:203–216`, `App.tsx:52–70` |
| 4 | Medium | `ControlBar` `onAccepted` never wired; accepted commands don't update status until next poll | `App.tsx:150`, `ControlBar.tsx:47–61` |
| 5 | Medium | Reseed (destructive world-wipe) reachable by any operator, no server-side authz shown; confirm modal is the only guard | `ControlBar.tsx:97–105`, `api.ts:reseed` |
| 6 | Medium | Playwright specs contain conditional `if (count)` branches that pass silently when nothing is exercised | `e2e/functional.spec.ts` (location cross-nav, participant cross-nav) |
| 7 | Medium | `password` retained in module-level memory (`lastPassword`) for silent re-auth | `auth.ts:58,190–199` |
| 8 | Medium | DivIcon marker HTML built by string concat with inline `onerror`; XSS surface if `escapeHtml` ever misses a field | `AgentMarker.tsx:41–58`, `LocationMarker.tsx:38–55` |
| 9 | Low | Non-JSON / network `fetch` errors surface as terse `Request failed (status)`; no retry/backoff on transient 5xx | `api.ts:request`, `authedFetch` |
| 10 | Low | `useAuthImage` object-URL revoke race under React 18 StrictMode double-invoke | `useAuthImage.ts:34–70` |
| 11 | Low | Accessibility: dialogs lack focus trap + `aria-live` gaps; progressbar text; map keyboard-only reach | `AgentPanel.tsx`, `AddEventModal.tsx`, `MapView.tsx` |
| 12 | Low | Hardcoded external CDN deps (Google Fonts, unpkg Leaflet CSS/tiles) — availability + privacy + no SRI on font CSS | `index.html:11–24` |
| 13 | Low | `ListView` location matching by float epsilon is brittle/O(n·m) | `ListView.tsx:27–33` |
| 14 | Info | `VITE_POLL_MS` clamp only caps upper bound; no floor → can hammer API | `usePolling.ts:44` |

---

## 1. CRITICAL — Real operator credentials baked into the public JS bundle

**Files:** `web/.env.production`, `web/src/auth.ts:24–27`, compiled
`web/dist/assets/index-*.js`.

`.env.production` contains:

```
VITE_OPERATOR_USER=operator
VITE_OPERATOR_PASS=Village!Melb2026
VITE_COGNITO_CLIENT_ID=1bnjnsrup6lfb4ovqa9bmu8ig1
VITE_API_BASE_URL=https://x7h1b78j89.execute-api.ap-southeast-2.amazonaws.com
```

`auth.ts` reads these at build time:

```ts
const OPERATOR_USER = import.meta.env.VITE_OPERATOR_USER ?? ''   // auth.ts:26
const OPERATOR_PASS = import.meta.env.VITE_OPERATOR_PASS ?? ''   // auth.ts:27
```

Because Vite statically replaces `import.meta.env.VITE_*` at build time, these
literals are inlined into the shipped bundle. **Confirmed by inspecting the
actual build output** — the minified `dist` JS contains
`Ap="operator",Pu="Village!Melb2026"` and the Cognito client id
`Ip="1bnjnsrup6lfb4ovqa9bmu8ig1"`. Anyone who loads the CloudFront site can open
DevTools (or `curl` the JS) and read a working username/password plus the
Cognito App Client ID and API endpoint.

Vite's own documentation is explicit: *"`VITE_*` variables should not contain
sensitive information such as API keys. The values of these variables are
bundled into your source code at build time."*

The README frames this as a known "demo" tradeoff, and `.gitignore` correctly
excludes `.env.production` from source control — so the secret is not in git
history. **But git-ignoring does not help**: the credentials are shipped to
every browser in the public bundle. Anyone with the URL has full operator
control: `start`/`pause`/`resume`/`stop` **and the destructive `reseed`** that
deletes the entire world (see Finding 5).

Additional exposure: the auth flow uses Cognito `USER_PASSWORD_AUTH` directly
from the browser (`auth.ts:111–122`), so the plaintext password is also the
literal used in the client `InitiateAuth` call.

**Impact:** Complete compromise of operator control of the live simulation by
any anonymous visitor. The exposed password (`Village!Melb2026`) should be
treated as burned and rotated.

**Remediation:**
1. **Rotate the `operator` Cognito user password immediately** — it is public.
2. Remove `VITE_OPERATOR_USER`/`VITE_OPERATOR_PASS` from any build. Never put
   secrets behind a `VITE_` prefix.
3. Replace embedded auto-login with an interactive Cognito login (the
   `LoginScreen` already exists and works — just stop auto-submitting embedded
   creds). Prefer the Cognito Hosted UI / Authorization Code + PKCE flow over
   `USER_PASSWORD_AUTH` in the browser so the app never handles the raw
   password.
4. If a public demo must "just work," put a thin auth broker behind the API
   (server-side credential, short-lived scoped token minted per session) rather
   than shipping a real operator password. The Cognito Client ID and API URL are
   not secrets and are fine to ship.

---

## 2. HIGH — Long-lived JWT persisted in `localStorage` (XSS token theft)

**File:** `web/src/auth.ts:56, 63–66, 74–82`.

```ts
let idToken: string | null = readStoredToken()          // auth.ts:56
function readStoredToken() { return window.localStorage.getItem(STORAGE_KEY) } // 63
function storeToken(token) { window.localStorage.setItem(STORAGE_KEY, token) } // 76
```

The Cognito IdToken (a bearer credential valid ~1h) is stored in
`localStorage`. `localStorage` is readable by any JavaScript running on the
origin, so a single XSS (e.g. via a compromised CDN dependency — see Finding
12, or an injection through unescaped marker HTML — Finding 8) lets an attacker
exfiltrate a live bearer token and impersonate the operator against the API.

This is compounded by the app rendering LLM- and operator-authored content
(agent bios, conversation utterances, event descriptions) that originates
server-side; any stored-XSS in that content pipeline immediately reaches a
token in `localStorage`.

**Remediation:**
- Prefer keeping the token **in memory only** (the module already has an
  in-memory `idToken`; the `localStorage` copy is what adds the risk). Losing
  the token on refresh is acceptable given auto re-auth exists.
- If persistence across reloads is required, the standard hardening is an
  HttpOnly, `Secure`, `SameSite` cookie set by a backend — not reachable from
  JS. That requires the auth-broker approach in Finding 1.
- Add a Content-Security-Policy (currently none — see Finding 12) to reduce XSS
  blast radius.

---

## 3. HIGH — Single shared operator identity / no real per-user auth

**Files:** `web/src/auth.ts:203–216` (`tryAutoLogin`), `web/src/App.tsx:52–70`.

`App` auto-logs-in on mount using the embedded operator creds:

```ts
const ok = await tryAutoLogin()   // App.tsx:56
```

Every visitor becomes the same `operator` principal. There is no
authentication of the human, no audit trail distinguishing operators, and no
authorization tiers (e.g. read-only viewer vs. controller). Combined with
Finding 1 this means the deployed app is effectively unauthenticated for
control actions.

**Remediation:** Introduce real per-user sign-in (Cognito Hosted UI / user
pool users) and, if a public read-only view is desired, gate control/reseed
actions behind an authenticated + authorized role while allowing anonymous
read.

---

## 4. MEDIUM — `ControlBar.onAccepted` is never wired; status lag on control actions

**Files:** `web/src/App.tsx:150` (`<ControlBar status={status} />`),
`web/src/components/ControlBar.tsx:38–61`.

`ControlBar` exposes an `onAccepted?: (status) => void` callback and calls it on
a successful command:

```ts
const res = await control(cmd)
onAccepted?.(res.status)   // ControlBar.tsx:52
```

But `App` mounts `<ControlBar status={status} />` **without** passing
`onAccepted`. So an accepted command's returned status is discarded, and the UI
only reflects the new status when the next `/state` poll lands (up to
`POLL_MS`, clamped ≤ 2000ms, later). The design comment says this is
intentional ("App only advances the displayed status from polled state"), which
is a defensible choice for a single source of truth — but then `onAccepted` is
**dead code** that misleads readers and the button remains in its `…` busy state
resolves instantly while the status pill lags, which can look like a no-op to
operators and invites double-clicks.

**Remediation:** Either (a) delete the unused `onAccepted` prop to remove dead
code, or (b) wire it to optimistically update status with the polled state as
reconciliation. Pick one and document the intent. At minimum, keep the button
disabled briefly after accept to avoid double submits.

---

## 5. MEDIUM — Destructive `reseed` guarded only by a client-side confirm modal

**Files:** `web/src/components/ControlBar.tsx:64–80, 97–105, 108–147`,
`web/src/api.ts` (`reseed`).

`reseed()` POSTs `{ confirm: true }` and, per its own doc comment, *"wipes all
agents/events/state and regenerates a fresh population."* The only barrier in
the UI is a confirmation dialog. Given Findings 1–3 (anonymous visitor = shared
operator), this destructive action is reachable by anyone who can load the site
and click twice.

The client sends `confirm:true` hardcoded, so the server's "explicit
confirmation" requirement is trivially satisfied programmatically and provides
no real protection.

**Remediation:** Enforce authorization server-side for `reseed` (privileged
role), independent of the JWT that any visitor currently obtains. Consider a
typed-confirmation (type the sim id) and/or rate limiting. This is primarily a
backend authz concern but the SPA should not present the control to
unauthorized users.

---

## 6. MEDIUM — Playwright specs with conditional branches that pass without asserting

**File:** `web/e2e/functional.spec.ts`.

Two tests are structured so they can pass while exercising nothing:

`present-agent list cross-navigates` (location panel):
```ts
if (await presentBtn.count()) { ...click...; opened = true; break }
...
if (opened) { await expect(dialog...).toBeVisible() }
else { test.info().annotations.push({ type:'note', description:'...not exercised.' }) } // ~line 245
```

`clicking another participant cross-navigates`:
```ts
if (await participantBtn.count()) { ...assert... }
else { test.info().annotations.push({...'No cross-navigable participant...'}) }  // ~line 300
```

When the mock world happens not to produce a present agent / cross-navigable
participant that session, the test records an annotation and **passes green
without asserting the behaviour it names**. That is a shallow test — it can
mask a real regression in cross-navigation. Playwright best practice is
deterministic, web-first assertions; conditional `if (await locator.count())`
gating is discouraged because it hides skipped verification behind a passing
result.

The mock backend (`src/mock.ts`) is deterministic (seeded PRNG `Ma=1337`,
fixed personas, a pre-seeded conversation `ry(Pi[0],Pi[4])`, and two agents
placed adjacent for a `socialise` action). So these conditions **can** be made
deterministic and asserted unconditionally.

Other specs are genuinely meaningful (the control round-trip
`running→paused→running→stopped→running`, the need-progressbar range checks,
add-event success + rejection paths, no-mock-badge guards) — those are good.

**Remediation:** Make the mock deterministically produce a location with a
present agent and a conversation with a cross-navigable participant (it nearly
does already), then drop the `if/else annotation` fallbacks and assert
directly. If a scenario is genuinely environment-dependent, use `test.skip()`
with a condition rather than a silently-passing branch.

Secondary test observations:
- `e2e/functional.spec.ts` relies on `dispatchEvent('click')` on Leaflet
  markers to bypass hit-testing — pragmatic and documented, acceptable.
- Time-ordering assertion parses `en-AU` labels with `Date.parse(`${t} 2026`)`
  and then only compares when `Number.isFinite` — if parsing fails for all
  rows the loop asserts nothing (soft). Consider asserting the parse succeeded.
- `e2e-live`/`e2e-prod` depend on the embedded-cred auto-login (see the comment
  in `e2e-live/agent-conversations.spec.ts`: *"auto-logged-in via the operator
  creds baked into the production bundle"*) — the tests themselves document the
  Finding 1 vulnerability.

---

## 7. MEDIUM — Plaintext password kept in module memory for silent re-auth

**File:** `web/src/auth.ts:57–58, 190–199`.

```ts
let lastUsername: string | null = null
let lastPassword: string | null = null           // auth.ts:58
...
lastUsername = username; lastPassword = password  // auth.ts:196–197 (in login)
```

`ensureAuth()` reuses `lastPassword` to silently re-authenticate on 401/expiry.
Holding the plaintext password in a long-lived module variable widens the
window for memory-scraping via XSS and is unnecessary if refresh tokens (or the
Hosted-UI code flow) are used.

**Remediation:** Use Cognito **refresh tokens** (returned by `InitiateAuth`) to
renew the IdToken instead of replaying the password. This removes the need to
retain the password at all.

---

## 8. MEDIUM — Marker DivIcon HTML built via string concatenation + inline `onerror`

**Files:** `web/src/components/AgentMarker.tsx:41–58`,
`web/src/components/LocationMarker.tsx:38–55`.

Markers are Leaflet `DivIcon`s whose HTML is assembled by template string:

```ts
const html = `<img class="village-marker ..." src="${imgSrc}"
  alt="${escapeHtml(alt)}" onerror="this.onerror=null;this.src='${placeholder}'" />`
```

`escapeHtml()` is applied to `alt`, which is good, but this is a fragile
pattern:
- `src="${imgSrc}"` is not escaped. Today `imgSrc` is a blob URL or a bundled
  data-URL placeholder, so it's controlled — but if the asset pipeline ever
  yields a subject-derived or server-provided string here, an unescaped `"`
  breaks out of the attribute. The safety currently depends entirely on
  upstream invariants, not on the sink.
- The inline `onerror` interpolates `${placeholder}` into a JS string context
  inside an HTML attribute — two layers of escaping (JS + HTML) that
  `escapeHtml` (HTML-only) does not fully cover. Placeholders are static
  data-URLs today, so it's safe *in practice*, but it's a latent injection sink.

Combined with the `localStorage` token (Finding 2), any HTML injection here is
high impact.

**Remediation:** Build the icon `<img>` with DOM APIs
(`document.createElement('img')`, set `.src`/`.alt` as properties, attach an
`onerror` handler function) and pass the element as the `DivIcon` html, or use
Leaflet's ability to accept an `HTMLElement`. This makes the sink structurally
injection-proof rather than escaping-dependent. Add a CSP as defence-in-depth.

---

## 9. LOW — Terse API error surfacing; no transient-error retry/backoff

**File:** `web/src/api.ts` (`request`, `authedFetch`).

- Non-JSON responses throw `Request failed (${res.status})` with no body
  detail (`api.ts` `request`), which hides gateway/5xx diagnostics from
  operators.
- `authedFetch` retries **once** only on `401` (re-auth). Transient `429`/`5xx`
  get no backoff; the poll loop (`usePolling`) will just count them as failures
  toward `connectionLost`. That satisfies the connection-banner requirement but
  gives operators no distinction between "backend down" and "rate limited".
- The `ApiRequestError` for control rejections is surfaced nicely via toasts
  (`ControlBar.tsx:55–60`) — that path is good.

**Remediation:** Include response text (truncated) in the thrown error for
non-JSON failures; consider a small jittered backoff for 429/503 in the poll
loop; differentiate auth-expired vs. server-error messaging.

---

## 10. LOW — `useAuthImage` object-URL lifecycle under StrictMode double-invoke

**File:** `web/src/useAuthImage.ts:34–70`; app wraps in `<StrictMode>`
(`main.tsx:11`).

The hook creates a blob object URL and revokes it in cleanup. Under React 18
StrictMode (dev), effects run mount→unmount→mount. The code guards with an
`active` flag and revokes non-current URLs, which is mostly correct, but:
- On the throwaway first mount, `fetchAssetObjectUrl` may resolve after cleanup;
  the `.then` handler revokes the URL when `!active` only if it `startsWith
  'blob:'` — good — but the `objectUrl` captured in the *second* effect closure
  is independent, so there's a brief window where a revoked/again-created URL
  could be assigned to state. In practice this manifests (rarely) as a broken
  image that falls back to placeholder. Low impact, dev-mostly.

**Remediation:** Track the created object URL in a ref keyed to the current
effect run, and only ever set state / revoke for the run that is still active.
Consider a tiny in-memory cache keyed by `subjectId` to avoid re-fetching the
same portrait on every selection/refresh (the panels refetch on a 2s interval —
each `useAuthImage` consumer refetches the image only on `subjectId` change, so
this is minor, but a cache would also cut object-URL churn).

---

## 11. LOW — Accessibility gaps

Strengths: skip-link (`App.tsx`), `role="dialog"`/`aria-modal`, Escape-to-close,
focus-on-open (`panelRef.current?.focus()`), `role="progressbar"` with
`aria-valuenow/min/max` on need bars, `role="alert"` on errors, alt text on
portraits, a genuine text-equivalent `ListView` table with `scope` attributes.
This is above-average a11y for a map app.

Gaps:
1. **No focus trap** in dialogs (`AgentPanel`, `LocationPanel`, `AddEventModal`,
   reseed confirm). Focus can Tab out to the map behind an open modal; WCAG 2.4.3
   / dialog pattern expects focus containment and focus restoration to the
   trigger on close. `AddEventModal` doesn't restore focus to the map/HUD on
   close.
2. **Leaflet markers as the only way to reach locations by keyboard**: markers
   set `keyboard` and handle Enter/Space, but reaching them requires tabbing
   through Leaflet's focus order, which is not obvious. The `ListView` covers
   agents but there is **no text-equivalent list for locations** — a keyboard/AT
   user cannot easily open a `LocationPanel`. (README claims list view is the
   map text-equivalent; it omits locations.)
3. **`aria-live` on the status pill and sim-time** is `polite` (good), but the
   connection banner is `assertive` and also `role="alert"` — double-announce.
   Minor.
4. Toasts use `role="alert"` (`ControlBar.tsx`) — good — but auto-dismissing
   success/close flows (`AddEventModal` closes after 1500ms) may not give AT
   users time to read the verdict.
5. Emoji `💬`/`📡` are `aria-hidden` correctly; the `MV` brand monogram has an
   `aria-hidden` sparkle — fine.

**Remediation:** Add a focus trap + focus restoration to all dialogs (small
utility or a headless dialog lib); add a locations list to the text-equivalent
view; lengthen or make dismissible the add-event success message; audit
duplicate live-region announcements.

---

## 12. LOW — Hardcoded external CDN dependencies; no CSP; SRI only on Leaflet CSS

**File:** `web/index.html:11–24`.

- Google Fonts (`fonts.googleapis.com` / `fonts.gstatic.com`) and Leaflet CSS
  from `unpkg.com` are loaded from third-party CDNs. Leaflet CSS has an
  `integrity` (SRI) hash — good — but the Google Fonts stylesheet does not
  (SRI on the Google CSS is impractical, but it is still a third-party
  script/style origin).
- Map tiles come from `tile.openstreetmap.org` (`MapView.tsx`) — an external
  dependency and a privacy leak (every operator's map interaction is visible to
  OSM). Fine for a demo; note for production and OSM tile-usage-policy
  compliance.
- **No Content-Security-Policy** anywhere (no meta CSP, none configured on the
  CloudFront/S3 side visible here). A CSP would materially reduce the XSS blast
  radius that makes Findings 2 and 8 dangerous.

**Remediation:** Self-host fonts and Leaflet CSS (bundle them) to remove
runtime third-party origins and enable a strict CSP; add a CSP (script-src
'self', connect-src limited to the API + Cognito, img-src 'self' data: blob: +
tile host); document tile-provider terms for production.

---

## 13. LOW — Brittle location matching in ListView

**File:** `web/src/components/ListView.tsx:27–33`.

```ts
const near = locations.find(l =>
  Math.abs(l.lat - agent.lat) < 0.0006 && Math.abs(l.lon - agent.lon) < 0.0006)
```

Location naming for an agent's position is inferred by a fixed lat/lon epsilon
rather than the authoritative `action.targetId`/`locationId`. This can mislabel
agents standing between two nearby venues (Flinders St and Fed Square are
~0.0003 apart in this seed and would both fall inside the window), and it is
O(agents × locations) each render.

**Remediation:** Prefer the agent's `action.targetId` / a server-provided
`locationId` to name the current place; fall back to coordinates only when
absent. Build a `Map<id, location>` once.

---

## 14. INFO — `VITE_POLL_MS` clamp has no lower bound

**File:** `web/src/usePolling.ts:44`.

```ts
const POLL_MS = Number.isFinite(rawPoll) && rawPoll > 0 ? Math.min(rawPoll, 2000) : 1500
```

Upper-bounded at 2000ms (to satisfy the ≤2s freshness requirement) but a
misconfigured `VITE_POLL_MS=50` would poll 20×/s and hammer the API. Since this
is build-time config the risk is low, but a sane floor (e.g. 500ms) is cheap
insurance.

---

## What is done well

- Clean separation: typed API client with mock/live parity, view-model
  normalization adapters isolated in `api.ts`, small focused components.
- Robust geometry guards (`geo.ts`, `isValidLatLon`) prevent one malformed
  DynamoDB record from crashing the Leaflet map — good defensive engineering.
- Connection-health hook implements the freshness/pause/lost requirements
  carefully with abort controllers and a separate staleness timer.
- Vite `define` pins `VITE_MOCK` at compile time so a stray env can't ship the
  mock backend — and there is a dedicated `e2e-prod` regression guard for it.
  This is a genuinely good hardening pattern (ironic that the *credential* env
  didn't get the same "don't ship it" treatment).
- Mock backend is deterministic (seeded PRNG), enabling reliable tests.
- Most Playwright specs use proper web-first assertions and exercise real state
  transitions through the poll loop.

## Priority order for remediation

1. **Finding 1 + rotate the exposed password** (Critical, do first).
2. Findings 2, 3, 7 — auth architecture (memory-only/refresh tokens, real
   per-user login, drop embedded creds). These are one coherent workstream.
3. Finding 5 — server-side authz for `reseed`.
4. Finding 6 — de-flake/strengthen the two conditional E2E tests.
5. Findings 8, 12 — injection-proof marker rendering + CSP (defence-in-depth).
6. Findings 4, 9, 10, 11, 13, 14 — correctness/UX/a11y polish.
