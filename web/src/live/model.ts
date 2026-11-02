// 실시간 화면 — 받은 값을 그리기 위한 손질만 한다. 숫자 · 상태 · 문구는 엔진이 정한다 (계약 §12-4).
// 화면이 스스로 정하는 것은 둘뿐이다: 그래프 창(최근 5분)과, 서버 응답이 끊겼을 때 화면 전체를 낡음으로 돌리는 것.

import { formatValue } from '../overview/model'
import type { LiveState, LiveStatus, LiveTile } from '../types'

export const POLL_MS = 500
export const WINDOW_S = 300 // 그래프에 보이는 최근 5분
export const TRAIL_S = 30 // 트랙맵의 최근 궤적
export const FROZEN_S = 3 // 서버 응답이 이만큼 없으면 화면의 값이 전부 그 전 값 — 색을 빼고 띠를 띄운다

// ---------------------------------------------------------------- 그래프 값 (받은 줄을 이어 붙인다)

export interface ChartBuf {
  t: number[]
  series: Record<string, (number | null)[]>
}

export const emptyBuf = (): ChartBuf => ({ t: [], series: {} })

export const sinceOf = (buf: ChartBuf): number | null => (buf.t.length ? buf.t[buf.t.length - 1] : null)

// since 뒤의 줄만 오므로 겹치지 않는다. 그래도 가진 마지막 t 이하는 버리고, 창(now − keep) 밖은 앞에서 자른다.
// 가진 값이 서버 시계보다 앞서 있으면(서버를 옮겼거나 시계가 바뀜) 처음부터 다시 쌓는다
export function mergeChart(buf: ChartBuf, add: LiveState['chart'], now: number, keep = WINDOW_S): ChartBuf {
  const last = sinceOf(buf)
  if (last !== null && last > now + 5) return mergeChart(emptyBuf(), add, now, keep)
  const keys = [...new Set([...Object.keys(buf.series), ...Object.keys(add.series)])]
  const t = buf.t.slice()
  const series: ChartBuf['series'] = {}
  for (const k of keys) series[k] = buf.series[k] ? buf.series[k].slice() : buf.t.map(() => null)
  add.t.forEach((x, i) => {
    if (last !== null && x <= last) return
    t.push(x)
    for (const k of keys) series[k].push(add.series[k]?.[i] ?? null)
  })
  let cut = 0
  while (cut < t.length && t[cut] < now - keep) cut++
  if (!cut) return { t, series }
  for (const k of keys) series[k] = series[k].slice(cut)
  return { t: t.slice(cut), series }
}

// 트랙맵의 최근 궤적 — 그래프 값에 실려 오는 위치(gps.x_m · y_m)에서 최근 TRAIL_S 만
export function trail(buf: ChartBuf, now: number, span = TRAIL_S): [number, number][] {
  const xs = buf.series['gps.x_m'] ?? []
  const ys = buf.series['gps.y_m'] ?? []
  const out: [number, number][] = []
  buf.t.forEach((t, i) => {
    const x = xs[i], y = ys[i]
    if (t >= now - span && x != null && y != null) out.push([x, y])
  })
  return out
}

// ---------------------------------------------------------------- 표시

// 서버 응답이 끊기면 모든 값이 그 전 값이다 — 색이 있는 상태도 낡음으로 (낡은 값에는 색을 칠하지 않는다, 설계안 §4-2)
export function shownStatus(status: LiveStatus, frozen: boolean): LiveStatus {
  if (status === 'none') return 'none'
  return frozen ? 'stale' : status
}

export function tileValue(tile: LiveTile): string {
  return tile.value === null ? '—' : formatValue(tile.value, tile.digits)
}

export function ageText(age: number | null): string {
  if (age === null) return ''
  if (age < 10) return `${age.toFixed(1)} s 전`
  if (age < 120) return `${Math.round(age)} s 전`
  return `${Math.round(age / 60)}분 전`
}

export const STATUS_WORD: Partial<Record<LiveStatus, string>> = { danger: '위험', warn: '주의' }

type Tone = 'ok' | 'warn' | 'danger' | 'stale' | 'none'

export function linkView(link: LiveState['link']): { text: string; tone: Tone } {
  if (link.status === 'none' || link.age_s === null) return { text: '수신 없음', tone: 'none' }
  if (link.status === 'ok') return { text: `수신 ${link.age_s.toFixed(1)} s`, tone: 'ok' }
  if (link.status === 'late') return { text: `수신 늦음 ${link.age_s.toFixed(1)} s`, tone: 'warn' }
  return { text: `수신 끊김 ${Math.round(link.since_s ?? link.age_s)} s`, tone: 'danger' }
}

export function rateText(link: LiveState['link']): string {
  return link.rate_10s === null ? '10 s —' : `10 s ${Math.round(link.rate_10s * 100)} %`
}

export function outageText(link: LiveState['link']): string {
  if (!link.outages) return '끊김 없음'
  return `끊김 ${link.outages}번${link.longest_s !== null ? ` · 최장 ${Math.round(link.longest_s)} s` : ''}`
}

export function deviceView(d: LiveState['device']): { text: string; tone: Tone } {
  if (d.status === 'on') return { text: '장치 켜짐', tone: 'ok' }
  if (d.status === 'off' || d.status === 'disconnected') return { text: '장치 꺼짐', tone: 'none' }
  return { text: '장치 상태 모름', tone: 'none' }
}

export function receiverView(r: LiveState['receiver']): { text: string; tone: Tone } {
  if (r.status === 'connected') return { text: '수신기', tone: 'ok' }
  if (r.status === 'disconnected') return { text: '수신기 끊김', tone: 'danger' }
  return { text: '수신기 상태 모름', tone: 'none' }
}

export const onOff = (v: boolean | null): string => (v === null ? '—' : v ? '켜짐' : '꺼짐')

// 랩 시간은 GPS 주기(2 Hz)만큼 틀릴 수 있어 소수 한 자리까지만 (엔진도 0.1 s 로 준다)
export function lapText(s: number | null): string {
  if (s === null || !Number.isFinite(s)) return '—'
  const m = Math.floor(s / 60)
  return m ? `${m}:${(s - m * 60).toFixed(1).padStart(4, '0')}` : s.toFixed(1)
}

// 서버 시계(epoch s) → 한국 시각 HH:MM:SS (폰 · 노트북의 시간대 설정과 상관없이)
const KST = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
export const clock = (t: number): string => KST.format(new Date(t * 1000))

// ---------------------------------------------------------------- 그래프 칸

export interface PanelSeries {
  id: string
  right?: boolean // 오른쪽 세로축
  color: 0 | 1 | 2 // --trace-<n>
}

export const PANELS: { key: string; title: string; series: PanelSeries[] }[] = [
  { key: 'speed', title: '속도', series: [{ id: 'gps.speed_kmh', color: 2 }] },
  { key: 'power', title: '전력 · 전류', series: [{ id: 'battery.power_kw', color: 0 }, { id: 'battery.current_a', right: true, color: 2 }] },
  { key: 'temp', title: '온도', series: [{ id: 'bms.temp_max_c', color: 0 }, { id: 'motor.t_motor_c', color: 2 }, { id: 'motor.t_igbt_c', color: 1 }] },
]
