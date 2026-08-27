// ConversationsPanel — a live feed of recent agent-to-agent conversations with
// full utterance text. Polls GET /v1/sim/{simId}/conversations while open so new
// conversations appear as agents talk. Clicking a participant name opens that
// agent's detail panel (and its thought-process).

import { useEffect, useRef, useState } from 'react'
import { getConversations } from '../api'
import type { ConversationItem } from '../types'

interface ConversationsPanelProps {
  onClose: () => void
  onSelectAgent: (agentId: string) => void
  /** Map of agentId -> display name, so we can show names instead of ids. */
  agentNames: Record<string, string>
}

function formatTime(iso: string): string {
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

export function ConversationsPanel({ onClose, onSelectAgent, agentNames }: ConversationsPanelProps) {
  const [conversations, setConversations] = useState<ConversationItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let active = true
    const ac = new AbortController()
    async function load() {
      try {
        const convos = await getConversations(undefined, ac.signal)
        if (active) {
          setConversations(convos)
          setError(null)
          setLoaded(true)
        }
      } catch (err) {
        if ((err as Error)?.name === 'AbortError') return
        if (active) {
          setError((err as Error).message || 'Failed to load conversations')
          setLoaded(true)
        }
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

  useEffect(() => {
    panelRef.current?.focus()
  }, [])

  const nameOf = (id: string) => agentNames[id] ?? id

  return (
    <aside
      className="panel conversations-panel"
      role="dialog"
      aria-label="Conversations"
      tabIndex={-1}
      ref={panelRef}
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
      }}
    >
      <button className="panel-close" onClick={onClose} aria-label="Close conversations">
        ×
      </button>
      <h2>💬 Conversations</h2>
      <p className="subtitle">What the villagers are saying to each other</p>

      {error && <p role="alert">{error}</p>}
      {!error && loaded && conversations.length === 0 && (
        <p className="muted">
          No conversations yet. When agents socialise with each other, their chats will appear here.
        </p>
      )}
      {!loaded && !error && <p>Loading conversations…</p>}

      <ul className="conversation-list">
        {conversations.map((c) => (
          <li className="conversation-card" key={c.id}>
            <div className="conversation-head">
              <span className="conversation-participants">
                {c.participants.map((id, i) => (
                  <span key={id}>
                    {i > 0 && <span aria-hidden="true"> · </span>}
                    <button className="link-btn" onClick={() => onSelectAgent(id)}>
                      {nameOf(id)}
                    </button>
                  </span>
                ))}
              </span>
              <span className="conversation-time">{formatTime(c.simTime)}</span>
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
    </aside>
  )
}
