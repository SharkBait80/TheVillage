// Built-in fake backend (VITE_MOCK=1). Generates a small, lively Melbourne:
// a handful of agents wandering within the map bounds, one active conversation,
// and one traveller following a polyline route. Enough to make every UI feature
// visually verifiable WITHOUT the real Simulation_API.
//
// Time advances in "simulated minutes" while status is running; pause holds it.

import type {
  AgentAction,
  AgentDetail,
  ControlCommand,
  ConversationItem,
  CreateEventInput,
  CreateEventResult,
  DecisionTrail,
  EventEntry,
  LatLon,
  LocationCategory,
  LocationDetail,
  LocationItem,
  SimState,
  SimStatus,
  StateAgent,
  StateConversation,
  Utterance,
} from './types'

// Melbourne map bounds (Req15.1 / Req3.3).
const LAT_MIN = -38.0
const LAT_MAX = -37.7
const LON_MIN = 144.85
const LON_MAX = 145.1

function clampLat(v: number): number {
  return Math.max(LAT_MIN, Math.min(LAT_MAX, v))
}
function clampLon(v: number): number {
  return Math.max(LON_MIN, Math.min(LON_MAX, v))
}

// Seeded PRNG so wandering looks organic but stable per session.
let seed = 1337
function rnd(): number {
  seed = (seed * 1664525 + 1013904223) % 4294967296
  return seed / 4294967296
}

interface MockLocation extends LocationItem {
  category: LocationCategory
}

const LOCATIONS: MockLocation[] = [
  { id: 'loc_fed_square', name: 'Federation Square', category: 'leisure', lat: -37.817979, lon: 144.96848, capacity: 500, hours: allDay('08:00', '23:00') },
  { id: 'loc_flinders_st', name: 'Flinders Street Station', category: 'transit', lat: -37.818239, lon: 144.966964, capacity: 2000, hours: allDay('05:00', '00:00') },
  { id: 'loc_qv_market', name: 'Queen Victoria Market', category: 'retail', lat: -37.807237, lon: 144.956776, capacity: 800, hours: allDay('06:00', '15:00') },
  { id: 'loc_carlton_cafe', name: 'Carlton Corner Café', category: 'food', lat: -37.7995, lon: 144.9672, capacity: 40, hours: allDay('07:00', '17:00'), price: 12.5 },
  { id: 'loc_carlton_apartments', name: 'Carlton Terrace Apartments', category: 'residence', lat: -37.798869, lon: 144.96719, capacity: 120, hours: allDay('00:00', '23:59') },
  { id: 'loc_southbank_office', name: 'Southbank Media Office', category: 'workplace', lat: -37.8226, lon: 144.9648, capacity: 300, hours: allDay('08:00', '18:00') },
  { id: 'loc_botanic_gardens', name: 'Royal Botanic Gardens', category: 'leisure', lat: -37.8304, lon: 144.9803, capacity: 1000, hours: allDay('07:00', '20:00') },
  { id: 'loc_remand', name: 'City Remand Centre', category: 'civic', lat: -37.8106, lon: 144.9498, capacity: 200, hours: allDay('00:00', '23:59'), isDetentionFacility: true },
]

function allDay(open: string, close: string) {
  return Array.from({ length: 7 }, () => ({ open, close }))
}

interface MockAgent {
  id: string
  name: string
  age: number
  occupation: string
  traits: string[]
  background: string
  homeLocationId: string
  mbti: string
  lat: number
  lon: number
  // wander target
  tlat: number
  tlon: number
  action: AgentAction
  legal: StateAgent['legal']
  employment: StateAgent['employment']
  cash: number
  needs: { hunger: number; energy: number; social: number; fun: number }
  events: EventEntry[]
  // traveller route (mock)
  route?: LatLon[]
  routeStep?: number
}

const FIRST = ['Aroha', 'Mia', 'Leo', 'Priya', 'Sam', 'Noah', 'Ivy', 'Kai', 'Zoe', 'Otis']
const LAST = ['Ngata', 'Chen', 'Rossi', 'Kaur', 'Nguyen', 'Okafor', 'Bell', 'Tanaka', 'Petrou', 'Ward']
const OCC = ['Barista', 'Illustrator', 'Nurse', 'Teacher', 'Musician', 'Gardener', 'Bookseller', 'Chef']
const TRAITS = [
  ['warm', 'curious', 'impulsive'],
  ['calm', 'thoughtful', 'kind'],
  ['bubbly', 'creative', 'restless'],
  ['shy', 'observant', 'loyal'],
  ['bold', 'cheeky', 'generous'],
]
const MBTI = [
  'ISTJ', 'ISFJ', 'INFJ', 'INTJ',
  'ISTP', 'ISFP', 'INFP', 'INTP',
  'ESTP', 'ESFP', 'ENFP', 'ENTP',
  'ESTJ', 'ESFJ', 'ENFJ', 'ENTJ',
]

function makeAgents(count: number): MockAgent[] {
  const agents: MockAgent[] = []
  for (let i = 0; i < count; i++) {
    const home = LOCATIONS[i % LOCATIONS.length]
    const lat = clampLat(home.lat + (rnd() - 0.5) * 0.02)
    const lon = clampLon(home.lon + (rnd() - 0.5) * 0.02)
    agents.push({
      id: `agent_${String(i + 1).padStart(2, '0')}`,
      name: `${FIRST[i % FIRST.length]} ${LAST[(i * 3) % LAST.length]}`,
      age: 22 + Math.floor(rnd() * 45),
      occupation: OCC[i % OCC.length],
      traits: TRAITS[i % TRAITS.length],
      background: 'A friendly Melburnian going about a cozy day in the village.',
      homeLocationId: home.id,
      mbti: MBTI[i % MBTI.length],
      lat,
      lon,
      tlat: clampLat(lat + (rnd() - 0.5) * 0.01),
      tlon: clampLon(lon + (rnd() - 0.5) * 0.01),
      action: { type: 'leisure', targetType: 'location', targetId: home.id, expectedDurationMin: 30, progress: rnd() },
      legal: i === 3 ? 'suspected' : 'clear',
      employment: i % 5 === 0 ? 'unemployed' : 'employed',
      cash: Math.round((50 + rnd() * 450) * 100) / 100,
      needs: {
        hunger: 40 + Math.floor(rnd() * 55),
        energy: 40 + Math.floor(rnd() * 55),
        social: 40 + Math.floor(rnd() * 55),
        fun: 40 + Math.floor(rnd() * 55),
      },
      events: [],
    })
  }
  return agents
}

const AGENT_COUNT = 8
const agents = makeAgents(AGENT_COUNT)

// Designate a traveller (agent_02) with a polyline route from Carlton to Southbank.
const traveller = agents[1]
traveller.route = [
  [traveller.lat, traveller.lon],
  [-37.808, 144.966],
  [-37.816, 144.965],
  [-37.8226, 144.9648],
]
traveller.routeStep = 0
traveller.action = {
  type: 'travel',
  targetType: 'location',
  targetId: 'loc_southbank_office',
  expectedDurationMin: 24,
  progress: 0,
  route: traveller.route.map(([lat, lon]) => ({ lat, lon })),
}

// A standing conversation between agent_03 and agent_04 (placed together).
const talkerA = agents[2]
const talkerB = agents[3]
talkerB.lat = talkerA.lat + 0.0003
talkerB.lon = talkerA.lon + 0.0003
talkerA.action = { type: 'socialise', targetType: 'agent', targetId: talkerB.id, expectedDurationMin: 8, progress: 0.3 }
talkerB.action = { type: 'socialise', targetType: 'agent', targetId: talkerA.id, expectedDurationMin: 8, progress: 0.3 }

// Simulated clock — Australia/Melbourne offset +11:00 (design startSimTime).
let simEpochMin = 0 // minutes since sim start
const START = new Date('2026-03-02T06:00:00+11:00').getTime()
let status: SimStatus = 'running'
const accel = 4
let seqCounter = 1
let lastTick = Date.now()

function simTimeIso(): string {
  const d = new Date(START + simEpochMin * 60_000)
  // Keep the +11:00 Melbourne offset in the string.
  const iso = new Date(d.getTime() + 11 * 3600_000).toISOString().replace('Z', '+11:00')
  return iso
}

function pushEvent(agent: MockAgent, category: string, description: string) {
  const ev: EventEntry = {
    seq: seqCounter++,
    simTime: simTimeIso(),
    realTime: new Date().toISOString(),
    category,
    agents: [agent.id],
    locationId: agent.action.targetId ?? null,
    description,
  }
  agent.events.unshift(ev)
  if (agent.events.length > 25) agent.events.pop()
}

// Seed a few events so detail panels have content immediately.
for (const a of agents) {
  pushEvent(a, 'action', `${a.name} started a ${a.action.type} action.`)
  pushEvent(a, 'planning', `${a.name} planned their day around ${a.occupation.toLowerCase()} work.`)
}

let conversationActive = true
let conversationEndsAtMin = 6 // ends a bit after start to exercise removal (R15.5)

// Rolling store of resolved conversation transcripts (most-recent first) so the
// Conversations panel has content in mock mode.
const conversationLog: ConversationItem[] = []
let convSeq = 1000

const SAMPLE_LINES: [string, string][] = [
  ['Morning! You grabbing a coffee too?', 'Always — the Carlton roast is unbeatable today.'],
  ['Did you see the markets are packed?', 'Saturday crowd! I got the last of the good tomatoes.'],
  ['How was your shift at the studio?', 'Long, but we wrapped the edit. Relieved.'],
  ['Fancy a walk through the gardens later?', 'Yes please, I need the fresh air after all that screen time.'],
]

function recordConversation(a: MockAgent, b: MockAgent): void {
  const pick = SAMPLE_LINES[Math.floor(rnd() * SAMPLE_LINES.length)]
  const utterances: Utterance[] = [
    { speaker: a.id, text: pick[0] },
    { speaker: b.id, text: pick[1] },
    { speaker: a.id, text: 'Catch you later then!' },
  ]
  conversationLog.unshift({
    id: `mock-convo-${convSeq}`,
    seq: convSeq++,
    simTime: simTimeIso(),
    locationId: a.action.targetId ?? 'loc_fed_square',
    participants: [a.id, b.id],
    utterances,
    truncated: false,
    utteranceCount: utterances.length,
  })
  if (conversationLog.length > 30) conversationLog.pop()
}

// Seed one resolved conversation so the Conversations panel has content on load
// (placed AFTER recordConversation + its const deps to avoid a TDZ error).
recordConversation(agents[0], agents[4])

/** Advance the mock world. Called lazily on each getState() while running. */
function advance() {
  if (status !== 'running') {
    lastTick = Date.now()
    return
  }
  const now = Date.now()
  const realElapsedS = (now - lastTick) / 1000
  lastTick = now
  // accel sim-seconds per real-second → sim minutes.
  const simMinutes = (realElapsedS * accel) / 60
  if (simMinutes <= 0) return
  simEpochMin += simMinutes

  for (const a of agents) {
    // Gentle need decay.
    a.needs.hunger = Math.max(0, a.needs.hunger - simMinutes * 0.1)
    a.needs.energy = Math.max(0, a.needs.energy - simMinutes * 0.07)

    if (a === traveller && a.route) {
      const p = Math.min(1, (a.action.progress ?? 0) + simMinutes / (a.action.expectedDurationMin ?? 24))
      a.action.progress = p
      // Interpolate along the polyline.
      const pos = interpolateRoute(a.route, p)
      a.lat = clampLat(pos[0])
      a.lon = clampLon(pos[1])
      if (p >= 1) {
        // Arrived: swap to leisure, drop route within the next update (R15.4).
        a.action = { type: 'leisure', targetType: 'location', targetId: 'loc_southbank_office', expectedDurationMin: 30, progress: 0 }
        a.route = undefined
        pushEvent(a, 'action', `${a.name} arrived at Southbank Media Office.`)
        // Restart a fresh trip after a beat to keep the demo lively.
        setTimeout(restartTraveller, 4000)
      }
      continue
    }

    if (a === talkerA || a === talkerB) {
      a.action.progress = Math.min(1, (a.action.progress ?? 0) + simMinutes / 8)
      continue
    }

    // Wanderers drift toward their target, then pick a new one.
    a.lat = clampLat(a.lat + (a.tlat - a.lat) * 0.05 * simMinutes)
    a.lon = clampLon(a.lon + (a.tlon - a.lon) * 0.05 * simMinutes)
    if (Math.abs(a.lat - a.tlat) < 0.0004 && Math.abs(a.lon - a.tlon) < 0.0004) {
      a.tlat = clampLat(a.lat + (rnd() - 0.5) * 0.012)
      a.tlon = clampLon(a.lon + (rnd() - 0.5) * 0.012)
    }
  }

  // End the conversation once to exercise indicator removal.
  if (conversationActive && simEpochMin >= conversationEndsAtMin) {
    conversationActive = false
    pushEvent(talkerA, 'conversation', `${talkerA.name} and ${talkerB.name} finished chatting.`)
    recordConversation(talkerA, talkerB)
    // Rekindle a new conversation later so the feature stays demonstrable.
    setTimeout(() => {
      conversationActive = true
      conversationEndsAtMin = simEpochMin + 6
    }, 6000)
  }
}

function restartTraveller() {
  traveller.route = [
    [traveller.lat, traveller.lon],
    [-37.808, 144.966],
    [-37.7995, 144.9672],
  ]
  traveller.action = {
    type: 'travel',
    targetType: 'location',
    targetId: 'loc_carlton_cafe',
    expectedDurationMin: 20,
    progress: 0,
    route: traveller.route.map(([lat, lon]) => ({ lat, lon })),
  }
}

function interpolateRoute(route: LatLon[], t: number): LatLon {
  if (route.length < 2) return route[0]
  const total = route.length - 1
  const scaled = t * total
  const i = Math.min(total - 1, Math.floor(scaled))
  const frac = scaled - i
  const [aLat, aLon] = route[i]
  const [bLat, bLon] = route[i + 1]
  return [aLat + (bLat - aLat) * frac, aLon + (bLon - aLon) * frac]
}

function toStateAgent(a: MockAgent): StateAgent {
  return {
    id: a.id,
    name: a.name,
    lat: a.lat,
    lon: a.lon,
    action: a.action,
    route: a.action.type === 'travel' && a.route ? a.route.map(([lat, lon]) => ({ lat, lon })) : null,
    legal: a.legal,
    employment: a.employment,
  }
}

function presentAt(locId: string): { id: string; name: string }[] {
  const loc = LOCATIONS.find((l) => l.id === locId)
  if (!loc) return []
  return agents
    .filter((a) => haversineM(a.lat, a.lon, loc.lat, loc.lon) <= 120)
    .map((a) => ({ id: a.id, name: a.name }))
}

/** Nearest agent to a point (for attributing operator-injected events). */
function nearestAgent(lat: number, lon: number): MockAgent | null {
  let best: MockAgent | null = null
  let bestD = Infinity
  for (const a of agents) {
    const d = haversineM(lat, lon, a.lat, a.lon)
    if (d < bestD) {
      bestD = d
      best = a
    }
  }
  return best
}

function haversineM(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const R = 6371000
  const dLat = ((lat2 - lat1) * Math.PI) / 180
  const dLon = ((lon2 - lon1) * Math.PI) / 180
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.sin(dLon / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(a))
}

function locationStatus(loc: MockLocation): LocationDetail['status'] {
  const present = presentAt(loc.id).length
  if (present >= loc.capacity) return 'at_capacity'
  return 'open'
}

export const mockBackend = {
  async getState(): Promise<SimState> {
    advance()
    const conversations: StateConversation[] = conversationActive
      ? [{ participants: [talkerA.id, talkerB.id], locationId: talkerA.action.targetId ?? 'loc_fed_square' }]
      : []
    return {
      simTime: simTimeIso(),
      status,
      accel,
      agents: agents.map(toStateAgent),
      conversations,
    }
  },

  async getAgent(agentId: string): Promise<AgentDetail> {
    const a = agents.find((x) => x.id === agentId)
    if (!a) throw new Error('agent not found')
    return {
      id: a.id,
      name: a.name,
      persona: {
        name: a.name,
        age: a.age,
        occupation: a.occupation,
        traits: a.traits,
        background: a.background,
        homeLocationId: a.homeLocationId,
        mbti: a.mbti,
      },
      needs: {
        hunger: Math.round(a.needs.hunger),
        energy: Math.round(a.needs.energy),
        social: Math.round(a.needs.social),
        fun: Math.round(a.needs.fun),
      },
      critical: {
        hunger: a.needs.hunger < 20,
        energy: a.needs.energy < 20,
        social: a.needs.social < 20,
        fun: a.needs.fun < 20,
      },
      cash: a.cash,
      employmentStatus: a.employment,
      legalStatus: a.legal,
      currentAction: a.action,
      lat: a.lat,
      lon: a.lon,
      recentEvents: a.events.slice(0, 10),
    }
  },

  async getLocations(): Promise<LocationItem[]> {
    return LOCATIONS.map((l) => ({ ...l, status: locationStatus(l), presentAgents: presentAt(l.id) }))
  },

  async getLocation(locId: string): Promise<LocationDetail> {
    const l = LOCATIONS.find((x) => x.id === locId)
    if (!l) throw new Error('location not found')
    return { ...l, status: locationStatus(l), presentAgents: presentAt(l.id) }
  },

  async getConversations(agentId?: string): Promise<ConversationItem[]> {
    advance()
    const all = conversationLog.slice()
    return agentId ? all.filter((c) => c.participants.includes(agentId)) : all
  },

  async getConversation(convId: string): Promise<ConversationItem> {
    const c = conversationLog.find((x) => x.id === convId)
    if (!c) throw new Error('conversation not found')
    return c
  },

  async getDecisionTrail(_seq: number): Promise<DecisionTrail | null> {
    // Synthesize a plausible thought-process for the demo. In mock mode the
    // AgentPanel passes the selected agent's latest action seq; we ignore it and
    // return a representative trail so the UI section is exercised.
    const a = agents[Math.floor(rnd() * agents.length)]
    return {
      actionEventSeq: _seq,
      simTime: simTimeIso(),
      reasoning: `My ${a.action.type} felt right — social battery is fine and I want to make the most of the afternoon in the village.`,
      perceptionInput: {
        simTime: simTimeIso(),
        locationId: a.action.targetId ?? null,
        needs: {
          hunger: Math.round(a.needs.hunger),
          energy: Math.round(a.needs.energy),
          social: Math.round(a.needs.social),
          fun: Math.round(a.needs.fun),
        },
        cash: a.cash,
        legalStatus: a.legal,
        employmentStatus: a.employment,
      },
      retrievedMemoryIds: [101, 102],
      action: a.action,
    }
  },

  async control(command: ControlCommand): Promise<{ status: SimStatus }> {
    // Enforce the same validity rules the real controller uses (R15.9/R15.12).
    const reject = (msg: string) => {
      const err = new Error(msg) as Error & { status: SimStatus }
      err.status = status
      throw err
    }
    switch (command) {
      case 'start':
        if (status === 'running' || status === 'paused') reject(`Cannot start while ${status}.`)
        status = 'running'
        lastTick = Date.now()
        break
      case 'pause':
        if (status !== 'running') reject(`Cannot pause while ${status}.`)
        status = 'paused'
        break
      case 'resume':
        if (status !== 'paused') reject(`Cannot resume while ${status}.`)
        status = 'running'
        lastTick = Date.now()
        break
      case 'stop':
        if (status === 'stopped') reject('Cannot stop while stopped.')
        status = 'stopped'
        break
    }
    return { status }
  },

  /**
   * Simulate POST /reseed. Regenerates the mock population in place (fresh
   * names, MBTI, bios) and resets the world to stopped, so the destructive
   * action visibly takes effect in mock mode.
   */
  async reseed(): Promise<{ accepted: boolean }> {
    const fresh = makeAgents(agents.length || AGENT_COUNT)
    agents.splice(0, agents.length, ...fresh)
    status = 'stopped'
    return { accepted: true }
  },

  /**
   * Simulate POST /events. Validates the Melbourne bounds + field lengths
   * locally (mirroring the API's 400s) then runs a tiny content-moderation
   * pass: descriptions containing an implausible marker ('dragon', 'unicorn',
   * 'alien', 'zombie') are rejected as implausible; a banned/toxic marker
   * ('slur', 'kill everyone') is rejected as toxic. Otherwise the event is
   * accepted, pushed into the mock event/conversation stream so the world
   * visibly reacts, and a positive verdict returned. Rejections throw an Error
   * whose message the modal surfaces (matching ApiRequestError semantics).
   */
  async createEvent(input: CreateEventInput): Promise<CreateEventResult> {
    const title = (input.title ?? '').trim()
    const description = (input.description ?? '').trim()

    // Structural validation (mirrors the API's 400 responses).
    if (title.length < 1 || title.length > 120) {
      throw new Error('Title must be between 1 and 120 characters.')
    }
    if (description.length < 1 || description.length > 1000) {
      throw new Error('Description must be between 1 and 1000 characters.')
    }
    if (
      typeof input.lat !== 'number' ||
      typeof input.lon !== 'number' ||
      Number.isNaN(input.lat) ||
      Number.isNaN(input.lon) ||
      input.lat < LAT_MIN ||
      input.lat > LAT_MAX ||
      input.lon < LON_MIN ||
      input.lon > LON_MAX
    ) {
      throw new Error('Coordinates are outside the Melbourne map bounds.')
    }

    // Content moderation (mirrors the API's 422 rejections).
    const lower = `${title} ${description}`.toLowerCase()
    const TOXIC = ['slur', 'kill everyone']
    const IMPLAUSIBLE = ['dragon', 'unicorn', 'alien', 'zombie']
    const toxicHit = TOXIC.find((w) => lower.includes(w))
    if (toxicHit) {
      const reason = `Content flagged as toxic (matched "${toxicHit}").`
      throw new Error(`Event rejected: ${reason}`)
    }
    const implausibleHit = IMPLAUSIBLE.find((w) => lower.includes(w))
    if (implausibleHit) {
      const reason = `Not plausible for Melbourne (mentions "${implausibleHit}").`
      throw new Error(`Event rejected: ${reason}`)
    }

    // Accepted: record it against the nearest agent so detail panels + the
    // event stream visibly react to the operator's injection.
    const simTime = simTimeIso()
    const nearest = nearestAgent(input.lat, input.lon)
    const id = `mock-event-${seqCounter}`
    if (nearest) {
      pushEvent(nearest, 'operator', `Operator event near ${nearest.name}: ${title} — ${description}`)
    }
    return {
      accepted: true,
      id,
      simTime,
      verdict: { plausible: true, relevant: true, toxic: false, reason: 'ok' },
    }
  },

  assetUrl(subjectId: string): string | null {
    // Return null for everything so the UI exercises its cute placeholders,
    // EXCEPT a couple of agents/locations which get a generated pastel tile so
    // the "real artwork" path is also visually demonstrated.
    if (subjectId === 'agent_01' || subjectId === 'loc_fed_square') {
      const hue = subjectId === 'agent_01' ? 330 : 200
      const doc = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64' width='64' height='64'>
        <defs><radialGradient id='g'><stop offset='0%' stop-color='hsl(${hue},90%,88%)'/><stop offset='100%' stop-color='hsl(${hue},70%,72%)'/></radialGradient></defs>
        <rect width='64' height='64' rx='16' fill='url(%23g)'/>
        <circle cx='32' cy='30' r='13' fill='white' opacity='0.85'/>
        <circle cx='27' cy='29' r='2' fill='%235b4b6b'/><circle cx='37' cy='29' r='2' fill='%235b4b6b'/>
        <path d='M26 35 Q32 40 38 35' stroke='%235b4b6b' stroke-width='2' fill='none' stroke-linecap='round'/>
      </svg>`
      return `data:image/svg+xml;utf8,${encodeURIComponent(doc)}`
    }
    return null
  },
}
