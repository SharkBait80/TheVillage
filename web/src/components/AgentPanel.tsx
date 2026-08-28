// AgentPanel — full agent detail (Req15.6): persona, 4 need bars, cash,
// employment, legal, current action, and the 10 most recent event-log entries.
// Fetches GET /v1/sim/{simId}/agents/{agentId} on selection and refreshes while
// open so the panel stays live.

import { useEffect, useMemo, useRef, useState } from 'react'
import { getAgent, getConversations, getDecisionTrail } from '../api'
import { agentPlaceholder } from '../placeholders'
import { useAuthImage } from '../useAuthImage'
import type { AgentDetail, ConversationItem, DecisionTrail, NeedLevels } from '../types'

interface AgentPanelProps {
  agentId: string
  onClose: () => void
  /**
   * Optional agentId -> display name map so conversation participants render as
   * names rather than ids. Falls back to the id when a name is unknown.
   */
  agentNames?: Record<string, string>
  /** Cross-navigate to another agent (e.g. clicking a conversation participant). */
  onSelectAgent?: (agentId: string) => void
}

/**
 * Sort conversations by date of occurrence, most recent first (descending).
 * `simTime` is an ISO string so lexical comparison is chronological; `seq` is a
 * monotonic tiebreaker for conversations that share a sim-time.
 */
function sortByDateDesc(convos: ConversationItem[]): ConversationItem[] {
  return [...convos].sort((a, b) => {
    const ta = a.simTime ?? ''
    const tb = b.simTime ?? ''
    if (ta !== tb) return tb < ta ? -1 : 1
    return (b.seq ?? 0) - (a.seq ?? 0)
  })
}

const NEED_ORDER: (keyof NeedLevels)[] = ['hunger', 'energy', 'social', 'fun']
const NEED_LABEL: Record<keyof NeedLevels, string> = {
  hunger: 'Hunger',
  energy: 'Energy',
  social: 'Social',
  fun: 'Fun',
}

function formatEventTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString('en-AU', {
      hour: '2-digit',
      minute: '2-digit',
      day: '2-digit',
      month: 'short',
      timeZone: 'Australia/Melbourne',
    })
  } catch {
    return iso
  }
}

function actionText(detail: AgentDetail): string {
  const a = detail.currentAction
  if (!a) return 'Idle (no current action)'
  const target = a.targetId ? ` → ${a.targetId}` : ''
  const dur = a.expectedDurationMin ? ` (~${a.expectedDurationMin} min)` : ''
  return `${a.type}${target}${dur}`
}

export function AgentPanel({ agentId, onClose, agentNames, onSelectAgent }: AgentPanelProps) {
  const [detail, setDetail] = useState<AgentDetail | null>(null)
  const [trail, setTrail] = useState<DecisionTrail | null>(null)
  const [conversations, setConversations] = useState<ConversationItem[]>([])
  const [conversationsLoaded, setConversationsLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let active = true
    const ac = new AbortController()
    async function load() {
      try {
        const d = await getAgent(agentId, ac.signal)
        if (active) {
          setDetail(d)
          setError(null)
        }
      } catch (err) {
        if ((err as Error)?.name === 'AbortError') return
        if (active) setError((err as Error).message || 'Failed to load agent')
      }
    }
    void load()
    // Refresh while open (same cadence as map, R15.6 live view).
    const timer = window.setInterval(load, 2000)
    return () => {
      active = false
      ac.abort()
      clearInterval(timer)
    }
  }, [agentId])

  // Move focus into the panel for keyboard users (WCAG 2.1 AA).
  useEffect(() => {
    panelRef.current?.focus()
  }, [agentId])

  // Fetch the "thought process" (decision trail) for the agent's most recent
  // decision (action) event. In mock mode the seq is synthetic; live mode uses
  // the latest action event's seq from recentEvents.
  useEffect(() => {
    const actionEvents = (detail?.recentEvents ?? []).filter((e) => e.category === 'action')
    if (actionEvents.length === 0) {
      setTrail(null)
      return
    }
    const latestSeq = actionEvents.reduce((m, e) => (e.seq > m ? e.seq : m), actionEvents[0].seq)
    let active = true
    const ac = new AbortController()
    void (async () => {
      try {
        const t = await getDecisionTrail(latestSeq, ac.signal)
        if (active) setTrail(t)
      } catch {
        if (active) setTrail(null)
      }
    })()
    return () => {
      active = false
      ac.abort()
    }
  }, [agentId, detail?.recentEvents])

  const portrait = useAuthImage(agentId, agentPlaceholder())

  // Conversations this agent took part in (Req: agent detail shows the
  // conversations they are involved in). The API filters by participant via
  // ?agentId=; we additionally sort client-side to guarantee date-descending
  // order regardless of backend/mock ordering. Refreshes while open so new
  // conversations appear as the agent keeps talking.
  useEffect(() => {
    let active = true
    const ac = new AbortController()
    setConversations([])
    setConversationsLoaded(false)
    async function load() {
      try {
        const convos = await getConversations(agentId, ac.signal)
        if (active) {
          setConversations(sortByDateDesc(convos))
          setConversationsLoaded(true)
        }
      } catch (err) {
        if ((err as Error)?.name === 'AbortError') return
        if (active) setConversationsLoaded(true)
      }
    }
    void load()
    const timer = window.setInterval(load, 2000)
    return () => {
      active = false
      ac.abort()
      clearInterval(timer)
    }
  }, [agentId])

  const nameOf = useMemo(() => {
    return (id: string) => {
      if (agentNames?.[id]) return agentNames[id]
      if (detail && id === detail.id) return detail.persona.name
      return id
    }
  }, [agentNames, detail])

  return (
    <aside
      className="panel"
      role="dialog"
      aria-label="Agent details"
      tabIndex={-1}
      ref={panelRef}
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
      }}
    >
      <button className="panel-close" onClick={onClose} aria-label="Close agent details">
        ×
      </button>

      {error && <p role="alert">{error}</p>}
      {!detail && !error && <p>Loading agent…</p>}

      {detail && (
        <>
          <img
            className="panel-portrait"
            src={portrait.src}
            width={72}
            height={72}
            alt={`Portrait of ${detail.persona.name}`}
            onError={(e) => {
              const img = e.currentTarget
              img.onerror = null
              img.src = agentPlaceholder()
            }}
          />
          <h2>{detail.persona.name}</h2>
          <p className="subtitle">
            {detail.persona.age} · {detail.persona.occupation}
            {detail.persona.mbti ? ` · ${detail.persona.mbti}` : ''}
          </p>
          <div style={{ clear: 'both' }} />

          <div>
            {detail.persona.traits.map((t) => (
              <span className="tag" key={t}>
                {t}
              </span>
            ))}
          </div>

          <p style={{ marginTop: 10 }}>{detail.persona.background}</p>

          <div className="section-title">Needs</div>
          {NEED_ORDER.map((key) => {
            const value = detail.needs[key]
            const critical = detail.critical?.[key]
            return (
              <div className={`need need-${key} ${critical ? 'critical' : ''}`} key={key}>
                <div className="need-row">
                  <span>
                    {NEED_LABEL[key]}
                    {critical ? ' ⚠ critical' : ''}
                  </span>
                  <span>{value}/100</span>
                </div>
                <div
                  className="need-track"
                  role="progressbar"
                  aria-valuenow={value}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={`${NEED_LABEL[key]} level`}
                >
                  <div className="need-fill" style={{ width: `${value}%` }} />
                </div>
              </div>
            )
          })}

          <div className="section-title">Status</div>
          <div className="kv">
            <span className="k">Cash</span>
            <span className="v">${detail.cash.toFixed(2)} AUD</span>
          </div>
          <div className="kv">
            <span className="k">Employment</span>
            <span className="v">{detail.employmentStatus}</span>
          </div>
          <div className="kv">
            <span className="k">Legal</span>
            <span className="v">{detail.legalStatus}</span>
          </div>
          <div className="kv">
            <span className="k">Current action</span>
            <span className="v">{actionText(detail)}</span>
          </div>

          <div className="section-title">Thought process</div>
          {trail && (trail.reasoning || trail.perceptionInput) ? (
            <div className="thought">
              {trail.reasoning ? (
                <p className="thought-reasoning">“{trail.reasoning}”</p>
              ) : (
                <p className="thought-reasoning muted">No reasoning recorded for the latest decision.</p>
              )}
              {trail.perceptionInput && (
                <div className="thought-perception" aria-label="What the agent perceived">
                  <span className="thought-label">Perceived when deciding:</span>
                  <ul>
                    {trail.perceptionInput.locationId && (
                      <li>Location: {trail.perceptionInput.locationId}</li>
                    )}
                    {trail.perceptionInput.needs && (
                      <li>
                        Needs —{' '}
                        {(['hunger', 'energy', 'social', 'fun'] as const)
                          .filter((k) => trail.perceptionInput?.needs?.[k] != null)
                          .map((k) => `${k} ${trail.perceptionInput!.needs![k]}`)
                          .join(', ')}
                      </li>
                    )}
                    {typeof trail.perceptionInput.cash === 'number' && (
                      <li>Cash: ${trail.perceptionInput.cash.toFixed(2)}</li>
                    )}
                  </ul>
                </div>
              )}
            </div>
          ) : (
            <p className="muted">No decision recorded yet.</p>
          )}

          <div className="section-title">Recent events</div>
          {detail.recentEvents.length === 0 ? (
            <p>No events yet.</p>
          ) : (
            <ul className="event-list">
              {detail.recentEvents.slice(0, 10).map((ev) => (
                <li className="event-item" key={ev.seq}>
                  <div className="event-time">{formatEventTime(ev.simTime)}</div>
                  {ev.description}
                </li>
              ))}
            </ul>
          )}

          <div className="section-title">Conversations</div>
          {!conversationsLoaded ? (
            <p className="muted">Loading conversations…</p>
          ) : conversations.length === 0 ? (
            <p className="muted">
              {detail.persona.name} hasn’t taken part in any conversations yet.
            </p>
          ) : (
            <ul className="conversation-list agent-conversation-list">
              {conversations.map((c) => (
                <li className="conversation-card" key={c.id}>
                  <div className="conversation-head">
                    <span className="conversation-participants">
                      {c.participants.map((id, i) => (
                        <span key={id}>
                          {i > 0 && <span aria-hidden="true"> · </span>}
                          {onSelectAgent && id !== detail.id ? (
                            <button className="link-btn" onClick={() => onSelectAgent(id)}>
                              {nameOf(id)}
                            </button>
                          ) : (
                            <span>{nameOf(id)}</span>
                          )}
                        </span>
                      ))}
                    </span>
                    <span className="conversation-time">{formatEventTime(c.simTime)}</span>
                  </div>
                  <ol className="utterances">
                    {c.utterances.map((u, i) => (
                      <li className="utterance" key={i}>
                        <span className="utterance-speaker">{nameOf(u.speaker)}:</span>{' '}
                        <span className="utterance-text">{u.text}</span>
                      </li>
                    ))}
                  </ol>
                  {c.truncated && <p className="muted small">(conversation was cut short)</p>}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </aside>
  )
}
