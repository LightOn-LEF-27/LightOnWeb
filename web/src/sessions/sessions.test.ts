import { describe, expect, it } from 'vitest'

import type { DataStatus, SessionRow } from '../types'
import { blockCell, blockColumns } from './blocks'
import { applyFilters, facet, readFilters, toggle, writeFilters } from './filters'
import { formatDuration, formatLapTime, formatStart, lapsCell, sessionName } from './format'

function row(over: Partial<SessionRow> = {}, status: Record<string, DataStatus['status']> = { gps: 'have' }): SessionRow {
  const data_status = Object.entries(status).map(([block, s]) => ({ block, title: `${block} 제목`, status: s, detail: '' }))
  return {
    sid: 'DEMO-1_2027-05-08_S3', title: 'DEMO-1 · 05-08 · 연습 2', name: '연습 2', car: 'DEMO-1', event_id: 'DEMO-1/2027-05-08_x',
    event_kind: '테스트', event_title: '데모 시험', discipline: '연습', place: '가상', driver_label: '드라이버 B',
    start: '2027-05-08T11:40:02.100+09:00', duration_s: 655, distance_km: 7.1, laps: 8, best_lap_s: 61.1, stats: {},
    frame_id: 'venue/x', split_by: 'gps', has_track: true, day_order: 4, run_no: null, purpose: null,
    blocks: { have: [], warn: [], seek: [], none: [] }, data_status, ...over,
  }
}

describe('세션 이름', () => {
  it('날짜 · R순번 · 출발 · 종목 · 드라이버, 목적은 따로', () => {
    expect(sessionName(row())).toEqual({ main: '05-08 · R4 · 11:40 · 연습 · B', purpose: '연습 2' })
    expect(sessionName(row(), { withCar: true }).main).toBe('DEMO-1 · 05-08 · R4 · 11:40 · 연습 · B')
  })
  it('일정표가 있으면 R번호 · 목적은 일정표에서 (그날 순번 · 세션 이름보다 먼저)', () => {
    expect(sessionName(row({ run_no: '6', purpose: '토크 맵 100 %' }))).toEqual({ main: '05-08 · R6 · 11:40 · 연습 · B', purpose: '토크 맵 100 %' })
  })
  it('순번 · 드라이버를 모르면 그 칸을 뺀다', () => {
    expect(sessionName(row({ day_order: null, driver_label: '' })).main).toBe('05-08 · 11:40 · 연습')
  })
  it('시작 시각이 없으면 엔진의 제목', () => {
    expect(sessionName(row({ start: null }))).toEqual({ main: 'DEMO-1 · 05-08 · 연습 2', purpose: '' })
  })
  it('시각은 그 자리 현지 시각 그대로 (브라우저 시간대로 바꾸지 않는다)', () => {
    expect(formatStart('2026-08-30T23:59:59+09:00')).toBe('2026-08-30 23:59')
    expect(formatStart(null)).toBe('—')
  })
})

describe('숫자 표시', () => {
  it('주행 시간 · 랩타임', () => {
    expect(formatDuration(829.51)).toBe('13:50')
    expect(formatDuration(3725)).toBe('1:02:05')
    expect(formatLapTime(65.36)).toBe('1:05.36')
    expect(formatLapTime(9.5)).toBe('9.50')
    expect(formatLapTime(null)).toBe('—')
  })
  it('랩 칸 — 코스 없음 · 랩 없는 종목은 —', () => {
    expect(lapsCell(row()).text).toBe('8')
    expect(lapsCell(row({ laps: 0 }))).toMatchObject({ text: '—', muted: true })
    expect(lapsCell(row({ laps: null, has_track: false })).tip).toContain('코스')
  })
})

describe('블록 칸', () => {
  it('보이는 세션에 있는 블록만, 틀 순서로, 숨긴 수를 센다', () => {
    const rows = [row({}, { video: 'have', gps: 'warn', pedal_brake_steer_wheel: 'none' }), row({}, { bms: 'seek', drive: 'warn' })]
    const c = blockColumns(rows)
    expect(c.columns.map((x) => x.key)).toEqual(['gps', 'bms', 'video'])
    expect(c.computed.map((x) => x.key)).toEqual(['drive'])
    expect(c.hidden.map((x) => x.key)).not.toContain('gps')
    expect(c.hidden).toHaveLength(14 - 4)
  })
  it('모르는 블록은 엔진의 이름으로 끝에 붙는다', () => {
    const c = blockColumns([row({}, { gps: 'have', aero: 'have' })])
    expect(c.columns.map((x) => [x.key, x.label])).toEqual([['gps', 'GPS'], ['aero', 'aero 제목']])
  })
  it('칸 표시 ✓ ▲ ✕ 빈칸', () => {
    const r = row({}, { gps: 'have', imu: 'warn', bms: 'seek', motor: 'none' })
    expect(['gps', 'imu', 'bms', 'motor', 'tire'].map((k) => blockCell(r, k).mark)).toEqual(['✓', '▲', '✕', '', ''])
  })
})

describe('거르기', () => {
  const rows = [
    row({ sid: 'a', car: 'DEMO-1', start: '2027-05-08T10:00:00+09:00', distance_km: 3 }),
    row({ sid: 'b', car: 'DEMO-2', start: '2027-05-09T10:00:00+09:00', distance_km: null }, { gps: 'warn' }),
    row({ sid: 'c', car: 'DEMO-2', start: '2027-05-07T10:00:00+09:00', distance_km: 9, discipline: '가속' }, { gps: 'have', bms: 'seek', drive: 'warn' }),
  ]
  const f = readFilters(new URLSearchParams())
  const sids = (xs: SessionRow[]) => xs.map((x) => x.sid)

  it('기본은 전부, 시작 최신순', () => {
    expect(sids(applyFilters(rows, f))).toEqual(['b', 'a', 'c'])
  })
  it('차량 · 종목 · 주의 · 미보유 · 찾기', () => {
    expect(sids(applyFilters(rows, { ...f, car: ['DEMO-2'] }))).toEqual(['b', 'c'])
    expect(sids(applyFilters(rows, { ...f, disc: ['가속'] }))).toEqual(['c'])
    expect(sids(applyFilters(rows, { ...f, warn: true }))).toEqual(['b']) // 계산 블록(drive)의 ▲ 는 주의로 치지 않는다
    expect(sids(applyFilters(rows, { ...f, seek: true }))).toEqual(['c'])
    expect(sids(applyFilters(rows, { ...f, q: 'demo-2 가속' }))).toEqual(['c'])
  })
  it('거리순은 모르는 값을 끝에', () => {
    expect(sids(applyFilters(rows, { ...f, sort: 'distance' }))).toEqual(['c', 'a', 'b'])
  })
  it('주소에 쓰고 다시 읽으면 같다', () => {
    const g = { ...f, q: '내구', car: ['DEMO-1'], disc: [], warn: true, sort: 'duration' as const }
    expect(readFilters(writeFilters(g))).toEqual(g)
    expect(writeFilters(f).toString()).toBe('')
  })
  it('체크를 다 켜면 다시 "전부"', () => {
    const all = ['DEMO-1', 'DEMO-2']
    expect(toggle(null, all, 'DEMO-1')).toEqual(['DEMO-2'])
    expect(toggle(['DEMO-2'], all, 'DEMO-1')).toBeNull()
  })
  it('거르기 항목과 세션 수', () => {
    expect(facet(rows, 'car')).toEqual([{ value: 'DEMO-1', n: 1 }, { value: 'DEMO-2', n: 2 }])
  })
})
