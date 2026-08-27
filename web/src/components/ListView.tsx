// ListView — WCAG 2.1 AA text-equivalent of the map (Req15.10). A togglable,
// keyboard-accessible table listing every Agent's Persona name, current Location
// or Position, and current Action. Refreshed at the same interval as the map
// because it renders directly from the polled SimState App passes in.

import type { LocationItem, SimState, StateAgent } from '../types'

interface ListViewProps {
  state: SimState | null
  locations: LocationItem[]
  onSelectAgent: (agentId: string) => void
  onClose: () => void
}

function actionSummary(agent: StateAgent): string {
  const a = agent.action
  if (!a) return 'idle'
  const target = a.targetId ? ` → ${a.targetId}` : ''
  return `${a.type}${target}`
}

/** Prefer a named location when the agent sits on one; else show coordinates. */
function locationSummary(agent: StateAgent, locations: LocationItem[]): string {
  const near = locations.find(
    (l) => Math.abs(l.lat - agent.lat) < 0.0006 && Math.abs(l.lon - agent.lon) < 0.0006,
  )
  if (near) return near.name
  return `${agent.lat.toFixed(5)}, ${agent.lon.toFixed(5)}`
}

export function ListView({ state, locations, onSelectAgent, onClose }: ListViewProps) {
  const agents = state?.agents ?? []
  return (
    <section className="listview" aria-label="Agent list (text equivalent)">
      <button className="panel-close" onClick={onClose} aria-label="Close agent list">
        ×
      </button>
      <table>
        <caption>Village agents — text equivalent view</caption>
        <thead>
          <tr>
            <th scope="col">Agent</th>
            <th scope="col">Location / Position</th>
            <th scope="col">Current action</th>
          </tr>
        </thead>
        <tbody>
          {agents.length === 0 ? (
            <tr>
              <td colSpan={3}>No agents to display.</td>
            </tr>
          ) : (
            agents.map((a) => (
              <tr key={a.id}>
                <th scope="row">
                  <button
                    className="btn btn-toggle"
                    style={{ padding: '4px 10px', fontSize: 13 }}
                    onClick={() => onSelectAgent(a.id)}
                  >
                    {a.name}
                  </button>
                </th>
                <td>{locationSummary(a, locations)}</td>
                <td>{actionSummary(a)}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </section>
  )
}
