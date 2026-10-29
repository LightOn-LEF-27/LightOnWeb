import { describe, expect, it } from 'vitest'

import type { QualityItem, SeriesResponse } from '../types'
import { addToAxis, readState, resolveState, toggleChannel, writeState } from './state'
import { axisData, DELTA, mergeDetail, shadeRanges, tracesFor, xShift } from './traces'
import { sameAxes, sheetAxes, workbook, WORKBOOKS } from './workbooks'

describe('주소 상태', () => {
  it('쓰고 다시 읽으면 같다', () => {
    const s = { sid: 'DEMO-1_2027-05-08_S4', x: 'distance' as const, ref: 7, cmp: [{ sid: null, lap: 3 }, { sid: 'DEMO-2_2026-11-14_S1', lap: 2 }],
                axes: [['gps.speed_kmh'], ['imu.lat_g', 'imu.long_g']], wb: 'coach' }
    expect(readState(writeState(s))).toEqual(s)
    expect(writeState(s).toString()).toContain('ax=gps.speed_kmh%7Cimu.lat_g%2Cimu.long_g')
    expect(writeState(s).get('cmp')).toBe('3,DEMO-2_2026-11-14_S1~2')
  })
  it('정하지 않은 것과 잘못된 값', () => {
    const s = readState(new URLSearchParams('sid=A&x=weird&ref=x&cmp=1,a,2,3'))
    expect(s).toEqual({ sid: 'A', x: null, ref: null, cmp: [{ sid: null, lap: 1 }, { sid: null, lap: 2 }], axes: null, wb: null })
    expect(readState(new URLSearchParams('sid=A&ax=')).axes).toEqual([]) // 축을 다 뺀 상태와 "기본 축" 을 가른다
  })
  it('세션에 맞춰 채우기 — 기준은 최속 랩, 랩이 없으면 세션 전체 · 시간축', () => {
    const base = readState(new URLSearchParams('sid=A'))
    expect(resolveState(base, [1, 2, 3], 2)).toEqual({ x: 'distance', ref: 2, cmp: [] })
    const L = (lap: number, sid: string | null = null) => ({ sid, lap })
    expect(resolveState({ ...base, x: 'distance', ref: 9, cmp: [L(2)] }, [], null)).toEqual({ x: 'time', ref: 'all', cmp: [] })
    expect(resolveState({ ...base, ref: 1, cmp: [L(1), L(3), L(9)] }, [1, 2, 3], 2)).toEqual({ x: 'distance', ref: 1, cmp: [L(3)] })
    expect(resolveState({ ...base, ref: 'all', x: 'distance', cmp: [L(3)] }, [1, 2, 3], 2)).toEqual({ x: 'time', ref: 'all', cmp: [] })
    // 다른 세션의 랩은 그대로 (받아 보고 안다), 이 세션을 sid 로 적은 것은 같은 것으로, 겹친 것은 하나로
    expect(resolveState({ ...base, ref: 1, cmp: [L(4, 'B'), L(3, 'A'), L(3)] }, [1, 2, 3], 2).cmp).toEqual([L(4, 'B'), L(3)])
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
    expect(tracesFor('A', 7, [{ sid: null, lap: 3 }, { sid: 'B', lap: 5 }]).map((t) => [t.key, t.sid, t.lap, t.slot])).toEqual([
      ['ref', 'A', 7, 0], ['cmp0', 'A', 3, 1], ['cmp1', 'B', 5, 2]])
    expect(tracesFor('A', 'all', [])).toEqual([{ key: 'ref', sid: 'A', lap: null, slot: 0 }])
  })
  it('시간축에서 랩을 겹칠 때는 그 랩의 시작만큼 옮긴다 (거리축 · 세션 전체는 그대로)', () => {
    const r = resp([100, 101], [1, 2], 100, 4)
    expect(xShift(r, 'time', 4)).toBe(100)
    expect(xShift(r, 'distance', 4)).toBe(0)
    expect(xShift(r, 'time', null)).toBe(0)
  })
  it('두 랩을 x 로 합친다 — 엔진의 null 은 그대로(선을 끊는다), 합치며 생긴 빈칸은 undefined(선을 잇는다)', () => {
    const traces = tracesFor('A', 1, [{ sid: null, lap: 2 }])
    const map = new Map([['ref', resp([10, 11, 12], [5, null, 7], 10, 1)], ['cmp0', resp([30.5, 31.5], [6, 8], 30, 2)]])
    const { data, meta } = axisData(traces, map, ['gps.speed_kmh'], 'time')
    expect(data[0]).toEqual([0, 0.5, 1, 1.5, 2])
    expect(data[1]).toEqual([5, undefined, null, undefined, 7]) // 기준 랩 값
    expect(data[4]).toEqual([undefined, 6, undefined, 8, undefined]) // 비교 랩 값
    expect(meta.map((m) => `${m.trace.key}:${m.kind}`)).toEqual(['ref:v', 'ref:min', 'ref:max', 'cmp0:v', 'cmp0:min', 'cmp0:max'])
  })
  it('그 세션에 없는 채널은 계열에서 빠진다', () => {
    const { meta } = axisData(tracesFor('A', 1, []), new Map([['ref', resp([0], [1], 0, 1)]]), ['bms.soc_pct'], 'time')
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

describe('확대해서 다시 받은 값 끼우기', () => {
  it('detail 이 덮는 x 구간은 detail, 그 밖은 처음 값 — 옆으로 넘겨도 빈 곳이 없다', () => {
    const base = resp([0, 10, 20, 30, 40], [1, 2, 3, 4, 5], 0, 1)
    const detail = resp([12, 14, 16, 18], [20, 21, 22, 23])
    const m = mergeDetail(base, detail)!
    expect(m.x.values).toEqual([0, 10, 12, 14, 16, 18, 20, 30, 40])
    expect(m.series[0].values).toEqual([1, 2, 20, 21, 22, 23, 3, 4, 5])
    expect(m.series[0].y_max).toEqual([1, 2, 20, 21, 22, 23, 3, 4, 5])
    expect(m.laps).toEqual(base.laps) // 랩 시작(시간축 겹치기)은 처음 값의 것
  })
  it('한쪽이 없으면 있는 쪽 그대로', () => {
    const base = resp([0, 1], [1, 2])
    expect(mergeDetail(base, undefined)).toBe(base)
    expect(mergeDetail(undefined, base)).toBe(base)
    expect(mergeDetail(base, resp([], []))).toBe(base)
  })
})

describe('워크북', () => {
  it('워크시트는 이 세션에 있는 채널만, 축마다 둘까지 — 하나도 없으면 숨김', () => {
    const sh = { name: 't', axes: [['gps.speed_kmh'], ['pedal.apps_pct', 'pedal.brake_f_bar', 'pedal.torque_cmd_pct'], ['motor.inverter_c', 'motor.t_igbt_c']] }
    expect(sheetAxes(sh, new Set(['gps.speed_kmh', 'pedal.apps_pct', 'pedal.torque_cmd_pct', 'motor.t_igbt_c']))).toEqual([
      ['gps.speed_kmh'], ['pedal.apps_pct', 'pedal.torque_cmd_pct'], ['motor.t_igbt_c']])
    expect(sheetAxes(sh, new Set(['bms.soc_pct']))).toBeNull()
    expect(sheetAxes({ name: 'd', axes: [[DELTA]] }, new Set([DELTA]))).toBeNull() // 시간 차만 남는 워크시트는 숨김
  })
  it('워크북 고르기 — 모르는 이름은 기본', () => {
    expect(workbook('coach').name).toBe('드라이버 코칭')
    expect(workbook('nope').key).toBe(WORKBOOKS[0].key)
    expect(sameAxes([['a'], ['b', 'c']], [['a'], ['b', 'c']])).toBe(true)
  })
})
