import { describe, expect, it } from 'vitest'

import type { LiveEvent, LiveLap, LiveState, LiveTrack } from '../types'
import { alertView, carOf, CARS, VIEW_TILES } from './cars'
import {
  ageText, bridgeShort, clock, deviceView, emptyBuf, eventText, foldEvents, gapBands, gaugePos, hasLapValues, lampOf, lapPoints, lapText, linkView, mapBounds,
  mergeChart, miniRange, niceAxis, offState, outageText, pathRuns, pctText, rateText, scaleOf, shownStatus, signedText, sinceOf, sourceWord,
  sparkPoints, tileOr, tileSource, TRACK_MIN_MS, trackWait, trail, valueAt, worstLamp,
} from './model'

const chart = (t: number[], speed: (number | null)[], extra: Record<string, (number | null)[]> = {}): LiveState['chart'] => ({
  t,
  series: { 'gps.speed_kmh': speed, ...extra },
})

const link = (over: Partial<LiveState['link']> = {}): LiveState['link'] => ({
  status: 'ok', age_s: 0.4, period_s: 0.5, rate_10s: 0.95, outages: 0, longest_s: null, since_s: null, ...over,
})

describe('실시간 그래프 값 잇기', () => {
  it('받은 줄을 뒤에 붙이고, since 로 다음에 물을 t 를 준다', () => {
    let b = mergeChart(emptyBuf(), chart([100, 100.5], [10, 11]), 101)
    b = mergeChart(b, chart([101], [12]), 101.5)
    expect(b.t).toEqual([100, 100.5, 101])
    expect(b.series['gps.speed_kmh']).toEqual([10, 11, 12])
    expect(sinceOf(b)).toBe(101)
  })
  it('가진 마지막 t 이하는 다시 붙이지 않는다', () => {
    const b = mergeChart(mergeChart(emptyBuf(), chart([100, 100.5], [10, 11]), 101), chart([100.5, 101], [99, 12]), 101)
    expect(b.t).toEqual([100, 100.5, 101])
    expect(b.series['gps.speed_kmh']).toEqual([10, 11, 12])
  })
  it('창(최근 300 s) 밖은 앞에서 자르고, 늦게 생긴 시리즈는 앞을 비워 길이를 맞춘다', () => {
    let b = mergeChart(emptyBuf(), chart([0, 100, 200], [1, 2, 3]), 200)
    b = mergeChart(b, chart([400], [4], { 'battery.power_kw': [30] }), 400)
    expect(b.t).toEqual([100, 200, 400])
    expect(b.series['gps.speed_kmh']).toEqual([2, 3, 4])
    expect(b.series['battery.power_kw']).toEqual([null, null, 30])
  })
  it('가진 값이 서버의 지금보다 앞서 있으면 처음부터 다시 쌓는다 (서버 시계가 바뀜)', () => {
    const b = mergeChart(mergeChart(emptyBuf(), chart([5000], [1]), 5000), chart([200], [2]), 200)
    expect(b.t).toEqual([200])
  })
  it('최근 궤적 — 위치가 둘 다 있는 줄만, 최근 30 s', () => {
    const b = mergeChart(emptyBuf(), chart([0, 80, 90, 95], [1, 1, 1, 1], { 'gps.x_m': [1, 2, null, 4], 'gps.y_m': [1, 2, 3, 4] }), 100)
    expect(trail(b, 100)).toEqual([[2, 2], [4, 4]])
  })
})

describe('실시간 표시', () => {
  it('서버 응답이 끊기면 색이 있는 상태도 낡음으로 — 무수신은 그대로', () => {
    expect(shownStatus('danger', true)).toBe('stale')
    expect(shownStatus('ok', true)).toBe('stale')
    expect(shownStatus('none', true)).toBe('none')
    expect(shownStatus('warn', false)).toBe('warn')
  })
  it('수신 상태 — 늦음은 주의, 끊김은 끊긴 지 몇 초로', () => {
    expect(linkView(link())).toEqual({ text: '수신 0.4 s', tone: 'ok' })
    expect(linkView(link({ status: 'late', age_s: 4.2 })).tone).toBe('warn')
    expect(linkView(link({ status: 'lost', age_s: 14.2, since_s: 14.2 }))).toEqual({ text: '수신 끊김 14 s', tone: 'danger' })
    expect(linkView(link({ status: 'none', age_s: null })).tone).toBe('none')
    expect(rateText(link())).toBe('10 s 95 %')
    expect(outageText(link({ outages: 1, longest_s: 14.2 }))).toBe('끊김 1번 · 최장 14 s')
    expect(outageText(link())).toBe('끊김 없음')
  })
  it('지난 시간 · 랩 시간 · 한국 시각', () => {
    expect(ageText(3.24)).toBe('3.2 s 전')
    expect(ageText(42)).toBe('42 s 전')
    expect(ageText(300)).toBe('5분 전')
    expect(lapText(66.4)).toBe('1:06.4')
    expect(lapText(31.2)).toBe('31.2')
    expect(lapText(null)).toBe('—')
    expect(clock(Date.UTC(2026, 10, 14, 3, 41, 7) / 1000)).toBe('12:41:07') // UTC 03:41 = 한국 12:41
  })
})

describe('수신이 꺼졌을 때의 틀', () => {
  it('뷰가 쓰는 칸이 전부 무수신이고 값은 없다 · 원천 5개도 무수신', () => {
    const s = offState(100)
    const ids = new Set(s.tiles.map((t) => t.id))
    const used = Object.values(VIEW_TILES).flat().filter((id) => !['motor.error_raw', 'pedal.fault_raw'].includes(id)) // 비트는 이름으로만
    expect(used.filter((id) => !ids.has(id))).toEqual([])
    expect(s.tiles.every((t) => t.status === 'none' && t.value === null)).toBe(true)
    expect(s.sources.map((x) => x.status)).toEqual(['none', 'none', 'none', 'none', 'none'])
    expect(linkView(s.link).text).toBe('수신 없음')
  })
  it('엔진이 안 보낸 칸은 무수신 칸으로 (이름은 정해 둔 것, 모르면 id)', () => {
    const t = tileOr(new Map(), 'bms.dcl_kw')
    expect([t.status, t.value, t.label]).toEqual(['none', null, '방전 출력 한계 (DCL)'])
    expect(tileOr(new Map(), '새.채널').label).toBe('새.채널')
  })
})

describe('차량 · 뷰', () => {
  it('25Evo 만 실시간 뷰 다섯 (계기판 · 그래프 · 배터리 · 인버터 · 모터 · 수신 · DAQ) — LEF-26 송출 불가 · LEF-27 구현 중', () => {
    expect(carOf('LEF-25evo')?.views).toEqual(['main', 'graph', 'battery', 'powertrain', 'link'])
    expect(carOf('LEF-26')?.live).toBe('none')
    expect(carOf('LEF-27')?.live).toBe('building')
    expect(carOf('LEF-99')).toBeNull()
    expect(CARS[0].device).toBe('Monolith')
  })
  it('경고 띠 — 그 칸이 지금 뷰에 없을 때만 그 뷰로 가는 링크', () => {
    const v = carOf('LEF-25evo')!.views!
    expect(alertView('W-TB-NOW', 'bms.temp_max_c', 'main', v)).toBeNull() // 메인에도 셀 최고 온도 칸
    expect(alertView('W-TB-NOW', 'bms.temp_max_c', 'powertrain', v)).toBe('main')
    expect(alertView('W-HV-OFF', 'motor.vdc_v', 'battery', v)).toBe('powertrain')
    expect(alertView('W-LINK', null, 'main', v)).toBe('link')
    expect(alertView('W-SRC-gps', null, 'link', v)).toBeNull()
    expect(alertView('W-DEV', null, 'main', v)).toBe('link') // Monolith 부품 치명 — 수신 · DAQ 뷰의 부품 상태
    expect(alertView('W-P80', null, 'main', v)).toBeNull() // 칸을 모르면 링크 없음
    expect(alertView('W-TB-NOW', 'bms.temp_max_c', 'graph', v)).toBe('main') // 그래프 뷰엔 칸이 없어 계기판으로
  })
})

describe('사건 접기', () => {
  const ev = (t: number, kind: string, text: string, extra: Partial<LiveEvent> = {}): LiveEvent => ({ t, kind, text, ...extra })
  it('같은 원천의 no data 가 두 번 이상이면 가장 최근 자리에 한 줄 — 최장은 gap_s 로만', () => {
    const rows = foldEvents([
      ev(90, 'source_back', 'GPS data back — 5.6 s gap', { source: 'gps', gap_s: 5.6 }),
      ev(80, 'source_lost', 'GPS no data', { source: 'gps' }),
      ev(70, 'source_back', 'GPS data back — 6.2 s gap', { source: 'gps', gap_s: 6.2 }),
      ev(60, 'source_lost', 'GPS no data', { source: 'gps' }),
      ev(50, 'alert', '셀 최고 온도 55 °C 이상 (57 °C)'),
    ])
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({ t: 90, kind: 'source_fold', source: 'gps', text: 'GPS no data ×2', count: 2, longest_s: 6.2 })
    expect(eventText(rows[0])).toBe('GPS no data ×2 · 최장 6 s')
    expect(rows[1].kind).toBe('alert')
  })
  it('한 번뿐이면 그대로, 다른 사건이 끼면 덩어리가 끊긴다, 원천마다 따로', () => {
    const rows = foldEvents([
      ev(100, 'source_lost', 'GPS no data', { source: 'gps' }),
      ev(95, 'source_lost', 'BMS no data', { source: 'bms' }),
      ev(90, 'source_lost', 'GPS no data', { source: 'gps' }),
      ev(80, 'lost', 'Monolith no data'),
      ev(70, 'source_lost', 'GPS no data', { source: 'gps' }),
    ])
    expect(rows.map((r) => r.text)).toEqual(['GPS no data ×2', 'BMS no data', 'Monolith no data', 'GPS no data'])
    expect(eventText(rows[0])).toBe('GPS no data ×2') // gap_s 가 없으면 최장 없이
  })
})

describe('게이지 · 추세선', () => {
  const tile = (id: string, over = {}) => ({ id, label: id, unit: '', sign: '', digits: 0, value: 50, age_s: 0.3, status: 'ok' as const, alert: null, ...over })
  it('엔진이 범위를 주면 그것을, 없으면 정해진 값(규정 한도 · 0 ~ 100 %)만, 그 밖은 게이지 없음', () => {
    expect(scaleOf(tile('battery.power_kw'))?.danger).toBe(80)
    expect(scaleOf(tile('bms.temp_max_c'))?.danger).toBe(60)
    expect(scaleOf(tile('bms.temp_max_c'))?.warn).toBeNull() // 55 °C 는 가정 — 엔진 문턱 파일에만
    expect(scaleOf(tile('bms.cell_min_v'))).toBeNull()
    const s = { min: 2.5, max: 4.2, warn: 3.0, danger: 2.8, dir: '<=' as const }
    expect(scaleOf(tile('bms.cell_min_v', { scale: s }))).toEqual(s)
  })
  it('게이지 위치는 범위 밖이면 끝에 붙는다', () => {
    const s = { min: -20, max: 90, warn: null, danger: 80, dir: '>=' as const }
    expect(gaugePos(-20, s)).toBe(0)
    expect(gaugePos(200, s)).toBe(1)
    expect(gaugePos(35, s)).toBeCloseTo(0.5)
  })
  it('수신률 · 부호 숫자 · 원천 상태 낱말', () => {
    expect(pctText(0.954)).toBe('95 %')
    expect(pctText(null)).toBe('—')
    expect(signedText(-2.5, 1)).toBe('−2.5')
    expect(signedText(0.5, 1)).toBe('+0.5')
    expect(sourceWord('ok', false).text).toBe('정상')
    expect(sourceWord('ok', true).text).toBe('낡음') // 서버 응답이 끊기면
    expect(sourceWord('none', true).text).toBe('수신 없음')
  })
  it('추세선 — 최근 60 s 를 0 ~ 1 로, 끊긴 곳은 null', () => {
    const b = mergeChart(emptyBuf(), chart([0, 50, 60, 70, 100], [5, 10, null, 20, 30]), 100)
    expect(sparkPoints(b, 'gps.speed_kmh', 100)).toEqual([[1 / 6, 0], null, [0.5, 0.5], [1, 1]]) // 40 s 이후만: 50 · 60(빈) · 70 · 100
  })
  it('장치 상태는 단정하지 않는다 — 연결 끊김 · 확인 중, 장치 자리에 차의 송신 장치 이름', () => {
    expect(deviceView({ status: 'disconnected', boot_at: null, firmware: null, boots: 0 }).text).toBe('장치 연결 끊김')
    expect(deviceView({ status: 'unknown', boot_at: null, firmware: null, boots: 0 }, 'Monolith').text).toBe('Monolith 확인 중')
    expect(deviceView({ status: 'on', boot_at: null, firmware: null, boots: 0 }, 'Monolith').text).toBe('Monolith 켜짐')
  })
})

describe('트랙맵 — 달린 궤적 · 다시 받기 · 범위', () => {
  const trk = (rev: number | null): LiveTrack => ({
    frame_id: 'venue/x', line: { x_m: [0, 10], y_m: [0, 0] }, gate: { x: 0, y: 0, heading_deg: 90, half_width_m: 30 },
    ...(rev === null ? {} : { path: { x_m: [], y_m: [], rev, step_m: 3, min_kmh: 10 } }),
  })

  it('처음엔 바로, 못 받았으면 간격마다 다시 받는다', () => {
    expect(trackWait(5, null, null, 1000)).toBe(0)
    expect(trackWait(5, null, 1000, 4000)).toBe(TRACK_MIN_MS - 3000)
    expect(trackWait(null, null, 1000, 1000 + TRACK_MIN_MS + 5)).toBe(0)
  })

  it('track_rev 가 받은 궤적과 다를 때만, 10 s 에 한 번까지 다시 받는다', () => {
    expect(trackWait(7, trk(7), 0, 60000)).toBeNull() // 그대로
    expect(trackWait(8, trk(7), 0, 3000)).toBe(TRACK_MIN_MS - 3000) // 늘었지만 간격 전
    expect(trackWait(8, trk(7), 0, 60000)).toBe(0)
    expect(trackWait(null, trk(7), 0, 60000)).toBeNull() // 경기장 없음
    expect(trackWait(undefined, trk(null), 0, 60000)).toBeNull() // 옛 엔진 — track_rev · path 없음
  })

  it('달린 궤적은 null 에서 끊고, 점 하나짜리 조각은 뺀다', () => {
    const p = { x_m: [0, 1, null, 5, null, 7, 8, 9], y_m: [0, 1, null, 5, null, 7, 8, 9], rev: 6, step_m: 3, min_kmh: 10 }
    expect(pathRuns(p)).toEqual([[[0, 0], [1, 1]], [[7, 7], [8, 8], [9, 9]]])
    expect(pathRuns(undefined)).toEqual([])
  })

  it('범위는 기준선 밖의 궤적 · 지금 위치까지 담는다', () => {
    expect(mapBounds([[0, 0], [10, 0], [-40, 25], [3, -8]])).toEqual({ minX: -40, maxX: 10, minY: -8, maxY: 25 })
    expect(mapBounds([[Number.NaN, 1]])).toBeNull()
  })
})

describe('계기판 — 연결 불 · 작은 그래프 축 · 랩 꼭짓점 (§6-1)', () => {
  const state = (over: Partial<LiveState> = {}): LiveState => ({
    ...offState(100),
    link: link(),
    sources: [
      { id: 'bms', label: 'BMS', status: 'ok', age_s: 0.2, rate_10s: 0.95, rate_status: 'ok' },
      { id: 'inverter', label: '인버터', status: 'ok', age_s: 0.2, rate_10s: 0.6, rate_status: 'warn' },
      { id: 'pedal', label: '가속페달', status: 'none', age_s: null, rate_10s: null, rate_status: null },
      { id: 'gps', label: 'GPS', status: 'stale', age_s: 4, rate_10s: null, rate_status: null, fix_10s: 0.4 },
      { id: 'lv', label: 'LV', status: 'ok', age_s: 0.2, rate_10s: null, rate_status: null },
    ],
    ...over,
  })

  it('칸 → 원천은 채널 이름 앞부분으로', () => {
    expect(['gps.speed_kmh', 'battery.power_kw', 'bms.soc_pct', 'motor.t_igbt_c', 'pedal.apps_pct', 'lv.voltage_v', 'brake.ain1_v', 'x.y'].map(tileSource))
      .toEqual(['gps', 'bms', 'bms', 'inverter', 'pedal', 'lv', 'lv', null])
  })

  it('페달 칸 불 — 가속 · 브레이크 중 나쁜 쪽 색, 글은 둘 다 (같은 글은 한 번)', () => {
    const s = state()
    const l = worstLamp(lampOf(s, 'pedal.apps_pct', false, false), lampOf(s, 'brake.ain1_v', false, false))
    expect(l.tone).toBe('danger')
    expect(l.text).toContain(' · ')
    expect(worstLamp({ tone: 'ok', text: 'a' }, { tone: 'stale', text: 'a' })).toEqual({ tone: 'stale', text: 'a' })
    expect(worstLamp({ tone: 'warn', text: 'a' }, { tone: 'none', text: 'b' }).tone).toBe('warn')
  })

  it('브레이크 칸 — 엔진이 안 보내면 무수신 칸 (이름 있음), 눈금은 0 ~ ADC 끝', () => {
    const t = tileOr(new Map(), 'brake.ain1_v')
    expect(t.status).toBe('none')
    expect(t.unit).toBe('V')
    expect(scaleOf(t)).toMatchObject({ min: 0, max: 4.096 })
  })

  it('연결 불 — 원천 수신률 상태 그대로, 없으면 원천 상태, LV 는 장치 수신, 멈춤은 회색 · 꺼짐은 빈 불', () => {
    const s = state()
    expect(lampOf(s, 'battery.power_kw', false, false).tone).toBe('ok')
    expect(lampOf(s, 'motor.t_motor_c', false, false).tone).toBe('warn')
    expect(lampOf(s, 'pedal.apps_pct', false, false).tone).toBe('danger') // 받은 적 없음 = 빨강
    expect(lampOf(s, 'gps.speed_kmh', false, false)).toEqual({ tone: 'warn', text: 'GPS 위치 잡음 40 %' })
    expect(lampOf(s, 'lv.voltage_v', false, false).tone).toBe('ok')
    expect(lampOf(state({ link: link({ status: 'lost' }) }), 'lv.voltage_v', false, false).tone).toBe('danger')
    expect(lampOf(s, 'battery.power_kw', true, false).tone).toBe('stale')
    expect(lampOf(s, 'battery.power_kw', false, true).tone).toBe('none')
  })

  it('속도 게이지는 0 ~ 120 km/h 눈금만 (문턱 없음)', () => {
    const t = tileOr(new Map(), 'gps.speed_kmh')
    expect(scaleOf(t)).toEqual({ min: 0, max: 120, warn: null, danger: null, dir: '>=' })
  })

  it('축 — 위 · 가운데 · 아래가 읽기 좋은 수, 한도 선 · 0 을 담는다', () => {
    expect(niceAxis(3.41, 3.78)).toEqual({ lo: 3.4, hi: 3.8, digits: 1 })
    expect(niceAxis(0, 83)).toEqual({ lo: 0, hi: 100, digits: 0 })
    expect(niceAxis(50, 50).lo).toBeLessThan(50)
    expect(miniRange([12, 40, null], [80], true)).toEqual({ lo: 0, hi: 80, digits: 0 })
    expect(miniRange([3.6, 3.7], [3.0, 2.8])).toEqual({ lo: 2.8, hi: 4, digits: 1 }) // 2.8 · 3.4 · 4.0
    expect(miniRange([-8, 60], [80], true)).toEqual({ lo: -20, hi: 100, digits: 0 }) // 회생(−) 이 있으면 0 아래로
    expect(miniRange([null], [])).toEqual({ lo: 0, hi: 1, digits: 1 }) // 0 · 0.5 · 1
    expect(miniRange([null], [3.0, 2.8], false, [2.5, 4.2])).toEqual({ lo: 2.5, hi: 4.5, digits: 1 }) // 받은 값이 없으면 게이지 범위로 (2.5 · 3.5 · 4.5)
  })

  const lap = (n: number, over: Partial<LiveLap> = {}): LiveLap => ({
    lap: n, t_start: n * 60, t_end: n * 60 + 60, lap_time_s: 60, net_wh: null, cover: null, soc_start_pct: null, soc_end_pct: null,
    soc_delta_pct: null, cell_v_min: null, cell_t_max_c: 40 + n, ...over,
  })
  const laps = (rows: LiveLap[], current: LiveLap | null): LiveState['laps'] => ({ count: rows.length, current_s: null, last_s: null, best_s: null, basis: '', rows, current })

  it('랩 꼭짓점 — 값이 있는 끝난 랩(끝난 시각) + 이번 랩, 최근 keep 랩만', () => {
    const l = laps([lap(1), lap(2, { cell_t_max_c: null }), lap(3)], lap(4, { cell_t_max_c: 47 }))
    expect(lapPoints(l, 'cell_t_max_c')).toEqual({ done: [{ lap: 1, t: 120, v: 41 }, { lap: 3, t: 240, v: 43 }], cur: { lap: 4, v: 47 } })
    expect(lapPoints(l, 'cell_t_max_c', 1).done).toEqual([{ lap: 3, t: 240, v: 43 }])
    expect(hasLapValues(l, 'cell_t_max_c')).toBe(true)
    expect(hasLapValues(l, 'motor_t_max_c')).toBe(false) // 엔진이 아직 안 줌 → 시간축 선
    expect(lapPoints(null, 'cell_t_max_c')).toEqual({ done: [], cur: null })
  })

  it('끊긴 띠 — 모든 시리즈가 빈 줄이 이어진 구간 (앞 줄부터 다시 받은 줄까지), 2 s 보다 짧으면 뺀다', () => {
    const b = mergeChart(emptyBuf(), chart([0, 1, 2, 3, 4, 5], [5, null, null, 6, null, null], { 'bms.soc_pct': [60, null, null, 60, 60, null] }), 5, 600)
    expect(gapBands(b, 0)).toEqual([[0, 3]])
    expect(gapBands(b, 0, 0)).toEqual([[0, 3], [4, 5]])
    expect(gapBands(b, 3.5, 0)).toEqual([[4, 5]])
  })

  it('짧은 빈 칸은 잇는다 — 앞뒤 받은 값 사이가 2 s 보다 짧으면 undefined, 길면 null (끊음), 처음 · 끝 빈 칸은 그대로', () => {
    const t = [0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5]
    const v = [null, 1, null, 2, null, null, null, null, 3, 4, null]
    expect(bridgeShort(t, v)).toEqual([null, 1, undefined, 2, null, null, null, null, 3, 4, null])
    expect(bridgeShort(t, v, 3)).toEqual([null, 1, undefined, 2, undefined, undefined, undefined, undefined, 3, 4, null])
    expect(bridgeShort([0, 0.5, 1], [1, 2, 3])).toEqual([1, 2, 3])
  })

  it('커서 값 — 가장 가까운 줄', () => {
    const b = mergeChart(emptyBuf(), chart([0, 1, 2, 3], [5, 6, null, 8]), 3, 600)
    expect(valueAt(b, 'gps.speed_kmh', 0.9)).toBe(6)
    expect(valueAt(b, 'gps.speed_kmh', 2.2)).toBeNull()
    expect(valueAt(b, 'gps.speed_kmh', 99)).toBe(8)
    expect(valueAt(b, 'none', 1)).toBeNull()
  })
})
