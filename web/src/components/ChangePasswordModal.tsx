// ChangePasswordModal — an accessible dialog letting the signed-in operator
// change their Cognito password. Collects current + new (+ confirm) passwords,
// validates client-side against the user pool's password policy (min 12 chars,
// upper, lower, digit, symbol), then calls auth.changePassword(). Cognito
// rejections (wrong current password, weak new password, reuse, throttling)
// surface as AuthError and are shown inline. On success it briefly confirms and
// closes; the existing session stays valid.

import { useEffect, useId, useRef, useState } from 'react'
import { AuthError, changePassword } from '../auth'

interface ChangePasswordModalProps {
  onClose: () => void
}

// Mirror the Cognito passwordPolicy configured in infra (village-stack.ts):
// minLength 12, requireLowercase/Uppercase/Digits/Symbols.
const MIN_LENGTH = 12

function describePolicyViolation(pw: string): string | null {
  if (pw.length < MIN_LENGTH) return `New password must be at least ${MIN_LENGTH} characters.`
  if (!/[a-z]/.test(pw)) return 'New password must include a lowercase letter.'
  if (!/[A-Z]/.test(pw)) return 'New password must include an uppercase letter.'
  if (!/[0-9]/.test(pw)) return 'New password must include a number.'
  // Cognito treats anything that is not a letter or digit as a symbol.
  if (!/[^A-Za-z0-9]/.test(pw)) return 'New password must include a symbol.'
  return null
}

export function ChangePasswordModal({ onClose }: ChangePasswordModalProps) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [done, setDone] = useState(false)

  const dialogRef = useRef<HTMLDivElement>(null)
  const firstInputRef = useRef<HTMLInputElement>(null)
  const titleId = useId()
  const errorId = useId()

  useEffect(() => {
    firstInputRef.current?.focus()
  }, [])

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.stopPropagation()
      onClose()
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (submitting) return
    setError(null)

    if (!current) {
      setError('Enter your current password.')
      return
    }
    const policy = describePolicyViolation(next)
    if (policy) {
      setError(policy)
      return
    }
    if (next !== confirm) {
      setError('New password and confirmation do not match.')
      return
    }
    if (next === current) {
      setError('New password must be different from the current password.')
      return
    }

    setSubmitting(true)
    try {
      await changePassword(current, next)
      setDone(true)
      window.setTimeout(onClose, 1500)
    } catch (err) {
      const msg = err instanceof AuthError ? err.message : 'Failed to change password.'
      setError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
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
        <button className="panel-close" onClick={onClose} aria-label="Close change password dialog">
          ×
        </button>
        <h2 id={titleId}>Change password</h2>
        <p className="subtitle">
          Update the password for your operator account. You will stay signed in after changing it.
        </p>

        {done ? (
          <div className="modal-success" role="status">
            <p className="modal-success-head">Password changed</p>
            <p className="muted small">Your new password is now active.</p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="modal-form">
            <label className="field-label" htmlFor={`${titleId}-current`}>
              Current password
            </label>
            <input
              id={`${titleId}-current`}
              ref={firstInputRef}
              className="login-input"
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              aria-invalid={!!error}
              aria-describedby={error ? errorId : undefined}
              required
            />

            <label className="field-label" htmlFor={`${titleId}-next`}>
              New password
            </label>
            <input
              id={`${titleId}-next`}
              className="login-input"
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              required
            />

            <label className="field-label" htmlFor={`${titleId}-confirm`}>
              Confirm new password
            </label>
            <input
              id={`${titleId}-confirm`}
              className="login-input"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
            />

            <p className="muted small">
              At least {MIN_LENGTH} characters, including an uppercase and lowercase letter, a number
              and a symbol.
            </p>

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
                {submitting ? 'Changing…' : 'Change password'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
