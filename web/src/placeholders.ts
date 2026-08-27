// Cute inline-SVG placeholder art (Req16.7). Used when the Simulation_API
// returns no image for a subject. Encoded as data URLs so they need no network.
//
// One friendly placeholder per Location category, and one per "agent".
// Kept small and pastel to match the storybook theme.

import type { LocationCategory } from './types'

function svg(body: string, bg: string): string {
  const doc = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
    <rect width="64" height="64" rx="16" fill="${bg}"/>
    ${body}
  </svg>`
  return `data:image/svg+xml;utf8,${encodeURIComponent(doc)}`
}

// A soft round face for agents.
const AGENT_PLACEHOLDER = svg(
  `<circle cx="32" cy="28" r="15" fill="#fff" opacity="0.9"/>
   <circle cx="26" cy="27" r="2.4" fill="#5b4b6b"/>
   <circle cx="38" cy="27" r="2.4" fill="#5b4b6b"/>
   <path d="M25 34 Q32 40 39 34" stroke="#5b4b6b" stroke-width="2.4" fill="none" stroke-linecap="round"/>
   <circle cx="22" cy="32" r="2.6" fill="#ffb3c6" opacity="0.7"/>
   <circle cx="42" cy="32" r="2.6" fill="#ffb3c6" opacity="0.7"/>`,
  '#c8b6ff',
)

// Category-specific emoji-like glyphs on pastel backgrounds.
const LOCATION_PLACEHOLDERS: Record<LocationCategory, string> = {
  residence: svg(
    `<path d="M16 32 L32 18 L48 32 V48 H16 Z" fill="#fff" opacity="0.92"/>
     <rect x="28" y="38" width="8" height="10" fill="#ffd6a5"/>
     <path d="M14 33 L32 17 L50 33" stroke="#ff8fab" stroke-width="3" fill="none" stroke-linecap="round"/>`,
    '#ffc8dd',
  ),
  workplace: svg(
    `<rect x="18" y="16" width="28" height="32" rx="3" fill="#fff" opacity="0.92"/>
     <rect x="23" y="21" width="6" height="6" fill="#a0c4ff"/>
     <rect x="35" y="21" width="6" height="6" fill="#a0c4ff"/>
     <rect x="23" y="31" width="6" height="6" fill="#a0c4ff"/>
     <rect x="35" y="31" width="6" height="6" fill="#a0c4ff"/>`,
    '#bde0fe',
  ),
  food: svg(
    `<circle cx="32" cy="32" r="15" fill="#fff" opacity="0.92"/>
     <path d="M24 30 Q32 22 40 30" stroke="#ff8fab" stroke-width="3" fill="none" stroke-linecap="round"/>
     <circle cx="27" cy="35" r="2" fill="#ffb703"/>
     <circle cx="37" cy="35" r="2" fill="#ffb703"/>`,
    '#ffd6a5',
  ),
  retail: svg(
    `<rect x="18" y="24" width="28" height="24" rx="3" fill="#fff" opacity="0.92"/>
     <path d="M24 24 V20 A8 8 0 0 1 40 20 V24" stroke="#ff8fab" stroke-width="3" fill="none"/>`,
    '#ffc6ff',
  ),
  leisure: svg(
    `<circle cx="32" cy="32" r="14" fill="#fff" opacity="0.92"/>
     <path d="M32 22 L34.6 29 H42 L36 33.5 L38.3 41 L32 36.5 L25.7 41 L28 33.5 L22 29 H29.4 Z" fill="#ffd166"/>`,
    '#caffbf',
  ),
  transit: svg(
    `<rect x="18" y="20" width="28" height="24" rx="6" fill="#fff" opacity="0.92"/>
     <rect x="22" y="24" width="20" height="8" rx="2" fill="#a0c4ff"/>
     <circle cx="25" cy="46" r="3" fill="#5b4b6b"/>
     <circle cx="39" cy="46" r="3" fill="#5b4b6b"/>`,
    '#9bf6ff',
  ),
  civic: svg(
    `<path d="M16 44 H48 V47 H16 Z" fill="#fff" opacity="0.92"/>
     <path d="M18 44 V28 M26 44 V28 M38 44 V28 M46 44 V28" stroke="#fff" stroke-width="3"/>
     <path d="M14 28 L32 16 L50 28 Z" fill="#fff" opacity="0.92"/>`,
    '#bdb2ff',
  ),
}

export function agentPlaceholder(): string {
  return AGENT_PLACEHOLDER
}

export function locationPlaceholder(category: LocationCategory): string {
  return LOCATION_PLACEHOLDERS[category] ?? LOCATION_PLACEHOLDERS.civic
}
