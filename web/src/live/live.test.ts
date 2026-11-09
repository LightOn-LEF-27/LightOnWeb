import { describe, expect, it } from 'vitest'

import type { LiveEvent, LiveState } from '../types'
import { alertView, carOf, CARS, VIEW_TILES } from './cars'
import {
  ageText, clock, deviceView, emptyBuf, eventText, foldEvents, gaugePos, lapText, linkView, mergeChart, offState, outageText, pctText, rateText,
  scaleOf, shownStatus, signedText, sinceOf, sourceWord, sparkPoints, tileOr, trail,
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
  it('25Evo 만 실시간 뷰 넷 — LEF-26 송출 불가 · LEF-27 구현 중', () => {
    expect(carOf('LEF-25evo')?.views).toEqual(['main', 'battery', 'powertrain', 'link'])
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
