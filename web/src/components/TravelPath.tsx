// TravelPath — draws the remaining route for a travelling agent as a soft
// dashed polyline from the agent's current Position to the destination
// (Req15.4). App only mounts this while the agent has a travel action + route;
// unmounting removes the line well within the 2s ceiling.

import { Polyline } from 'react-leaflet'
import type { LatLngExpression } from 'leaflet'
import type { StateAgent } from '../types'
import { isValidLatLon } from '../geo'

interface TravelPathProps {
  agent: StateAgent
}

/**
 * Build the remaining path: current position followed by the route coordinates
 * that lie ahead of the agent. We approximate "ahead" by dropping the route
 * points closest to (already passed near) the current position, then prefixing
 * the live position so the line always starts at the marker.
 */
function remainingPath(agent: StateAgent): LatLngExpression[] {
  const route = agent.route ?? agent.action?.route ?? []
  if (!route || route.length < 2) return []
  const progress = agent.action?.progress ?? 0
  // Determine how far along the polyline we are and keep the forward segment.
  const total = route.length - 1
  const scaled = Math.max(0, Math.min(total, progress * total))
  const nextIndex = Math.min(route.length - 1, Math.ceil(scaled))
  const ahead = route
    .slice(nextIndex)
    .filter((p) => isValidLatLon(p?.lat, p?.lon))
    .map((p) => [p.lat, p.lon] as LatLngExpression)
  // Only prefix the live position when it is a valid coordinate; otherwise the
  // path still renders from the remaining route points.
  const path: LatLngExpression[] = isValidLatLon(agent.lat, agent.lon)
    ? [[agent.lat, agent.lon] as LatLngExpression, ...ahead]
    : ahead
  return path
}

export function TravelPath({ agent }: TravelPathProps) {
  const path = remainingPath(agent)
  if (path.length < 2) return null
  return (
    <Polyline
      positions={path}
      pathOptions={{
        color: '#ff8fab',
        weight: 4,
        opacity: 0.85,
        dashArray: '2 10',
        lineCap: 'round',
      }}
    />
  )
}
