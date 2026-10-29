// 그래프 계열 — 랩 하나 = 계열 하나 (기준 주황 · 비교 1 흰색 · 비교 2 파랑, 사이트_설계.md §7). 색은 늘 랩 색.
// 랩마다 x 값이 달라 uPlot.join 으로 한 표에 합친다: 합치며 생긴 빈칸은 undefined(선을 잇는다), 엔진이 비운 곳은 null(선을 끊는다)

import uPlot from 'uplot'

import type { DeltaResponse, QualityItem, SeriesResponse } from '../types'
import type { CmpLap, XMode } from './state'

export interface Trace {
  key: string // 'ref' · 'cmp0' · 'cmp1'
  sid: string // 이 계열의 세션 (비교 랩은 다른 세션일 수 있다)
  lap: number | null // null = 세션 전체
  slot: 0 | 1 | 2 // 색 순서 (기준 · 비교 1 · 비교 2)
}

export function tracesFor(sid: string, ref: number | 'all', cmp: CmpLap[]): Trace[] {
  const base: Trace = { key: 'ref', sid, lap: ref === 'all' ? null : ref, slot: 0 }
  return [base, ...cmp.map((c, i): Trace => ({ key: `cmp${i}`, sid: c.sid ?? sid, lap: c.lap, slot: (i + 1) as 1 | 2 }))]
}

// 시간축에서 랩을 겹칠 때는 그 랩의 시작을 0 으로 옮긴다 (화면의 "이동" — 값은 그대로)
export function xShift(resp: SeriesResponse, x: XMode, lap: number | null): number {
  if (x !== 'time' || lap === null) return 0
  return resp.laps.find((l) => l.lap === lap)?.x_start ?? 0
}

export interface SeriesMeta {
  trace: Trace
  channel: string
  axis: 0 | 1 // 0 = 왼쪽 세로축, 1 = 오른쪽 (점선)
  kind: 'v' | 'min' | 'max'
}

export interface AxisData {
  data: uPlot.AlignedData
  meta: SeriesMeta[] // data[1 + i] 의 뜻
}

export const DELTA = '@delta' // 화면의 "시간 차" 축 — 값은 엔진의 get_delta (비교 랩마다, 기준 거리 위)

// 축 하나의 그래프 표 — 계열마다 [x, (값 · 최소 · 최대) × 채널] 을 만들어 x 로 합친다.
// 시간 차는 x 칸이 그래프 값과 달라 따로 표를 만든다 (합칠 때 같은 x 축 — 기준 거리 — 위에 놓인다)
export function axisData(traces: Trace[], responses: Map<string, SeriesResponse>, channels: string[], x: XMode,
                         deltas: Map<string, DeltaResponse> = new Map()): AxisData {
  const tables: uPlot.AlignedData[] = []
  const meta: SeriesMeta[] = []
  const add = (t: Trace, ch: string, axis: number, xs: number[], cols: (number | null)[][]) => {
    tables.push([xs, ...cols] as uPlot.AlignedData)
    for (const kind of ['v', 'min', 'max'] as const) meta.push({ trace: t, channel: ch, axis: axis as 0 | 1, kind })
  }
  for (const t of traces) {
    const r = responses.get(t.key)
    channels.forEach((ch, axis) => {
      if (ch === DELTA) {
        const d = deltas.get(t.key)
        if (d && x === 'distance') add(t, ch, axis, d.x.values, [d.delta_s, d.delta_s, d.delta_s])
        return
      }
      const s = r?.series.find((v) => v.channel_id === ch)
      if (!r || !s) return // 이 랩의 세션에 없는 채널
      const shift = xShift(r, x, t.lap)
      add(t, ch, axis, shift ? r.x.values.map((v) => v - shift) : r.x.values, [s.values, s.y_min, s.y_max])
    })
  }
  if (tables.length === 0) return { data: [[]], meta: [] }
  return { data: tables.length === 1 ? tables[0] : uPlot.join(tables), meta }
}

// 품질 경고 구간 — 그 축의 채널 블록에 걸린 것만, 화면 x 로 옮겨서 (시간축만: 경고 구간은 세션 시간)
export function shadeRanges(quality: QualityItem[], channels: string[], x: XMode, ref: SeriesResponse | undefined, lap: number | null): [number, number][] {
  if (x !== 'time' || !ref) return []
  const blocks = new Set(channels.map((c) => c.split('.')[0]))
  const shift = xShift(ref, x, lap)
  return quality.filter((q) => q.level !== 'info' && blocks.has(q.block) && q.t_ranges).flatMap((q) => q.t_ranges!.map(([a, b]): [number, number] => [a - shift, b - shift]))
}

// 확대해서 다시 받은 촘촘한 값(detail)을 처음 받은 값(base)에 끼운다 — detail 이 덮는 x 구간은 detail, 그 밖은 base.
// 옆으로 넘기거나 확대 범위를 바꾸는 동안 새 값이 오기 전에도 빈 화면 대신 처음 값이 보이게 (값은 둘 다 엔진이 준 그대로)
export function mergeDetail(base: SeriesResponse | undefined, detail: SeriesResponse | undefined): SeriesResponse | undefined {
  const dx = detail?.x.values
  if (!base || !detail || !dx?.length) return base ?? detail
  const bx = base.x.values
  let a = 0
  while (a < bx.length && bx[a] < dx[0]) a++
  let b = a
  while (b < bx.length && bx[b] <= dx[dx.length - 1]) b++
  const cat = <T,>(bv: T[], dv: T[]): T[] => [...bv.slice(0, a), ...dv, ...bv.slice(b)]
  const series = base.series.map((s) => {
    const d = detail.series.find((v) => v.channel_id === s.channel_id)
    return d ? { ...s, values: cat(s.values, d.values), y_min: cat(s.y_min ?? s.values, d.y_min ?? d.values), y_max: cat(s.y_max ?? s.values, d.y_max ?? d.values) } : s
  })
  return { ...base, x: { ...base.x, values: cat(bx, dx) }, series, reduced: detail.reduced }
}
