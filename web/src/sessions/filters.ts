// 세션 목록 거르기 · 찾기 · 정렬 — 상태는 주소(URL)에 둔다 (링크를 복사하면 같은 목록).
// 목록 자체는 엔진이 준 순서(시작 최신순)와 값 그대로, 여기서는 고르고 늘어놓기만 한다.

import { COMPUTED } from './blocks'
import type { SessionRow } from '../types'

export type SortKey = 'start_desc' | 'start_asc' | 'distance' | 'duration'

export const SORTS: { key: SortKey; label: string }[] = [
  { key: 'start_desc', label: '시작 시각 · 최신' },
  { key: 'start_asc', label: '시작 시각 · 오래된' },
  { key: 'distance', label: '거리 · 긴 것' },
  { key: 'duration', label: '주행 시간 · 긴 것' },
]

export type FacetKey = 'car' | 'event' | 'disc'

export interface Filters {
  q: string
  car: string[] | null // null = 전부
  event: string[] | null
  disc: string[] | null
  warn: boolean // 주의가 있는 세션만
  seek: boolean // 미보유 데이터가 있는 세션만
  sort: SortKey
}

const FACET_VALUE: Record<FacetKey, (r: SessionRow) => string> = {
  car: (r) => r.car,
  event: (r) => r.event_title || r.event_id,
  disc: (r) => r.discipline,
}

const COMPUTED_KEYS = new Set(COMPUTED.map((c) => c.key))

const list = (v: string | null) => (v === null ? null : v.split(',').filter(Boolean))

export function readFilters(p: URLSearchParams): Filters {
  const sort = p.get('sort') as SortKey | null
  return {
    q: p.get('q') ?? '',
    car: list(p.get('car')),
    event: list(p.get('event')),
    disc: list(p.get('disc')),
    warn: p.get('warn') === '1',
    seek: p.get('seek') === '1',
    sort: SORTS.some((s) => s.key === sort) ? (sort as SortKey) : 'start_desc',
  }
}

export function writeFilters(f: Filters): URLSearchParams {
  const p = new URLSearchParams()
  if (f.q) p.set('q', f.q)
  for (const k of ['car', 'event', 'disc'] as const) if (f[k] !== null) p.set(k, f[k]!.join(','))
  if (f.warn) p.set('warn', '1')
  if (f.seek) p.set('seek', '1')
  if (f.sort !== 'start_desc') p.set('sort', f.sort)
  return p
}

export function hasWarn(r: SessionRow): boolean {
  return r.data_status.some((d) => d.status === 'warn' && !COMPUTED_KEYS.has(d.block)) // 추정 블록은 늘 주의라 빼고 본다
}

export function hasSeek(r: SessionRow): boolean {
  return r.data_status.some((d) => d.status === 'seek')
}

function matches(r: SessionRow, q: string): boolean {
  if (!q) return true
  const hay = [r.sid, r.title, r.name, r.car, r.event_title, r.discipline, r.driver_label, r.place].join(' ').toLowerCase()
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w))
}

const nullsLast = (a: number | null, b: number | null) => (a === null ? (b === null ? 0 : 1) : b === null ? -1 : b - a)

export function applyFilters(rows: SessionRow[], f: Filters): SessionRow[] {
  const out = rows.filter(
    (r) =>
      (['car', 'event', 'disc'] as const).every((k) => f[k] === null || f[k]!.includes(FACET_VALUE[k](r))) &&
      (!f.warn || hasWarn(r)) &&
      (!f.seek || hasSeek(r)) &&
      matches(r, f.q),
  )
  const start = (r: SessionRow) => r.start ?? ''
  const by: Record<SortKey, (a: SessionRow, b: SessionRow) => number> = {
    start_desc: (a, b) => start(b).localeCompare(start(a)),
    start_asc: (a, b) => start(a).localeCompare(start(b)),
    distance: (a, b) => nullsLast(a.distance_km, b.distance_km),
    duration: (a, b) => nullsLast(a.duration_s, b.duration_s),
  }
  return out.sort(by[f.sort])
}

export interface FacetItem {
  value: string
  n: number
}

// 거르기 칸의 항목과 세션 수 (전체 목록 기준). 이벤트는 최신 것부터, 나머지는 이름 순
export function facet(rows: SessionRow[], key: FacetKey): FacetItem[] {
  const count = new Map<string, { n: number; latest: string }>()
  for (const r of rows) {
    const v = FACET_VALUE[key](r)
    const c = count.get(v) ?? { n: 0, latest: '' }
    count.set(v, { n: c.n + 1, latest: (r.start ?? '') > c.latest ? (r.start ?? '') : c.latest })
  }
  const items = [...count.entries()].map(([value, c]) => ({ value, n: c.n, latest: c.latest }))
  items.sort((a, b) => (key === 'event' ? b.latest.localeCompare(a.latest) : a.value.localeCompare(b.value, 'ko')))
  return items.map(({ value, n }) => ({ value, n }))
}

// 체크 하나를 켜고 끈 결과. 전부 켜지면 null(= 전부)로 돌린다 — 새 차 · 새 이벤트가 생겨도 걸러지지 않게
export function toggle(current: string[] | null, all: string[], value: string): string[] | null {
  const on = new Set(current ?? all)
  if (on.has(value)) on.delete(value)
  else on.add(value)
  return all.every((v) => on.has(v)) ? null : all.filter((v) => on.has(v))
}
