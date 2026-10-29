import { describe, expect, it } from 'vitest'

import type { QualityItem, SeriesResponse } from '../types'
import { addToAxis, readState, resolveState, toggleChannel, writeState } from './state'
import { axisData, shadeRanges, tracesFor, xShift } from './traces'

describe('주소 상태', () => {
  it('쓰고 다시 읽으면 같다', () => {
    const s = { sid: 'DEMO-1_2027-05-08_S4', x: 'distance' as const, ref: 7, cmp: [3, 5], axes: [['gps.speed_kmh'], ['imu.lat_g', 'imu.long_g']] }
    expect(readState(writeState(s))).toEqual(s)
    expect(writeState(s).toString()).toContain('ax=gps.speed_kmh%7Cimu.lat_g%2Cimu.long_g')
  })
  it('정하지 않은 것과 잘못된 값', () => {
    const s = readState(new URLSearchParams('sid=A&x=weird&ref=x&cmp=1,a,2,3'))
    expect(s).toEqual({ sid: 'A', x: null, ref: null, cmp: [1, 2], axes: null })
    expect(readState(new URLSearchParams('sid=A&ax=')).axes).toEqual([]) // 축을 다 뺀 상태와 "기본 축" 을 가른다
  })
  it('세션에 맞춰 채우기 — 기준은 최속 랩, 랩이 없으면 세션 전체 · 시간축', () => {
    const base = readState(new URLSearchParams('sid=A'))
    expect(resolveState(base, [1, 2, 3], 2)).toEqual({ x: 'distance', ref: 2, cmp: [] })
    expect(resolveState({ ...base, x: 'distance', ref: 9, cmp: [2] }, [], null)).toEqual({ x: 'time', ref: 'all', cmp: [] })
    expect(resolveState({ ...base, ref: 1, cmp: [1, 3, 9] }, [1, 2, 3], 2)).toEqual({ x: 'distance', ref: 1, cmp: [3] })
    expect(resolveState({ ...base, ref: 'all', x: 'distance', cmp: [3] }, [1, 2, 3], 2)).toEqual({ x: 'time', ref: 'all', cmp: [] })
  })
  it('채널 누르기 — 새 축 / 빼기, 오른쪽 축 더하기', () => {
    expect(toggleChannel([['a']], 'b')).toEqual([['a'], ['b']])
    expect(toggleChannel([['a', 'b'], ['c']], 'b')).toEqual([['a'], ['c']])
    expect(toggleChannel([['a']], 'a')).toEqual([])
    expect(addToAxis([['a'], ['c']], 0, 'b')).toEqual([['a', 'b'], ['c']])
    expect(addToAxis([['a', 'b']], 0, 'c')).toEqual([['a', 'b']]) // 축 하나에 둘까지
  })
})

function resp(xs: number[], ys: (number | null)[], lapStart = 0, lap: number | null = null): SeriesResponse {
  return {
    sid: 'A', x: { kind: 'time', unit: 's', values: xs, lap, frame_id: null },
    series: [{ channel_id: 'gps.speed_kmh', unit: 'km/h', values: ys, y_min: ys, y_max: ys, samples: xs.length }],
    laps: lap === null ? [] : [{ lap, x_start: lapStart, x_end: lapStart + 10 }], reduced: { from_points: 3, to_points: 3, method: 'minmax', smooth_s: null },
  }
}

describe('랩 겹치기', () => {
  it('계열 = 기준 + 비교 (색 순서)', () => {
    expect(tracesFor(7, [3, 5]).map((t) => [t.key, t.lap, t.slot])).toEqual([['ref', 7, 0], ['cmp0', 3, 1], ['cmp1', 5, 2]])
    expect(tracesFor('all', [])).toEqual([{ key: 'ref', lap: null, slot: 0 }])
  })
  it('시간축에서 랩을 겹칠 때는 그 랩의 시작만큼 옮긴다 (거리축 · 세션 전체는 그대로)', () => {
    const r = resp([100, 101], [1, 2], 100, 4)
    expect(xShift(r, 'time', 4)).toBe(100)
    expect(xShift(r, 'distance', 4)).toBe(0)
    expect(xShift(r, 'time', null)).toBe(0)
  })
  it('두 랩을 x 로 합친다 — 엔진의 null 은 그대로(선을 끊는다), 합치며 생긴 빈칸은 undefined(선을 잇는다)', () => {
    const traces = tracesFor(1, [2])
    const map = new Map([['ref', resp([10, 11, 12], [5, null, 7], 10, 1)], ['cmp0', resp([30.5, 31.5], [6, 8], 30, 2)]])
    const { data, meta } = axisData(traces, map, ['gps.speed_kmh'], 'time')
    expect(data[0]).toEqual([0, 0.5, 1, 1.5, 2])
    expect(data[1]).toEqual([5, undefined, null, undefined, 7]) // 기준 랩 값
    expect(data[4]).toEqual([undefined, 6, undefined, 8, undefined]) // 비교 랩 값
    expect(meta.map((m) => `${m.trace.key}:${m.kind}`)).toEqual(['ref:v', 'ref:min', 'ref:max', 'cmp0:v', 'cmp0:min', 'cmp0:max'])
  })
  it('그 세션에 없는 채널은 계열에서 빠진다', () => {
    const { meta } = axisData(tracesFor(1, []), new Map([['ref', resp([0], [1], 0, 1)]]), ['bms.soc_pct'], 'time')
    expect(meta).toEqual([])
  })
  it('경고 음영 — 그 축의 블록 · warn 이상 · 시간축만, 화면 x 로 옮겨서', () => {
    const q: QualityItem[] = [
      { level: 'warn', code: 'a', block: 'gps', message: '', t_ranges: [[105, 106]] },
      { level: 'info', code: 'b', block: 'gps', message: '', t_ranges: [[1, 2]] },
      { level: 'warn', code: 'c', block: 'bms', message: '', t_ranges: [[1, 2]] },
    ]
    const r = resp([100], [1], 100, 4)
    expect(shadeRanges(q, ['gps.speed_kmh'], 'time', r, 4)).toEqual([[5, 6]])
    expect(shadeRanges(q, ['gps.speed_kmh'], 'distance', r, 4)).toEqual([])
  })
})
