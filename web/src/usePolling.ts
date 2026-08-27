// Polling + connection-health hook.
//
// Satisfies:
//  - Req15.3: poll state at least every 2s; never surface a position older than 4s.
//  - Req15.11: after 3 consecutive failures OR 10s with no success, report
//    connection-lost, keep last data, retry every 5s.
//  - Req15.13: on a successful retry, clear the lost state within 2s (next tick).
//  - Req15.14: while paused, keep last data (we simply keep the last SimState).

import { useCallback, useEffect, useRef, useState } from 'react'
import { getState } from './api'
import type { SimState } from './types'

const DEFAULT_POLL_MS = 1500
const RETRY_MS = 5000
const STALE_MS = 4000 // Req15.3 freshness ceiling
const LOST_AFTER_FAILS = 3 // Req15.11
const LOST_AFTER_MS = 10_000 // Req15.11

export interface ConnectionState {
  /** Most recent successfully fetched state (retained on failure per R15.11). */
  state: SimState | null
  /** True once we've crossed the connection-lost threshold. */
  connectionLost: boolean
  /** SimTime of the last successful update (shown in the lost notice). */
  lastSimTime: string | null
  /** Real epoch (ms) of the last successful update — used for staleness. */
  lastSuccessMs: number | null
  /** Whether the most recently rendered position is stale (>4s old). */
  stale: boolean
}

const rawPoll = Number(import.meta.env.VITE_POLL_MS)
const POLL_MS = Number.isFinite(rawPoll) && rawPoll > 0 ? Math.min(rawPoll, 2000) : DEFAULT_POLL_MS

export function usePolling(): ConnectionState {
  const [conn, setConn] = useState<ConnectionState>({
    state: null,
    connectionLost: false,
    lastSimTime: null,
    lastSuccessMs: null,
    stale: false,
  })

  const failCountRef = useRef(0)
  const lastSuccessMsRef = useRef<number | null>(null)
  const firstFailMsRef = useRef<number | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const timerRef = useRef<number | null>(null)
  const mountedRef = useRef(true)

  const tick = useCallback(async () => {
    abortRef.current?.abort()
    const ac = new AbortController()
    abortRef.current = ac
    try {
      const state = await getState(ac.signal)
      if (!mountedRef.current) return
      const now = Date.now()
      failCountRef.current = 0
      firstFailMsRef.current = null
      lastSuccessMsRef.current = now
      setConn({
        state,
        connectionLost: false, // R15.13: cleared on success
        lastSimTime: state.simTime,
        lastSuccessMs: now,
        stale: false,
      })
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') return
      if (!mountedRef.current) return
      failCountRef.current += 1
      const now = Date.now()
      if (firstFailMsRef.current == null) firstFailMsRef.current = now
      const elapsedSinceSuccess =
        lastSuccessMsRef.current != null ? now - lastSuccessMsRef.current : now - (firstFailMsRef.current ?? now)
      const lost =
        failCountRef.current >= LOST_AFTER_FAILS ||
        (lastSuccessMsRef.current != null && elapsedSinceSuccess >= LOST_AFTER_MS) ||
        (lastSuccessMsRef.current == null && elapsedSinceSuccess >= LOST_AFTER_MS)
      setConn((prev) => ({
        ...prev, // retain last state + lastSimTime (R15.11)
        connectionLost: lost || prev.connectionLost,
      }))
    } finally {
      if (mountedRef.current) {
        // Retry cadence: 5s while lost, otherwise normal poll interval.
        const lost = failCountRef.current >= LOST_AFTER_FAILS
        const delay = lost ? RETRY_MS : POLL_MS
        timerRef.current = window.setTimeout(tick, delay)
      }
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void tick()
    // Separate lightweight timer to flag staleness (R15.3) between polls.
    const staleTimer = window.setInterval(() => {
      if (!mountedRef.current) return
      setConn((prev) => {
        if (prev.lastSuccessMs == null) return prev
        const isStale = Date.now() - prev.lastSuccessMs > STALE_MS
        if (isStale === prev.stale) return prev
        return { ...prev, stale: isStale }
      })
    }, 1000)
    return () => {
      mountedRef.current = false
      abortRef.current?.abort()
      if (timerRef.current != null) clearTimeout(timerRef.current)
      clearInterval(staleTimer)
    }
  }, [tick])

  return conn
}
