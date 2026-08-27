import type { SimStatus } from '../types'

interface HudProps {
  simTime: string | null
  accel: number | null
  status: SimStatus | null
}

function formatSimTime(iso: string | null): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    // Display in Melbourne local wall-clock reading from the ISO string.
    return d.toLocaleString('en-AU', {
      weekday: 'short',
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

const STATUS_LABEL: Record<SimStatus, string> = {
  running: 'Running',
  paused: 'Paused',
  stopped: 'Stopped',
}

/** Header/HUD — Simulated_Time, Acceleration_Factor, status (Req15.8). */
export function Hud({ simTime, accel, status }: HudProps) {
  return (
    <>
      <div className="brand">
        <span className="sparkle" aria-hidden="true">
          MV
        </span>
        Melbourne Agent Village
      </div>
      <div className="hud-stats" role="group" aria-label="Simulation status">
        <div className="hud-chip">
          <span className="hud-label">Simulated Time</span>
          <span className="hud-value" aria-live="polite">
            {formatSimTime(simTime)}
          </span>
        </div>
        <div className="hud-chip">
          <span className="hud-label">Acceleration</span>
          <span className="hud-value">{accel != null ? `${accel}×` : '—'}</span>
        </div>
        <div className="hud-chip">
          <span className="hud-label">Status</span>
          <span
            className={`status-pill ${status ? `status-${status}` : 'status-stopped'}`}
            aria-live="polite"
          >
            {status ? STATUS_LABEL[status] : '—'}
          </span>
        </div>
      </div>
    </>
  )
}
