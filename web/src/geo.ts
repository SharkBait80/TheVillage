// Coordinate guards for Leaflet geometry.
//
// The API delivers agent/location positions and route points sourced from
// DynamoDB. Records can legitimately be missing `lat`/`lon` (e.g. an agent whose
// state hasn't been fully initialised, or a route point that failed to
// serialise), in which case the JSON carries `null` and the SPA sees
// `undefined`. Leaflet throws "Invalid LatLng object: (undefined, undefined)"
// the moment such a value reaches a Marker/Polyline, which previously crashed
// the entire map. These helpers let callers skip invalid geometry instead.

import type { LatLngExpression } from 'leaflet'

/** True only for a real, finite number (rejects undefined/null/NaN/Infinity). */
export function isFiniteNum(n: unknown): n is number {
  return typeof n === 'number' && Number.isFinite(n)
}

/** True if both lat and lon are finite numbers. */
export function isValidLatLon(lat: unknown, lon: unknown): boolean {
  return isFiniteNum(lat) && isFiniteNum(lon)
}

/**
 * Return `[lat, lon]` as a Leaflet LatLngExpression when both are finite,
 * otherwise `null` so callers can filter it out.
 */
export function toLatLng(lat: unknown, lon: unknown): LatLngExpression | null {
  return isValidLatLon(lat, lon) ? ([lat, lon] as LatLngExpression) : null
}
