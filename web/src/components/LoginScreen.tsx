// LoginScreen — a small, friendly sign-in card matching the pastel storybook
// theme. Shown by App when there is no valid Cognito token (and not in mock
// mode). On submit it calls auth.login(); on success App re-renders the app.

import { useState, type FormEvent } from 'react'
import { AuthError, login } from '../auth'

interface LoginScreenProps {
  onSuccess: () => void
  /** Optional note shown above the form (e.g. after auto-login failed). */
  notice?: string | null
}

export function LoginScreen({ onSuccess, notice }: LoginScreenProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await login(username.trim(), password)
      onSuccess()
    } catch (err) {
      const msg = err instanceof AuthError ? err.message : 'Sign-in failed. Please try again.'
      setError(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={handleSubmit} aria-labelledby="login-title">
        <div className="login-emoji" aria-hidden="true">
          🏘️✨
        </div>
        <h1 id="login-title" className="login-title">
          Melbourne Agent Village
        </h1>
        <p className="login-sub">Sign in to peek into the village</p>

        {notice && (
          <p className="login-notice" role="status">
            {notice}
          </p>
        )}

        <label className="login-label" htmlFor="login-username">
          Username
        </label>
        <input
          id="login-username"
          className="login-input"
          type="text"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          disabled={busy}
          required
          autoFocus
        />

        <label className="login-label" htmlFor="login-password">
          Password
        </label>
        <input
          id="login-password"
          className="login-input"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={busy}
          required
        />

        {error && (
          <p className="login-error" role="alert">
            {error}
          </p>
        )}

        <button className="btn btn-login" type="submit" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in 🌸'}
        </button>
      </form>
    </div>
  )
}
