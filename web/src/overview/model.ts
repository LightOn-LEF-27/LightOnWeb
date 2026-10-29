// 세션 개요(보드 2)의 표시 규칙 — 엔진 값을 글자 · 색으로만 바꾼다 (계산하지 않는다)

import { formatDuration, formatLapTime, formatWithUnit } from '../sessions/format'
import type { DataStatus, LapColumn, LapRow, LapTable, QualityItem, SectorKey, SessionPage } from '../types'

const SECTORS: SectorKey[] = ['s1_s', 's2_s', 's3_s']
const grouped = new Map<number, Intl.NumberFormat>()

// 자릿수는 엔진이 준 digits 그대로, 천 단위 쉼표
export function formatValue(v: unknown, digits: number): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  let f = grouped.get(digits)
  if (!f) grouped.set(digits, (f = new Intl.NumberFormat('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })))
  return f.format(v)
}

export function formatBytes(n: number | undefined): string {
  if (n === undefined || !Number.isFinite(n)) return ''
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (n >= 1000 && i < units.length - 1) {
    n /= 1000
    i++
  }
  return i === 0 ? `${n} B` : `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`
}

// 랩 표 칸: 랩타임은 분:초, 구간은 초, 나머지는 엔진 자릿수. 보라 = 세션 최속 (랩타임 · 구간)
export function lapCell(row: LapRow, col: LapColumn): { text: string; best: boolean } {
  const v = row[col.key]
  if (col.key === 'lap_time_s') return { text: formatLapTime(typeof v === 'number' ? v : null), best: row.best }
  const sector = SECTORS.indexOf(col.key as SectorKey)
  if (sector >= 0) return { text: formatValue(v, col.digits), best: row.best_sectors.includes(sector + 1) }
  return { text: formatValue(v, col.digits), best: false }
}

// 랩 표 아래 줄 — 평균(완주 랩) · 이론 최속. 엔진이 준 값만 (없는 칸은 비운다)
export function lapFooter(t: LapTable, col: LapColumn): { mean: string; theoretical: string } {
  const s = t.sectors
  if (!s) return { mean: '', theoretical: '' }
  if (col.key === 'lap_time_s') return { mean: '', theoretical: formatLapTime(s.theoretical_best_s) }
  if (SECTORS.includes(col.key as SectorKey)) return { mean: formatValue(s.mean[col.key as SectorKey], col.digits), theoretical: '' }
  return { mean: '', theoretical: '' }
}

export function lapEmptyText(page: SessionPage): string | null {
  const s = page.summary
  if (!s.has_track) return '코스(위치) 기록이 없는 세션이라 랩이 없습니다.'
  if (!s.laps) return '랩이 없는 세션입니다 (가속 · 스키드패드 등).'
  return null
}

// 데이터 현황: 보유 · 주의 · 미보유는 한 줄씩, "기록 안 함" 은 맨 아래 한 줄로 접는다
export function statusGroups(rows: DataStatus[]): { shown: DataStatus[]; none: DataStatus[] } {
  return { shown: rows.filter((d) => d.status !== 'none'), none: rows.filter((d) => d.status === 'none') }
}

// 맨 위 주의 띠 = 품질 경고 중 warn · error (info 는 블록을 펼쳤을 때만)
export function bandItems(quality: QualityItem[], statuses: DataStatus[]): { title: string; message: string; level: string }[] {
  const title = new Map(statuses.map((d) => [d.block, d.title]))
  return quality
    .filter((q) => q.level !== 'info')
    .map((q) => ({ title: title.get(q.block) ?? q.block, message: q.message, level: q.level }))
}

export interface SummaryRow {
  k: string
  v: string
  best?: boolean
}

// 요약 칸 — 엔진이 값을 준 것만 (에너지 · 전류는 배터리 블록이 있는 세션에만)
export function summaryRows(page: SessionPage, startText: string): SummaryRow[] {
  const s = page.summary
  const stats = s.stats ?? {}
  const rows: (SummaryRow | null)[] = [
    { k: '차량', v: s.car },
    { k: '시작', v: startText },
    { k: '이벤트', v: [s.event_title, s.event_kind].filter(Boolean).join(' · ') },
    { k: '종목', v: s.discipline },
    { k: '장소', v: s.place || '—' },
    { k: '드라이버', v: s.driver_label || '—' },
    { k: '주행 시간', v: formatDuration(s.duration_s) },
    s.has_track ? { k: '거리', v: formatWithUnit(s.distance_km, 2, 'km') } : null,
    s.has_track ? { k: '완주 랩', v: s.laps ? `${s.laps}랩` : '—' } : null,
    s.best_lap_s !== null ? { k: '최속 랩', v: `${formatLapTime(s.best_lap_s)}${page.best_lap ? ` (${page.best_lap}랩)` : ''}`, best: true } : null,
    stats.vmax_kmh != null ? { k: '최고 속도', v: formatWithUnit(stats.vmax_kmh, 1, 'km/h') } : null,
    stats.net_wh != null ? { k: '순 에너지', v: `${formatValue(stats.net_wh, 0)} Wh` } : null,
    stats.wh_per_km != null ? { k: '거리당 에너지', v: `${formatValue(stats.wh_per_km, 0)} Wh/km` } : null,
    stats.i_max_a != null ? { k: '전류 최대', v: `${formatValue(stats.i_max_a, 0)} A` } : null,
  ]
  return rows.filter((r): r is SummaryRow => r !== null)
}

// 파일 ID 에는 "/" 가 들어 있다 (<sid>/files/laps.csv) — 조각마다 인코딩
export function fileUrl(fileId: string): string {
  return `/api/files/${fileId.split('/').map(encodeURIComponent).join('/')}`
}
