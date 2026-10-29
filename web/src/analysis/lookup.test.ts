import { describe, expect, it } from 'vitest'

import type { DeltaResponse, SectorsResponse, SeriesResponse, TrackResponse } from '../types'
import { channelColors, deltaColors, floorIndex, nearestIndex, paceColor, paceColors, PURPLE, trackIndex, valueAt, xOfTrackPoint } from './lookup'
import { axisData, DELTA, tracesFor } from './traces'

const track = (over: Partial<TrackResponse> = {}): TrackResponse => ({
  sid: 'A', lap: 2, t_s: [100, 101, 102, 103], x_m: [0, 1, 2, 3], y_m: [0, 0, 0, 0], dist_m: [0, 10, 20, 30], ref_m: [0, 10, 20, 30],
  gate: { x_m: 0, y_m: 0, heading_deg: 90, half_width_m: 30 }, frame_id: 'f', direction: 'ccw', frame: '', ...over,
})

describe('가까운 칸 찾기', () => {
  it('가장 가까운 칸 · 이하인 마지막 칸', () => {
    expect(nearestIndex([0, 10, 20], 14)).toBe(1)
    expect(nearestIndex([0, 10, 20], 16)).toBe(2)
    expect(nearestIndex([0, 10, 20], -5)).toBe(0)
    expect(floorIndex([0, 10, 20], 19.9)).toBe(1)
    expect(floorIndex([0, 10, 20], 99)).toBe(2)
    expect(floorIndex([0, 10, 20], -1)).toBe(0)
  })
  it('커서 값 — 시간축은 랩 시작만큼 옮겨서 찾고, 칸 간격의 두 배보다 멀면 없음', () => {
    const r = { x: { values: [100, 100.5, 101] }, series: [{ channel_id: 'c', values: [1, 2, 3] }] } as unknown as SeriesResponse
    expect(valueAt(r, 'c', 0.4, 100)).toBe(2)
    expect(valueAt(r, 'c', 5, 100)).toBeNull()
    expect(valueAt(r, 'nope', 0.4, 100)).toBeNull()
  })
  it('그래프 x ↔ 트랙맵 점 — 거리축은 기준 거리, 시간축은 세션 시간', () => {
    const t = track()
    expect(trackIndex(t, 19, 'distance', 100)).toBe(2)
    expect(trackIndex(t, 1.2, 'time', 100)).toBe(1)
    expect(trackIndex(track({ ref_m: null }), 101.2, 'distance', 0)).toBe(1) // 세션 전체 궤적은 시각으로
    expect(xOfTrackPoint(t, 3, 'distance', 100)).toBe(30)
    expect(xOfTrackPoint(t, 3, 'time', 100)).toBe(3)
  })
})

describe('트랙맵 색', () => {
  const sec: SectorsResponse = {
    sid: 'A', frame_id: 'f', sectors: [], mini: { mini_m: 10, edges_ref_m: [0, 10, 20, 30], count: 3 },
    laps: [{ lap: 2, sector_s: [], mini_s: [1.0, 2.0, 3.5] }],
    best: { sector_s: [], sector_lap: [], mini_s: [1.0, 1.5, 3.0], mini_lap: [2, 1, 1] }, mean: { sector_s: [], mini_s: [1.2, 2.2, 3.1], n_laps: 3 },
  }
  it('보라 = 그 미니 구간이 세션 최속, 나머지는 평균보다 빠르면 초록 · 느리면 빨강', () => {
    const c = paceColors(track(), sec, 2)!
    expect(c[0]).toBe(PURPLE) // 미니 1 은 최속
    expect(c[3]).toBe(paceColor(1)) // 미니 3 은 평균보다 느림 (가장 진한 빨강 쪽)
    expect(paceColors(track({ ref_m: null }), sec, 2)).toBeNull()
    expect(paceColors(track(), sec, 9)).toBeNull()
  })
  it('초록 · 빨강은 차이가 클수록 진하게', () => {
    expect(paceColor(-0.1)).not.toBe(paceColor(-1))
    expect(paceColor(-1)).toBe('rgb(24, 176, 79)')
    expect(paceColor(1)).toBe('rgb(226, 38, 28)')
  })
  it('시간 차 색 · 채널 색', () => {
    expect(deltaColors(track(), [0, 10, 20, 30], [-0.2, 0, 0.4])).toHaveLength(4)
    const lv = channelColors([1, 0, -1, null], [{ value: 1 }])
    expect(lv.colors).toEqual(['#18b04f', '#8e959e', '#e2261c', null])
    expect(channelColors([10, 20]).min).toBe(10)
  })
})

describe('시간 차 축', () => {
  it('비교 랩마다 엔진의 시간 차를 기준 거리 위에 — 기준 랩은 선이 없다', () => {
    const d = { x: { values: [0, 100, 200] }, delta_s: [0, 0.1, 0.3] } as unknown as DeltaResponse
    const { data, meta } = axisData(tracesFor(7, [3]), new Map(), [DELTA], 'distance', new Map([['cmp0', d]]))
    expect(meta.map((m) => `${m.trace.key}:${m.kind}`)).toEqual(['cmp0:v', 'cmp0:min', 'cmp0:max'])
    expect(data[1]).toEqual([0, 0.1, 0.3])
    expect(axisData(tracesFor(7, [3]), new Map(), [DELTA], 'time', new Map([['cmp0', d]])).meta).toEqual([]) // 시간축에는 없다
  })
})
