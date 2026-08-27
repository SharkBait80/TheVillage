// TravelPath — draws the remaining route for a travelling agent as a soft
// dashed polyline from the agent's current Position to the destination
// (Req15.4). App only mounts this while the agent has a travel action + route;
// unmounting removes the line well within the 2s ceiling.

import { Polyline } from 'react-leaflet'
import type { LatLngExpression } from 'leaflet'
import type { StateAgent } from '../types'

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
  const here: LatLngExpression = [agent.lat, agent.lon]
  const progress = agent.action?.progress ?? 0
  // Determine how far along the polyline we are and keep the forward segment.
  const total = route.length - 1
  const scaled = Math.max(0, Math.min(total, progress * total))
  const nextIndex = Math.min(route.length - 1, Math.ceil(scaled))
  const ahead = route.slice(nextIndex).map((p) => [p.lat, p.lon] as LatLngExpression)
  return [here, ...ahead]
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
