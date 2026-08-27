// App — top-level state and layout for the Melbourne Agent Village client.
//
// Responsibilities:
//  - Poll GET /v1/sim/{simId}/state every ≤2s via usePolling (Req15.3).
//  - Track selection (agent XOR location) for the detail panels (Req15.6/15.7).
//  - Track & display Simulated_Time, Acceleration_Factor, status (Req15.8).
//  - Provide start/pause/resume/stop controls, enabling only valid ones (Req15.9).
//  - Show a connection-lost banner and keep last positions (Req15.11/15.13).
//  - Honour pause: hold displayed sim-time, keep last positions/routes/indicators
//    (Req15.14) — achieved by continuing to render the last SimState.
//  - Enforce freshness: never render a Position older than 4s (Req15.3) — when the
//    hook reports `stale` (and we are NOT paused), we suppress live markers until
//    a fresh update arrives, while retaining the map itself.
//  - Offer a text-equivalent ListView toggle (Req15.10).

import { useCallback, useEffect, useState } from 'react'
import { usePolling } from './usePolling'
import { getLocations, config } from './api'
import { authConfig, hasValidToken, tryAutoLogin } from './auth'
import type { LocationItem, SimState, SimStatus } from './types'
import { Hud } from './components/Hud'
import { ControlBar } from './components/ControlBar'
import { MapView } from './components/MapView'
import { AgentPanel } from './components/AgentPanel'
import { LocationPanel } from './components/LocationPanel'
import { ListView } from './components/ListView'
import { ConnectionBanner } from './components/ConnectionBanner'
import { ConversationsPanel } from './components/ConversationsPanel'
import { LoginScreen } from './components/LoginScreen'

type Selection =
  | { kind: 'agent'; id: string }
  | { kind: 'location'; id: string }
  | null

const MOCK = config.mock

/**
 * Auth gate. In mock mode the login screen is skipped entirely (no network).
 * Otherwise, we require a valid Cognito token before mounting the polling app,
 * attempting an automatic operator login on load when credentials are provided.
 */
export default function App() {
  const [authed, setAuthed] = useState<boolean>(MOCK || hasValidToken())
  // null = still deciding (auto-login in flight); once resolved we know whether
  // to show the login screen.
  const [checking, setChecking] = useState<boolean>(!MOCK && !hasValidToken())
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (MOCK || authed) return
    let active = true
    void (async () => {
      const ok = await tryAutoLogin()
      if (!active) return
      if (ok) {
        setAuthed(true)
      } else if (authConfig.hasOperatorCreds) {
        setNotice('Automatic sign-in failed — please sign in manually.')
      }
      setChecking(false)
    })()
    return () => {
      active = false
    }
  }, [authed])

  if (!MOCK && !authed) {
    if (checking) {
      return (
        <div className="login-screen">
          <div className="login-card" aria-busy="true">
            <div className="login-emoji" aria-hidden="true">
              MV
            </div>
            <p className="login-sub">Signing in…</p>
          </div>
        </div>
      )
    }
    return <LoginScreen onSuccess={() => setAuthed(true)} notice={notice} />
  }

  return <VillageApp />
}

function VillageApp() {
  const conn = usePolling()
  const [locations, setLocations] = useState<LocationItem[]>([])
  const [selection, setSelection] = useState<Selection>(null)
  const [showList, setShowList] = useState(false)
  const [showConversations, setShowConversations] = useState(false)

  const status: SimStatus | null = conn.state?.status ?? null
  const paused = status === 'paused'

  // Load the static-ish location set once, then refresh occasionally so
  // status/present-agent counts stay current without hammering the API.
  useEffect(() => {
    let active = true
    const ac = new AbortController()
    async function load() {
      try {
        const locs = await getLocations(ac.signal)
        if (active) setLocations(locs)
      } catch {
        // Non-fatal: markers just won't render until locations load.
      }
    }
    void load()
    const timer = window.setInterval(load, 2000)
    return () => {
      active = false
      ac.abort()
      clearInterval(timer)
    }
  }, [])

  // Freshness gate (Req15.3): while running, never render positions older than
  // 4s. When stale, we withhold live agent/route/conversation layers but keep
  // the map + locations. While paused we intentionally hold the last state
  // (Req15.14), so staleness does not apply.
  const renderState: SimState | null =
    conn.state == null ? null : paused ? conn.state : conn.stale ? withoutAgents(conn.state) : conn.state

  const handleSelectAgent = useCallback((id: string) => {
    setSelection({ kind: 'agent', id })
  }, [])
  const handleSelectLocation = useCallback((id: string) => {
    setSelection({ kind: 'location', id })
  }, [])
  const clearSelection = useCallback(() => setSelection(null), [])

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to map
      </a>

      <header className="hud">
        <Hud simTime={conn.lastSimTime} accel={conn.state?.accel ?? null} status={status} />
        <ControlBar status={status} />
        <button
          className="btn btn-toggle"
          aria-pressed={showList}
          onClick={() => setShowList((v) => !v)}
        >
          {showList ? 'Map view' : 'List view'}
        </button>
        <button
          className="btn btn-toggle"
          aria-pressed={showConversations}
          onClick={() => setShowConversations((v) => !v)}
        >
          {showConversations ? 'Close chats' : 'Conversations'}
        </button>
        {config.mock && (
          <span className="hud-chip" aria-label="Running in mock mode">
            <span className="hud-label">Mode</span>
            <span className="hud-value">Mock</span>
          </span>
        )}
      </header>

      <main className="main" id="main-content">
        <MapView
          state={renderState}
          locations={locations}
          selectedAgentId={selection?.kind === 'agent' ? selection.id : null}
          selectedLocationId={selection?.kind === 'location' ? selection.id : null}
          onSelectAgent={handleSelectAgent}
          onSelectLocation={handleSelectLocation}
        />

        {conn.connectionLost && <ConnectionBanner lastSimTime={conn.lastSimTime} />}

        {showList && (
          <ListView
            state={renderState ?? conn.state}
            locations={locations}
            onSelectAgent={handleSelectAgent}
            onClose={() => setShowList(false)}
          />
        )}

        {selection?.kind === 'agent' && (
          <AgentPanel agentId={selection.id} onClose={clearSelection} />
        )}
        {selection?.kind === 'location' && (
          <LocationPanel
            locationId={selection.id}
            onClose={clearSelection}
            onSelectAgent={handleSelectAgent}
          />
        )}

        {showConversations && (
          <ConversationsPanel
            onClose={() => setShowConversations(false)}
            onSelectAgent={(id) => {
              handleSelectAgent(id)
            }}
            agentNames={Object.fromEntries(
              (conn.state?.agents ?? []).map((a) => [a.id, a.name]),
            )}
          />
        )}
      </main>
    </div>
  )
}

/** Strip live agents/conversations from a stale state while keeping metadata. */
function withoutAgents(state: SimState): SimState {
  return { ...state, agents: [], conversations: [] }
}
