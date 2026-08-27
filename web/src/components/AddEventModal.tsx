// AddEventModal — an accessible dialog for operators to inject a world event at
// a map-clicked coordinate. Collects a title, description, scale, severity and
// (for local events) an optional radius, validates client-side, then POSTs via
// createEvent(). Content rejections surface as ApiRequestError and are shown
// inline. On success it shows a brief confirmation (including the moderation
// verdict) and closes.

import { useEffect, useId, useRef, useState } from 'react'
import { createEvent, ApiRequestError } from '../api'
import type { CreateEventResult, EventScale, EventSeverity } from '../types'

// Melbourne map bounds — must match the API + mock validators.
const LAT_MIN = -38.0
const LAT_MAX = -37.7
const LON_MIN = 144.85
const LON_MAX = 145.1

interface AddEventModalProps {
  lat: number
  lon: number
  onClose: () => void
}

const SCALES: { value: EventScale; label: string }[] = [
  { value: 'local', label: 'Local' },
  { value: 'city', label: 'City-wide' },
  { value: 'wide', label: 'Wide area' },
]

const SEVERITIES: { value: EventSeverity; label: string }[] = [
  { value: 'info', label: 'Info' },
  { value: 'minor', label: 'Minor' },
  { value: 'major', label: 'Major' },
  { value: 'severe', label: 'Severe' },
]

export function AddEventModal({ lat, lon, onClose }: AddEventModalProps) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [scale, setScale] = useState<EventScale>('local')
  const [severity, setSeverity] = useState<EventSeverity>('minor')
  const [radiusM, setRadiusM] = useState<string>('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState<CreateEventResult | null>(null)

  const dialogRef = useRef<HTMLDivElement>(null)
  const titleInputRef = useRef<HTMLInputElement>(null)
  const titleId = useId()
  const errorId = useId()

  // Focus the first field on open (basic focus management for a11y).
  useEffect(() => {
    titleInputRef.current?.focus()
  }, [])

  // Escape closes the dialog.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.stopPropagation()
      onClose()
    }
  }

  const inBounds = lat >= LAT_MIN && lat <= LAT_MAX && lon >= LON_MIN && lon <= LON_MAX

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)

    const t = title.trim()
    const d = description.trim()
    if (t.length < 1 || t.length > 120) {
      setError('Title must be between 1 and 120 characters.')
      return
    }
    if (d.length < 1 || d.length > 1000) {
      setError('Description must be between 1 and 1000 characters.')
      return
    }
    if (!inBounds) {
      setError('The picked point is outside the Melbourne map bounds.')
      return
    }
    let radius: number | undefined
    if (scale === 'local' && radiusM.trim() !== '') {
      const parsed = Number(radiusM)
      if (Number.isNaN(parsed) || parsed <= 0) {
        setError('Radius must be a positive number of metres.')
        return
      }
      radius = parsed
    }

    setSubmitting(true)
    try {
      const res = await createEvent({
        title: t,
        description: d,
        lat,
        lon,
        scale,
        severity,
        ...(radius != null ? { radiusM: radius } : {}),
      })
      setResult(res)
      // Briefly show the confirmation, then close.
      window.setTimeout(onClose, 1500)
    } catch (err) {
      if (err instanceof ApiRequestError) {
        setError(err.message)
      } else {
        setError((err as Error).message || 'Failed to add event.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        // Click on the backdrop (not the dialog) closes.
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        className="modal-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={dialogRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <button className="panel-close" onClick={onClose} aria-label="Close add event dialog">
          ×
        </button>
        <h2 id={titleId}>Add event</h2>
        <p className="subtitle">
          Inject a world event at the point you picked. The simulation will moderate it before
          it takes effect.
        </p>

        {result ? (
          <div className="modal-success" role="status">
            <p className="modal-success-head">
              {result.accepted ? 'Event accepted' : 'Event recorded'}
            </p>
            <p className="muted small">
              {result.verdict.reason
                ? `Verdict: ${result.verdict.reason}`
                : 'The event has been added to the world.'}
            </p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="modal-form">
            <label className="field-label" htmlFor={`${titleId}-title`}>
              Title
            </label>
            <input
              id={`${titleId}-title`}
              ref={titleInputRef}
              className="login-input"
              type="text"
              maxLength={120}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              aria-invalid={!!error}
              aria-describedby={error ? errorId : undefined}
              required
            />

            <label className="field-label" htmlFor={`${titleId}-desc`}>
              Description
            </label>
            <textarea
              id={`${titleId}-desc`}
              className="login-input modal-textarea"
              maxLength={1000}
              rows={4}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              required
            />

            <div className="modal-row">
              <div className="modal-col">
                <label className="field-label" htmlFor={`${titleId}-scale`}>
                  Scale
                </label>
                <select
                  id={`${titleId}-scale`}
                  className="login-input"
                  value={scale}
                  onChange={(e) => setScale(e.target.value as EventScale)}
                >
                  {SCALES.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="modal-col">
                <label className="field-label" htmlFor={`${titleId}-sev`}>
                  Severity
                </label>
                <select
                  id={`${titleId}-sev`}
                  className="login-input"
                  value={severity}
                  onChange={(e) => setSeverity(e.target.value as EventSeverity)}
                >
                  {SEVERITIES.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {scale === 'local' && (
              <>
                <label className="field-label" htmlFor={`${titleId}-radius`}>
                  Radius (m) — optional
                </label>
                <input
                  id={`${titleId}-radius`}
                  className="login-input"
                  type="number"
                  min={1}
                  value={radiusM}
                  onChange={(e) => setRadiusM(e.target.value)}
                />
              </>
            )}

            <div className="modal-coords" aria-label="Picked coordinates">
              <span className="field-label">Location</span>
              <span className="hud-value">
                {lat.toFixed(5)}, {lon.toFixed(5)}
              </span>
            </div>

            {error && (
              <p className="login-error" role="alert" id={errorId}>
                {error}
              </p>
            )}

            <div className="modal-actions">
              <button type="button" className="btn btn-toggle" onClick={onClose} disabled={submitting}>
                Cancel
              </button>
              <button type="submit" className="btn btn-start" disabled={submitting}>
                {submitting ? 'Adding…' : 'Add event'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
