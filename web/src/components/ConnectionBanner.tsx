// ConnectionBanner — connection-lost notice (Req15.11). App renders this while
// the polling hook reports `connectionLost`. It shows the Simulated_Time of the
// last received update. The last Agent Positions are retained on the map because
// App keeps rendering the last SimState. Retry cadence (every 5s) and clearing
// within 2s on success are handled by usePolling; App simply stops rendering
// this banner once the hook reports recovery.

interface ConnectionBannerProps {
  lastSimTime: string | null
}

function formatSimTime(iso: string | null): string {
  if (!iso) return 'unknown'
  try {
    return new Date(iso).toLocaleString('en-AU', {
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

export function ConnectionBanner({ lastSimTime }: ConnectionBannerProps) {
  return (
    <div className="conn-banner" role="alert" aria-live="assertive">
      <span role="img" aria-hidden="true">
        📡
      </span>
      <span>
        Connection lost — retrying every 5 seconds. Showing the village as of{' '}
        <strong>{formatSimTime(lastSimTime)}</strong> (simulated time).
      </span>
    </div>
  )
}
