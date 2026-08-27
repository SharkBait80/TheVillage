// ControlBar — issues start/pause/resume/stop to the Simulation_API (Req15.9).
// Only the controls valid for the currently displayed status are enabled. On a
// rejection (Req15.12) it surfaces the API's returned status + message via a
// toast and does NOT change the displayed status — the App only advances the
// displayed status from polled state.

import { useState } from 'react'
import { control, ApiRequestError } from '../api'
import type { ControlCommand, SimStatus } from '../types'

interface ControlBarProps {
  /** Currently displayed status (from polled state); null until first update. */
  status: SimStatus | null
  /** Called with the returned status when a command is accepted. */
  onAccepted?: (status: SimStatus) => void
}

/** Validity table mirrors Req2.7 / mock controller rules. */
function isEnabled(command: ControlCommand, status: SimStatus | null): boolean {
  if (status == null) return command === 'start'
  switch (command) {
    case 'start':
      return status === 'stopped'
    case 'pause':
      return status === 'running'
    case 'resume':
      return status === 'paused'
    case 'stop':
      return status === 'running' || status === 'paused'
  }
}

const COMMANDS: { cmd: ControlCommand; label: string; cls: string }[] = [
  { cmd: 'start', label: '▶ Start', cls: 'btn-start' },
  { cmd: 'pause', label: '⏸ Pause', cls: 'btn-pause' },
  { cmd: 'resume', label: '⏵ Resume', cls: 'btn-resume' },
  { cmd: 'stop', label: '■ Stop', cls: 'btn-stop' },
]

export function ControlBar({ status, onAccepted }: ControlBarProps) {
  const [busy, setBusy] = useState<ControlCommand | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  async function issue(cmd: ControlCommand) {
    setBusy(cmd)
    setToast(null)
    try {
      const res = await control(cmd)
      onAccepted?.(res.status)
    } catch (err) {
      // Req15.12: show returned status + message; DON'T change displayed status.
      if (err instanceof ApiRequestError) {
        const statusPart = err.status ? `Status: ${err.status}. ` : ''
        setToast(`${statusPart}${err.message}`)
      } else {
        setToast((err as Error).message || 'Command failed')
      }
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <div className="controls" role="group" aria-label="Simulation controls">
        {COMMANDS.map(({ cmd, label, cls }) => {
          const enabled = isEnabled(cmd, status) && busy == null
          return (
            <button
              key={cmd}
              className={`btn ${cls}`}
              disabled={!enabled}
              aria-disabled={!enabled}
              onClick={() => issue(cmd)}
            >
              {busy === cmd ? '…' : label}
            </button>
          )
        })}
      </div>
      {toast && (
        <div className="toast" role="alert">
          {toast}
          <button
            className="panel-close"
            style={{ position: 'static', marginLeft: 12, width: 24, height: 24 }}
            aria-label="Dismiss message"
            onClick={() => setToast(null)}
          >
            ×
          </button>
        </div>
      )}
    </>
  )
}
