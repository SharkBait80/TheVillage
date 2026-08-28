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

/** Build a DivIcon for a location.
 *
 * Constructed via DOM APIs (element properties, `textContent` for the label)
 * rather than an interpolated HTML string, so name/category/src values cannot
 * break out into markup or a JS handler context — structurally injection-proof.
 */
function buildIcon(location: LocationItem, imgSrc: string, placeholder: string): L.DivIcon {
  const alt = `${location.name} (${location.category})`

  const inner = document.createElement('div')
  inner.className = 'loc-marker-inner'

  const img = document.createElement('img')
  img.className = 'village-marker marker-loc'
  img.width = 44
  img.height = 44
  img.src = imgSrc
  img.alt = alt
  img.onerror = () => {
    img.onerror = null
    img.src = placeholder
  }

  const label = document.createElement('span')
  label.className = 'marker-label'
  label.textContent = location.name

  inner.appendChild(img)
  inner.appendChild(label)

  return L.divIcon({
    html: inner,
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
