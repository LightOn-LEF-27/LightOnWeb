// 분석 화면의 상태 — 전부 주소(URL)에 담는다 (링크를 복사하면 같은 화면). 세션은 sid 로만 (계약 §3-1)
//   /analysis?sid=…&x=distance&ref=7&cmp=3,DEMO-2_2026-11-14_S1~4&ax=gps.speed_kmh|imu.lat_g,imu.long_g&wb=basic
//   ref = 기준 랩 번호 또는 all(세션 전체 — 시간축만), cmp = 비교 랩(최대 2 — 다른 세션의 랩은 <sid>~<랩>), ax = 축마다 채널(축 하나에 둘까지),
//   wb = 워크북

export type XMode = 'time' | 'distance'

// 비교 랩 — sid 가 null 이면 이 세션의 랩. 다른 세션은 같은 frame(경기장 · 계측선)일 때만 겹친다 (계약 §3 frame_id)
export interface CmpLap {
  sid: string | null
  lap: number
}

export interface AnalysisState {
  sid: string | null
  x: XMode | null // null = 정하지 않음 (랩이 있으면 거리, 없으면 시간)
  ref: number | 'all' | null // null = 정하지 않음 (세션 최속 랩)
  cmp: CmpLap[]
  axes: string[][] | null // null = 엔진이 제안한 기본 축 (default_views)
  wb: string | null // 워크북 (null = 기본)
}

const CMP_ITEM = /^(?:([A-Za-z0-9][A-Za-z0-9_.-]{0,127})~)?(\d+)$/

export const cmpKey = (c: CmpLap): string => (c.sid ? `${c.sid}~${c.lap}` : String(c.lap))

export const MAX_CMP = 2
export const MAX_AXES = 8
export const MAX_PER_AXIS = 2

export function readState(p: URLSearchParams): AnalysisState {
  const x = p.get('x')
  const ref = p.get('ref')
  const refNum = ref !== null && /^\d+$/.test(ref) ? Number(ref) : null
  const ax = p.get('ax')
  return {
    sid: p.get('sid') || null,
    x: x === 'time' || x === 'distance' ? x : null,
    ref: ref === 'all' ? 'all' : refNum,
    cmp: (p.get('cmp') ?? '').split(',').map((v) => v.match(CMP_ITEM)).filter((m): m is RegExpMatchArray => !!m)
      .map((m) => ({ sid: m[1] ?? null, lap: Number(m[2]) })).slice(0, MAX_CMP),
    axes: ax === null ? null : ax === '' ? [] : ax.split('|').map((a) => a.split(',').filter(Boolean).slice(0, MAX_PER_AXIS)).filter((a) => a.length).slice(0, MAX_AXES),
    wb: p.get('wb') || null,
  }
}

export function writeState(s: AnalysisState): URLSearchParams {
  const p = new URLSearchParams()
  if (s.sid) p.set('sid', s.sid)
  if (s.x) p.set('x', s.x)
  if (s.ref !== null) p.set('ref', String(s.ref))
  if (s.cmp.length) p.set('cmp', s.cmp.map(cmpKey).join(','))
  if (s.axes !== null) p.set('ax', s.axes.map((a) => a.join(',')).join('|'))
  if (s.wb) p.set('wb', s.wb)
  return p
}

// 정하지 않은 것을 세션에 맞춰 채운다 — 랩이 없는 세션은 시간축 · 세션 전체만
export function resolveState(s: AnalysisState, laps: number[], bestLap: number | null): { x: XMode; ref: number | 'all'; cmp: CmpLap[] } {
  const ref: number | 'all' = laps.length === 0 ? 'all' : s.ref !== null && (s.ref === 'all' || laps.includes(s.ref)) ? s.ref : bestLap ?? laps[0]
  const x: XMode = ref === 'all' ? 'time' : s.x ?? 'distance'
  const seen = new Set<string>()
  const cmp =
    ref === 'all'
      ? []
      : s.cmp
          .map((c) => (c.sid === s.sid ? { sid: null, lap: c.lap } : c)) // 이 세션을 sid 로 적었으면 같은 것으로
          .filter((c) => (c.sid ? true : c.lap !== ref && laps.includes(c.lap))) // 다른 세션의 랩은 받아 보고 안다
          .filter((c) => !seen.has(cmpKey(c)) && !!seen.add(cmpKey(c)))
          .slice(0, MAX_CMP)
  return { x, ref, cmp }
}

// 채널 하나를 누르면: 이미 그려져 있으면 그 축에서 빼고(빈 축은 없앤다), 없으면 새 축으로
export function toggleChannel(axes: string[][], ch: string): string[][] {
  if (axes.some((a) => a.includes(ch))) return axes.map((a) => a.filter((c) => c !== ch)).filter((a) => a.length)
  return axes.length >= MAX_AXES ? axes : [...axes, [ch]]
}

// 축에 둘째 채널(오른쪽 세로축)을 더한다
export function addToAxis(axes: string[][], i: number, ch: string): string[][] {
  return axes.map((a, k) => (k === i && a.length < MAX_PER_AXIS && !a.includes(ch) ? [...a, ch] : a))
}

// ---------------------------------------------------------------- 최근 연 세션 · 마지막 분석 (이 브라우저에만)

const RECENT = 'lefsite.recent'
const LAST = 'lefsite.analysis.last'

function load<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(key)
    return v ? (JSON.parse(v) as T) : fallback
  } catch {
    return fallback
  }
}

function save(key: string, v: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(v))
  } catch {
    // 저장소를 막은 브라우저 — 기억하지 못할 뿐 화면은 그대로
  }
}

export const recentSessions = (): string[] => load<string[]>(RECENT, [])
export const lastAnalysis = (): string | null => load<string | null>(LAST, null)

export function remember(sid: string, search: string): void {
  save(RECENT, [sid, ...recentSessions().filter((s) => s !== sid)].slice(0, 8))
  save(LAST, search)
}
