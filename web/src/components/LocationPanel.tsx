// LocationPanel — full location detail (Req15.7): display name, category,
// opening hours, capacity, current status (closed/at_capacity/open), and the
// Persona name of every Agent present. Fetches GET /v1/sim/{simId}/locations/{id}
// on selection and refreshes while open.

import { useEffect, useRef, useState } from 'react'
import { getLocation } from '../api'
import { locationPlaceholder } from '../placeholders'
import { useAuthImage } from '../useAuthImage'
import type { LocationDetail } from '../types'

interface LocationPanelProps {
  locationId: string
  onClose: () => void
  onSelectAgent: (agentId: string) => void
}

const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

const STATUS_LABEL: Record<string, string> = {
  open: 'Open',
  closed: 'Closed',
  at_capacity: 'At capacity',
}

export function LocationPanel({ locationId, onClose, onSelectAgent }: LocationPanelProps) {
  const [detail, setDetail] = useState<LocationDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let active = true
    const ac = new AbortController()
    async function load() {
      try {
        const d = await getLocation(locationId, ac.signal)
        if (active) {
          setDetail(d)
          setError(null)
        }
      } catch (err) {
        if ((err as Error)?.name === 'AbortError') return
        if (active) setError((err as Error).message || 'Failed to load location')
      }
    }
    void load()
    const timer = window.setInterval(load, 2000)
    return () => {
      active = false
      ac.abort()
      clearInterval(timer)
    }
  }, [locationId])

  useEffect(() => {
    panelRef.current?.focus()
  }, [locationId])

  // Placeholder art depends on the (async) category; fall back to civic until
  // the detail loads. The hook always yields a usable src (placeholder while
  // loading / on failure).
  const placeholder = locationPlaceholder(detail?.category ?? 'civic')
  const art = useAuthImage(locationId, placeholder)

  return (
    <aside
      className="panel"
      role="dialog"
      aria-label="Location details"
      tabIndex={-1}
      ref={panelRef}
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
      }}
    >
      <button className="panel-close" onClick={onClose} aria-label="Close location details">
        ×
      </button>

      {error && <p role="alert">{error}</p>}
      {!detail && !error && <p>Loading location…</p>}

      {detail && (
        <>
          <img
            className="panel-portrait"
            src={art.src}
            width={72}
            height={72}
            alt={`Artwork of ${detail.name}`}
            onError={(e) => {
              const img = e.currentTarget
              img.onerror = null
              img.src = locationPlaceholder(detail.category)
            }}
          />
          <h2>{detail.name}</h2>
          <p className="subtitle">{detail.category}</p>
          <div style={{ clear: 'both' }} />

          <div className="kv">
            <span className="k">Status</span>
            <span className="v">{STATUS_LABEL[detail.status] ?? detail.status}</span>
          </div>
          <div className="kv">
            <span className="k">Capacity</span>
            <span className="v">{detail.capacity}</span>
          </div>

          <div className="section-title">Opening hours</div>
          <table className="listview" role="table" style={{ position: 'static', width: '100%', boxShadow: 'none', padding: 0 }}>
            <tbody>
              {detail.hours.map((h, i) => (
                <tr key={i}>
                  <th scope="row">{DAY_LABELS[i] ?? `Day ${i + 1}`}</th>
                  <td>
                    {h.open} – {h.close}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="section-title">
            Present agents ({detail.presentAgents.length})
          </div>
          {detail.presentAgents.length === 0 ? (
            <p>No one here right now.</p>
          ) : (
            <ul className="event-list">
              {detail.presentAgents.map((a) => (
                <li className="event-item" key={a.id}>
                  <button
                    className="btn btn-toggle"
                    style={{ width: '100%', textAlign: 'left' }}
                    onClick={() => onSelectAgent(a.id)}
                  >
                    {a.name}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </aside>
  )
}
