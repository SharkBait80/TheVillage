// AgentMarker — one selectable marker per agent (Req15.2). Uses the agent's
// generated portrait when the API provides one, otherwise the cute placeholder
// (Req16.7). Kept individually selectable even when two agents share a Position
// by giving each its own Leaflet marker with a per-agent zIndexOffset.
//
// The portrait lives behind a Cognito-protected /assets route that 302-redirects
// to a presigned S3 URL, so it cannot be loaded via a bare <img src>. We use
// useAuthImage to fetch it with the bearer token and swap the DivIcon's <img>
// from the placeholder to the resulting blob URL once it loads.

import { useMemo } from 'react'
import { Marker } from 'react-leaflet'
import L from 'leaflet'
import type { StateAgent } from '../types'
import { agentPlaceholder } from '../placeholders'
import { useAuthImage } from '../useAuthImage'

interface AgentMarkerProps {
  agent: StateAgent
  index: number
  selected: boolean
  onSelect: (agentId: string) => void
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** Build a DivIcon whose <img> uses the resolved (authenticated) image src. */
function buildIcon(agent: StateAgent, imgSrc: string, placeholder: string): L.DivIcon {
  const legalClass =
    agent.legal === 'suspected'
      ? 'legal-suspected'
      : agent.legal === 'charged' || agent.legal === 'detained'
        ? 'legal-charged'
        : ''
  // Alt text (Req15.10 non-text content): agent name + legal status.
  const alt = `${agent.name}${agent.legal !== 'clear' ? ` (${agent.legal})` : ''}`
  const html = `<img
      class="village-marker marker-agent ${legalClass}"
      width="40" height="40"
      src="${imgSrc}"
      alt="${escapeHtml(alt)}"
      onerror="this.onerror=null;this.src='${placeholder}'"
    />`
  return L.divIcon({
    html,
    className: 'agent-marker-wrap',
    iconSize: [40, 40],
    iconAnchor: [20, 20],
  })
}

export function AgentMarker({ agent, index, selected, onSelect }: AgentMarkerProps) {
  const placeholder = agentPlaceholder()
  const { src } = useAuthImage(agent.id, placeholder)
  const icon = useMemo(
    () => buildIcon(agent, src, placeholder),
    [agent.id, agent.legal, agent.name, src, placeholder],
  )

  return (
    <Marker
      position={[agent.lat, agent.lon]}
      icon={icon}
      // Overlapping agents remain individually selectable: stagger z-order and
      // raise the selected one to the top (Req15.2).
      zIndexOffset={selected ? 1000 : index}
      keyboard
      title={agent.name}
      alt={agent.name}
      eventHandlers={{
        click: () => onSelect(agent.id),
        keypress: (e) => {
          const oe = e.originalEvent as KeyboardEvent
          if (oe.key === 'Enter' || oe.key === ' ') onSelect(agent.id)
        },
      }}
    />
  )
}
