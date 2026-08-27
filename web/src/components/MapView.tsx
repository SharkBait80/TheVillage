// MapView — the Leaflet map bounded to Melbourne (Req15.1 / Req3.3). Hosts
// Location markers, Agent markers (Req15.2), travel paths (Req15.4), and
// conversation indicators (Req15.5). Positions/paths/indicators come from the
// most recent SimState; App holds the last-known state during pause / connection
// loss (Req15.14 / Req15.11) so the map simply renders whatever it is given.

import { MapContainer, TileLayer, useMapEvents } from 'react-leaflet'
import L from 'leaflet'
import type { LocationItem, SimState, StateAgent } from '../types'
import { AgentMarker } from './AgentMarker'
import { LocationMarker } from './LocationMarker'
import { TravelPath } from './TravelPath'
import { ConversationIndicator } from './ConversationIndicator'
import { isValidLatLon } from '../geo'

// Melbourne map bounds (Req3.3): lat [-38.00, -37.70], lon [144.85, 145.10].
const BOUNDS = L.latLngBounds([-38.0, 144.85], [-37.7, 145.1])
const CENTER: [number, number] = [-37.8136, 144.9631]

interface MapViewProps {
  state: SimState | null
  locations: LocationItem[]
  selectedAgentId: string | null
  selectedLocationId: string | null
  onSelectAgent: (agentId: string) => void
  onSelectLocation: (locId: string) => void
  /**
   * When provided (i.e. "add event" mode is armed), a click anywhere on the map
   * reports the clicked coordinate so the operator can place an event there.
   * When undefined, clicks fall through to normal pan/marker behaviour.
   */
  onMapClick?: (latlng: { lat: number; lon: number }) => void
}

/**
 * Invisible child of MapContainer that forwards map clicks to `onMapClick`.
 * Rendered only while add-event mode is armed so ordinary panning and marker
 * selection are unaffected the rest of the time.
 */
function MapClickCapture({ onMapClick }: { onMapClick: (latlng: { lat: number; lon: number }) => void }) {
  useMapEvents({
    click(e) {
      onMapClick({ lat: e.latlng.lat, lon: e.latlng.lng })
    },
  })
  return null
}

function isTravelling(agent: StateAgent): boolean {
  const route = agent.route ?? agent.action?.route
  return agent.action?.type === 'travel' && Array.isArray(route) && route.length >= 2
}

export function MapView({
  state,
  locations,
  selectedAgentId,
  selectedLocationId,
  onSelectAgent,
  onSelectLocation,
  onMapClick,
}: MapViewProps) {
  const agents = state?.agents ?? []
  const conversations = state?.conversations ?? []
  const agentsById = new Map(agents.map((a) => [a.id, a]))

  return (
    <div className={`map-wrap${onMapClick ? ' map-arm-add' : ''}`}>
      <MapContainer
        center={CENTER}
        zoom={13}
        minZoom={12}
        maxZoom={18}
        maxBounds={BOUNDS}
        maxBoundsViscosity={1.0}
        style={{ height: '100%', width: '100%' }}
        zoomControl
      >
        {onMapClick && <MapClickCapture onMapClick={onMapClick} />}

        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />

        {/* Locations first so agents render on top. Skip any without a valid
            coordinate so one malformed record can't crash the map. */}
        {locations
          .filter((loc) => isValidLatLon(loc.lat, loc.lon))
          .map((loc) => (
            <LocationMarker
              key={loc.id}
              location={loc}
              selected={loc.id === selectedLocationId}
              onSelect={onSelectLocation}
            />
          ))}

        {/* Travel paths for currently-travelling agents (Req15.4). */}
        {agents.filter(isTravelling).map((a) => (
          <TravelPath key={`path-${a.id}`} agent={a} />
        ))}

        {/* Conversation indicators (Req15.5). */}
        {conversations.map((c, i) => (
          <ConversationIndicator
            key={`conv-${c.participants.join('-')}-${i}`}
            conversation={c}
            agentsById={agentsById}
          />
        ))}

        {/* Agent markers, individually selectable even when overlapping (Req15.2).
            Skip any without a valid coordinate to avoid Leaflet LatLng errors. */}
        {agents
          .filter((a) => isValidLatLon(a.lat, a.lon))
          .map((a, i) => (
            <AgentMarker
              key={a.id}
              agent={a}
              index={i}
              selected={a.id === selectedAgentId}
              onSelect={onSelectAgent}
            />
          ))}
      </MapContainer>
    </div>
  )
}
