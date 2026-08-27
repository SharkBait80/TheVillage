// LocationMarker — renders a Location with its generated artwork (or category
// placeholder, Req16.7) plus its display name label (Req15.1). Selectable to
// open the LocationPanel (Req15.7).
//
// The artwork lives behind a Cognito-protected /assets route that 302-redirects
// to a presigned S3 URL, so it cannot be loaded via a bare <img src>. We use
// useAuthImage to fetch it with the bearer token and swap the DivIcon's <img>
// from the placeholder to the resulting blob URL once it loads.

import { useMemo } from 'react'
import { Marker } from 'react-leaflet'
import L from 'leaflet'
import type { LocationItem } from '../types'
import { locationPlaceholder } from '../placeholders'
import { useAuthImage } from '../useAuthImage'

interface LocationMarkerProps {
  location: LocationItem
  selected: boolean
  onSelect: (locId: string) => void
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function buildIcon(location: LocationItem, imgSrc: string, placeholder: string): L.DivIcon {
  const alt = `${location.name} (${location.category})`
  const html = `
    <div class="loc-marker-inner">
      <img
        class="village-marker marker-loc"
        width="44" height="44"
        src="${imgSrc}"
        alt="${escapeHtml(alt)}"
        onerror="this.onerror=null;this.src='${placeholder}'"
      />
      <span class="marker-label">${escapeHtml(location.name)}</span>
    </div>`
  return L.divIcon({
    html,
    className: 'loc-marker-wrap',
    iconSize: [44, 60],
    iconAnchor: [22, 22],
  })
}

export function LocationMarker({ location, selected, onSelect }: LocationMarkerProps) {
  const placeholder = locationPlaceholder(location.category)
  const { src } = useAuthImage(location.id, placeholder)
  const icon = useMemo(
    () => buildIcon(location, src, placeholder),
    [location.id, location.name, location.category, src, placeholder],
  )

  return (
    <Marker
      position={[location.lat, location.lon]}
      icon={icon}
      zIndexOffset={selected ? 500 : -100}
      keyboard
      title={location.name}
      alt={location.name}
      eventHandlers={{
        click: () => onSelect(location.id),
        keypress: (e) => {
          const oe = e.originalEvent as KeyboardEvent
          if (oe.key === 'Enter' || oe.key === ' ') onSelect(location.id)
        },
      }}
    />
  )
}
