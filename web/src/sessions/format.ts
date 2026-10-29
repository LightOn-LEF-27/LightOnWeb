// 세션 이름 · 숫자 표시. 값을 계산하지 않고 엔진이 준 값을 글자로만 바꾼다.

import type { SessionRow } from '../types'

// 이름 규칙 (사이트_설계.md §7): 날짜 · R순번 · 출발 시각 · 종목 · 드라이버 · 목적
// R순번은 엔진의 day_order (화면용 — 세션이 더 들어오면 바뀐다. 주소 · 저장은 sid 로만)
// 목적 칸이 아직 없어 엔진의 세션 이름(name)을 목적 자리에 둔다 — 세팅 기록이 생기면 그쪽으로
export interface SessionName {
  main: string
  purpose: string
}

export function driverShort(label: string): string {
  return label.replace(/^드라이버\s*/, '')
}

// 시각은 엔진이 준 그 자리의 현지 시각 그대로 쓴다 (브라우저 시간대로 바꾸지 않는다)
function localParts(start: string | null): { date: string; md: string; hm: string; s: string } | null {
  const m = start?.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/)
  return m ? { date: `${m[1]}-${m[2]}-${m[3]}`, md: `${m[2]}-${m[3]}`, hm: `${m[4]}:${m[5]}`, s: m[6] ?? '00' } : null
}

export function sessionName(row: SessionRow, opts: { withCar?: boolean } = {}): SessionName {
  const t = localParts(row.start)
  const parts = t
    ? [t.md, row.day_order ? `R${row.day_order}` : '', t.hm, row.discipline, driverShort(row.driver_label)]
    : [row.title]
  if (opts.withCar && t) parts.unshift(row.car)
  return { main: parts.filter(Boolean).join(' · '), purpose: t ? row.name : '' }
}

export function formatStart(start: string | null, opts: { seconds?: boolean } = {}): string {
  const t = localParts(start)
  return t ? `${t.date} ${t.hm}${opts.seconds ? `:${t.s}` : ''}` : '—'
}

export function formatDuration(s: number | null): string {
  if (s === null || !Number.isFinite(s)) return '—'
  const total = Math.round(s)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const sec = String(total % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`
}

export function formatNumber(v: number | null | undefined, digits: number): string {
  return v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toFixed(digits)
}

// 단위는 값이 있을 때만 붙인다 ("— km" 가 아니라 "—")
export function formatWithUnit(v: number | null | undefined, digits: number, unit: string): string {
  const s = formatNumber(v, digits)
  return s === '—' ? s : `${s} ${unit}`
}

export function formatLapTime(s: number | null): string {
  if (s === null || !Number.isFinite(s)) return '—'
  const m = Math.floor(s / 60)
  return m ? `${m}:${(s - m * 60).toFixed(2).padStart(5, '0')}` : s.toFixed(2)
}

// 랩 칸: 코스 기록 없음(has_track false) · 랩이 없는 종목(laps 0) 은 "—" 로, 이유는 풍선 도움말로
export function lapsCell(row: SessionRow): { text: string; tip: string; muted: boolean } {
  if (!row.has_track) return { text: '—', tip: '코스(위치) 기록이 없는 세션', muted: true }
  if (!row.laps) return { text: '—', tip: '랩이 없는 세션 (가속 · 스키드패드 등)', muted: true }
  return { text: String(row.laps), tip: '', muted: false }
}
