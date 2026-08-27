// ConversationIndicator — links the markers of conversing agents with a soft
// line and floats a chat/heart bubble at the midpoint (Req15.5). App only
// mounts this while the conversation is present in state; unmounting removes it
// within the 2s ceiling.

import { Polyline, Marker } from 'react-leaflet'
import L from 'leaflet'
import type { LatLngExpression } from 'leaflet'
import type { StateAgent, StateConversation } from '../types'
import { isValidLatLon } from '../geo'

interface ConversationIndicatorProps {
  conversation: StateConversation
  agentsById: Map<string, StateAgent>
}

function midpoint(points: LatLngExpression[]): LatLngExpression | null {
  if (points.length === 0) return null
  let lat = 0
  let lon = 0
  for (const p of points) {
    const [la, lo] = p as [number, number]
    lat += la
    lon += lo
  }
  return [lat / points.length, lon / points.length]
}

const bubbleIcon = L.divIcon({
  html: '<span class="chat-bubble" role="img" aria-label="agents conversing">💬</span>',
  className: 'conv-bubble-wrap',
  iconSize: [24, 24],
  iconAnchor: [12, 24],
})

export function ConversationIndicator({ conversation, agentsById }: ConversationIndicatorProps) {
  const points: LatLngExpression[] = conversation.participants
    .map((id) => agentsById.get(id))
    .filter((a): a is StateAgent => Boolean(a))
    .filter((a) => isValidLatLon(a.lat, a.lon))
    .map((a) => [a.lat, a.lon] as LatLngExpression)

  if (points.length < 2) return null
  const mid = midpoint(points)

  return (
    <>
      <Polyline
        positions={points}
        pathOptions={{ color: '#c8b6ff', weight: 3, opacity: 0.9, dashArray: '6 6' }}
      />
      {mid && <Marker position={mid} icon={bubbleIcon} interactive={false} />}
    </>
  )
}
