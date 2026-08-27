// ControlBar — issues start/pause/resume/stop to the Simulation_API (Req15.9).
// Only the controls valid for the currently displayed status are enabled. On a
// rejection (Req15.12) it surfaces the API's returned status + message via a
// toast and does NOT change the displayed status — the App only advances the
// displayed status from polled state.

import { useState } from 'react'
import { control, reseed, ApiRequestError } from '../api'
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
  { cmd: 'start', label: 'Start', cls: 'btn-start' },
  { cmd: 'pause', label: 'Pause', cls: 'btn-pause' },
  { cmd: 'resume', label: 'Resume', cls: 'btn-resume' },
  { cmd: 'stop', label: 'Stop', cls: 'btn-stop' },
]

export function ControlBar({ status, onAccepted }: ControlBarProps) {
  const [busy, setBusy] = useState<ControlCommand | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [confirmReseed, setConfirmReseed] = useState(false)
  const [reseeding, setReseeding] = useState(false)

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

  async function doReseed() {
    setReseeding(true)
    setToast(null)
    try {
      await reseed()
      setConfirmReseed(false)
      setToast('World delete + re-seed started. Fresh agents will appear shortly.')
    } catch (err) {
      if (err instanceof ApiRequestError) {
        setToast(err.message)
      } else {
        setToast((err as Error).message || 'Reseed failed')
      }
    } finally {
      setReseeding(false)
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
        <button
          className="btn btn-stop"
          disabled={busy != null || reseeding}
          aria-disabled={busy != null || reseeding}
          onClick={() => setConfirmReseed(true)}
          title="Delete the world and generate a fresh population"
        >
          Reseed
        </button>
      </div>

      {confirmReseed && (
        <div
          className="modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="reseed-title"
        >
          <div className="modal-dialog">
            <h3 id="reseed-title">Delete and re-seed the world?</h3>
            <p className="subtitle">
              This permanently deletes <strong>all</strong> current agents,
              events, conversations and simulation state, then generates a brand
              new population with freshly written biographies, personalities and
              portraits. This cannot be undone.
            </p>
            <div className="controls" style={{ marginTop: 16 }}>
              <button
                className="btn"
                disabled={reseeding}
                onClick={() => setConfirmReseed(false)}
              >
                Cancel
              </button>
              <button
                className="btn btn-stop"
                disabled={reseeding}
                onClick={doReseed}
              >
                {reseeding ? 'Reseeding…' : 'Delete & re-seed'}
              </button>
            </div>
          </div>
        </div>
      )}
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
