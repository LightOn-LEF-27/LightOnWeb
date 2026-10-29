import { describe, expect, it } from 'vitest'

import type { LapColumn, LapRow, LapTable, SessionPage, SessionRow } from '../types'
import { bandItems, fileUrl, formatBytes, formatValue, lapCell, lapEmptyText, lapFooter, statusGroups, summaryRows } from './model'

const col = (key: string, digits = 2): LapColumn => ({ key, label: key, unit: 's', digits, block: 'gps' })
const lapRow = (over: Partial<LapRow> = {}): LapRow => ({
  lap: 3, t_start_s: 100, t_end_s: 161.84, best: false, best_sectors: [], lap_time_s: 61.84, s1_s: 19.2, s2_s: 23.5, s3_s: 19.14, net_wh: 1234, ...over,
})

function page(over: Partial<SessionRow> = {}, best_lap: number | null = 5): SessionPage {
  const summary = {
    sid: 'DEMO-1_2027-05-08_S1', title: 't', name: '기본 세팅', car: 'DEMO-1', event_id: 'e', event_kind: '테스트', event_title: '데모 시험',
    discipline: '연습', place: '가상 서킷', driver_label: '드라이버 A', start: '2027-05-08T09:58:12.400+09:00', duration_s: 612.4,
    distance_km: 6.3, laps: 7, best_lap_s: 61.84, stats: { vmax_kmh: 88.4, net_wh: 1480 }, frame_id: 'f', split_by: 'gps', has_track: true,
    blocks: { have: [], warn: [], seek: [], none: [] }, data_status: [], day_order: 1, run_no: null, purpose: null, ...over,
  } as SessionRow
  return { summary, notes: [], blocks: [], missing: [], best_lap, made: { built_at: '', commit: '', command: '' } }
}

describe('랩 표 칸', () => {
  it('랩타임은 분:초, 구간은 초, 나머지는 엔진 자릿수 + 천 단위', () => {
    expect(lapCell(lapRow(), col('lap_time_s')).text).toBe('1:01.84')
    expect(lapCell(lapRow(), col('s1_s')).text).toBe('19.20')
    expect(lapCell(lapRow(), col('net_wh', 0)).text).toBe('1,234')
    expect(lapCell(lapRow({ net_wh: null }), col('net_wh', 0)).text).toBe('—')
  })
  it('보라 = 세션 최속 랩의 랩타임 · 최속 구간만', () => {
    expect(lapCell(lapRow({ best: true }), col('lap_time_s')).best).toBe(true)
    expect(lapCell(lapRow({ best: true }), col('s1_s')).best).toBe(false)
    expect(lapCell(lapRow({ best_sectors: [2] }), col('s2_s')).best).toBe(true)
    expect(lapCell(lapRow({ best_sectors: [2] }), col('net_wh', 0)).best).toBe(false)
  })
  it('아래 줄 — 구간 평균 · 이론 최속 (엔진 값 그대로)', () => {
    const t: LapTable = {
      columns: [], rows: [], notes: [], best_lap: 1,
      sectors: { best: { s1_s: { value: 19, lap: 1 }, s2_s: { value: 23, lap: 2 }, s3_s: { value: 19, lap: 1 } },
                 mean: { s1_s: 19.5, s2_s: 23.6, s3_s: 19.4 }, n_laps: 7, theoretical_best_s: 61.02 },
    }
    expect(lapFooter(t, col('s2_s'))).toEqual({ mean: '23.60', theoretical: '' })
    expect(lapFooter(t, col('lap_time_s'))).toEqual({ mean: '', theoretical: '1:01.02' })
    expect(lapFooter(t, col('net_wh', 0))).toEqual({ mean: '', theoretical: '' })
    expect(lapFooter({ ...t, sectors: null }, col('s1_s'))).toEqual({ mean: '', theoretical: '' })
  })
  it('랩이 없을 때의 안내', () => {
    expect(lapEmptyText(page())).toBeNull()
    expect(lapEmptyText(page({ laps: 0 }))).toContain('랩이 없는 세션')
    expect(lapEmptyText(page({ has_track: false, laps: null }))).toContain('코스')
  })
})

describe('개요 표시', () => {
  it('요약은 엔진이 준 값만 — 배터리 값이 없으면 그 줄이 없다', () => {
    const keys = summaryRows(page(), 'x').map((r) => r.k)
    expect(keys).toContain('순 에너지')
    expect(keys).not.toContain('전류 최대')
    expect(summaryRows(page(), 'x').find((r) => r.k === '최속 랩')).toEqual({ k: '최속 랩', v: '1:01.84 (5랩)', best: true })
    const timed = summaryRows(page({ has_track: false, laps: null, distance_km: null, best_lap_s: null }, null), 'x').map((r) => r.k)
    expect(timed).not.toContain('거리')
    expect(timed).not.toContain('최속 랩')
  })
  it('주의 띠는 warn · error 만, 블록 이름을 붙여서', () => {
    const items = bandItems(
      [
        { level: 'warn', code: 'a', block: 'bms', message: 'm1', t_ranges: null },
        { level: 'info', code: 'b', block: 'gps', message: 'm2', t_ranges: null },
        { level: 'error', code: 'c', block: 'aero', message: 'm3', t_ranges: null },
      ],
      [{ block: 'bms', title: '배터리 상태', status: 'warn', detail: '' }],
    )
    expect(items.map((i) => [i.title, i.message])).toEqual([['배터리 상태', 'm1'], ['aero', 'm3']])
  })
  it('데이터 현황 — 기록 안 함은 따로 접는다', () => {
    const g = statusGroups([
      { block: 'gps', title: 'g', status: 'have', detail: '' },
      { block: 'video', title: 'v', status: 'seek', detail: '' },
      { block: 'tire', title: 't', status: 'none', detail: '' },
    ])
    expect(g.shown.map((d) => d.block)).toEqual(['gps', 'video'])
    expect(g.none.map((d) => d.block)).toEqual(['tire'])
  })
  it('파일 크기 · 숫자 · 파일 주소', () => {
    expect(formatBytes(950)).toBe('950 B')
    expect(formatBytes(1_416_304)).toBe('1.4 MB')
    expect(formatBytes(4_000_517_007)).toBe('4.0 GB')
    expect(formatBytes(undefined)).toBe('')
    expect(formatValue(2509, 0)).toBe('2,509')
    expect(fileUrl('DEMO-1_2027-05-08_S1/files/laps.csv')).toBe('/api/files/DEMO-1_2027-05-08_S1/files/laps.csv')
    expect(fileUrl('A_B/raw/x y.log')).toBe('/api/files/A_B/raw/x%20y.log')
  })
})
