// 커서 위치의 값 · 트랙맵 점 찾기 · 트랙맵 색 — 엔진이 준 표에서 가까운 칸을 찾고 색을 고를 뿐 (값을 새로 계산하지 않는다)

import type { SectorsResponse, SeriesResponse, TrackResponse } from '../types'
import type { XMode } from './state'

// 오름차순 배열에서 v 에 가장 가까운 칸
export function nearestIndex(arr: number[], v: number): number {
  if (!arr.length) return -1
  let lo = 0
  let hi = arr.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (arr[mid] <= v) lo = mid
    else hi = mid
  }
  return Math.abs(arr[hi] - v) < Math.abs(arr[lo] - v) ? hi : lo
}

// 오름차순 배열에서 v 이하인 마지막 칸 (0 ~ 길이 − 1 로 자른다)
export function floorIndex(arr: number[], v: number): number {
  let lo = 0
  let hi = arr.length
  while (lo < hi) {
    const mid = (lo + hi) >> 1
    if (arr[mid] <= v) lo = mid + 1
    else hi = mid
  }
  return Math.min(Math.max(lo - 1, 0), arr.length - 1)
}

// 화면 x(시간축이면 랩 시작을 0 으로 옮긴 값)에서 그 계열의 채널 값 — 가장 가까운 칸, 칸 간격보다 멀면 없음
export function valueAt(resp: SeriesResponse | undefined, channel: string, x: number, shift: number): number | null {
  const s = resp?.series.find((v) => v.channel_id === channel)
  if (!resp || !s) return null
  const xs = resp.x.values
  const i = nearestIndex(xs, x + shift)
  if (i < 0) return null
  const gap = xs.length > 1 ? Math.abs(xs[1] - xs[0]) * 2 : Infinity
  return Math.abs(xs[i] - (x + shift)) <= gap ? s.values[i] : null
}

// 그래프의 x → 트랙맵 점: 거리축은 기준 거리(ref_m), 시간축은 세션 시간(t_s)으로 찾는다
export function trackIndex(track: TrackResponse, x: number, mode: XMode, lapStart: number): number {
  if (mode === 'distance' && track.ref_m) return nearestIndex(track.ref_m, x)
  return nearestIndex(track.t_s, x + lapStart)
}

// 트랙맵 점 → 그래프의 x (진행 막대 · 재생에서 커서 A 를 옮길 때)
export function xOfTrackPoint(track: TrackResponse, i: number, mode: XMode, lapStart: number): number {
  return mode === 'distance' && track.ref_m ? track.ref_m[i] : track.t_s[i] - lapStart
}

// ---------------------------------------------------------------- 트랙맵 색 (사이트_설계.md §7)

export const PURPLE = '#b07cf0'
const G_LO = [163, 228, 181] // 옅은 초록 — 평균 근처
const G_HI = [24, 176, 79]
const R_LO = [246, 163, 155]
const R_HI = [226, 38, 28]

function mix(a: number[], b: number[], t: number): string {
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * t))
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`
}

// v < 0 = 빠름(초록), v > 0 = 느림(빨강). |v| 가 클수록 진하게 — 어둡게 내리지 않는다 (갈색이 된다)
export function paceColor(v: number): string {
  const t = Math.min(1, Math.abs(v))
  return v < 0 ? mix(G_LO, G_HI, t) : mix(R_LO, R_HI, t)
}

// 미니 구간마다 값(빠름 −1 ~ 느림 +1)을 미니 구간 가운데끼리 이어서 점마다 — 점이 어느 미니 구간인지는 기준 거리로
function smoothAt(edges: number[], vals: number[], ref: number): number {
  const mids = vals.map((_, k) => (edges[k] + edges[k + 1]) / 2)
  if (ref <= mids[0]) return vals[0]
  if (ref >= mids[mids.length - 1]) return vals[vals.length - 1]
  const k = floorIndex(mids, ref)
  const f = (ref - mids[k]) / (mids[k + 1] - mids[k])
  return vals[k] * (1 - f) + vals[k + 1] * f
}

function miniOf(edges: number[], ref: number): number {
  return Math.min(floorIndex(edges, ref), edges.length - 2)
}

// "구간 시간" 색 — 그 랩의 미니 구간 시간을 세션 최속(보라) · 평균(초록 ↔ 빨강)과 견준다. 평균은 완주 랩만 (엔진 값)
export function paceColors(track: TrackResponse, sec: SectorsResponse, lap: number): string[] | null {
  const L = sec.laps.find((l) => l.lap === lap)
  if (!track.ref_m || !L || !sec.best || !sec.mean) return null
  const edges = sec.mini.edges_ref_m
  const scale = L.mini_s.map((_, k) => Math.max(sec.mean!.mini_s[k] - sec.best!.mini_s[k], 0.02))
  const v = L.mini_s.map((t, k) => Math.max(-1, Math.min(1, (t - sec.mean!.mini_s[k]) / scale[k])))
  return track.ref_m.map((r) => {
    const k = miniOf(edges, r)
    return L.mini_s[k] <= sec.best!.mini_s[k] ? PURPLE : paceColor(smoothAt(edges, v, r))
  })
}

// "시간 차" 색 — 미니 구간마다 비교 랩이 기준보다 빠르면 초록 · 느리면 빨강 (가장 큰 차이를 가장 진하게)
export function deltaColors(track: TrackResponse, edges: number[], miniDelta: number[]): string[] | null {
  if (!track.ref_m || !miniDelta.length) return null
  const top = Math.max(...miniDelta.map(Math.abs), 0.01)
  const v = miniDelta.map((d) => d / top)
  return track.ref_m.map((r) => paceColor(smoothAt(edges, v, r)))
}

// 채널 색 — 정해진 값(가속 · 타력 · 제동)은 그 색, 나머지는 채널마다 색표 (사이트_설계.md §7 "트랙맵 색" 검토안)
const LEVEL_COLORS: Record<number, string> = { 1: '#18b04f', 0: '#8e959e', [-1]: '#e2261c' }
const GRAY = [110, 116, 124]

export interface Palette {
  kind: 'ramp' | 'magnitude' | 'regen' | 'hue'
  from: number[]
  to: number[]
  legend: string
}

export function channelPalette(id: string, unit: string): Palette {
  if (/apps|throttle|torque_cmd/.test(id)) return { kind: 'ramp', from: GRAY, to: [24, 176, 79], legend: '회색 → 진한 초록 (많이 밟음)' }
  if (/brake/.test(id)) return { kind: 'ramp', from: GRAY, to: [226, 38, 28], legend: '회색 → 진한 빨강 (세게 밟음)' }
  if (/^imu\.(lat|long)_g$/.test(id)) return { kind: 'magnitude', from: GRAY, to: [255, 255, 255], legend: '크기만 — 0 g 회색 → 큰 값 흰색 (방향 없음)' }
  if (/^battery\.power/.test(id)) return { kind: 'regen', from: GRAY, to: [235, 235, 235], legend: '회생(−) 노랑 · 방전 회색 → 흰색' }
  if (unit === '°C') return { kind: 'hue', from: [], to: [], legend: '파랑 낮음 → 빨강 높음' }
  return { kind: 'hue', from: [], to: [], legend: '파랑 낮음 → 빨강 높음' }
}

export function channelColors(values: (number | null)[], levels?: { value: number }[], id = '', unit = ''): { colors: (string | null)[]; min: number; max: number } {
  const ok = values.filter((v): v is number => v !== null)
  const min = ok.length ? Math.min(...ok) : 0
  const max = ok.length ? Math.max(...ok) : 1
  const p = channelPalette(id, unit)
  const top = Math.max(...ok.map(Math.abs), 1e-9)
  const colors = values.map((v) => {
    if (v === null) return null
    if (levels) return LEVEL_COLORS[Math.round(v)] ?? '#8e959e'
    if (p.kind === 'magnitude') return mix(p.from, p.to, Math.abs(v) / top) // 화면 처리 — 값은 그대로, 색만 |값| (계약 §6-6)
    if (p.kind === 'regen') return v < 0 ? mix([200, 170, 40], [250, 220, 60], Math.min(1, -v / top)) : mix(p.from, p.to, Math.max(0, v) / Math.max(max, 1e-9))
    if (p.kind === 'ramp') return mix(p.from, p.to, max > min ? (v - min) / (max - min) : 0)
    const t = max > min ? (v - min) / (max - min) : 0.5
    return `hsl(${Math.round(220 - 220 * t)}, 75%, 55%)`
  })
  return { colors, min, max }
}

// 그래프 채널 목록 · 겹치기에서 빼는 채널 — 위치(경기장 원점에서 동 · 북 m)는 지도 좌표라 시간 · 거리 그래프로 볼 일이 없다
// (사용자 결정 2026-10-29). 트랙맵은 엔진의 코스 좌표(get_track)를 따로 받으므로 상관없다. 옛 주소에 있으면 그대로 그린다
export const GRAPH_HIDDEN = new Set(['gps.x_m', 'gps.y_m'])

// 0 을 가운데 두는 채널 — 좌 · 우, 앞 · 뒤처럼 두 방향이 같은 무게인 것 (가속도 · 요레이트 · 조향각).
// 부호가 있어도 한쪽이 주가 되는 것(배터리 전류 · 전력 — 방전 +)과 위치(원점이 경기장마다 정한 아무 점이라 0 에 뜻이 없다)는 아니다
export function centerZero(c: { unit: string; sign: string } | undefined): boolean {
  return !!c?.sign && ['g', '°/s', '°'].includes(c.unit)
}
