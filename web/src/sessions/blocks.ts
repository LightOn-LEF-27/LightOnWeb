// 세션 목록의 블록 칸 — 틀은 LEF-27 의 전체 데이터 기준, 보이는 세션에 없는 블록은 숨기고 몇 개 숨겼는지 알린다 (사이트_설계.md §7).
// 어떤 블록이 있는지는 엔진이 준 목록(data_status)대로 — 아래 표는 칸의 순서 · 짧은 이름만 정한다.

import type { BlockStatus, SessionRow } from '../types'

export interface Column {
  key: string
  label: string // 칸 머리의 짧은 이름
  computed: boolean // 엔진이 다른 블록에서 계산한 블록 (주행 상태 추정 등)
}

// LEF-27 의 블록 틀. steer · wheel · tire 는 엔진에 아직 없는 이름이라 엔진이 다른 이름을 쓰면 아래 "모르는 블록" 으로 붙는다
export const FRAME: Column[] = [
  { key: 'gps', label: 'GPS' },
  { key: 'imu', label: 'IMU' },
  { key: 'pedal', label: '페달' },
  { key: 'steer', label: '조향' },
  { key: 'wheel', label: '휠' },
  { key: 'battery', label: '팩' },
  { key: 'bms', label: 'BMS' },
  { key: 'thermistor', label: '셀 온도' },
  { key: 'motor', label: '모터' },
  { key: 'cooling', label: '냉각' },
  { key: 'suspension', label: '서스' },
  { key: 'tire', label: '타이어' },
  { key: 'video', label: '영상' },
].map((c) => ({ ...c, computed: false }))

export const COMPUTED: Column[] = [{ key: 'drive', label: '주행 상태', computed: true }]

export const MARK: Record<BlockStatus, { mark: string; text: string }> = {
  have: { mark: '✓', text: '보유' },
  warn: { mark: '▲', text: '보유 (주의)' },
  seek: { mark: '✕', text: '미보유 — 기록했는데 우리한테 없음' },
  none: { mark: '', text: '기록 안 함' },
}

const SHOWN: BlockStatus[] = ['have', 'warn', 'seek'] // "기록 안 함" 만 있는 블록은 칸을 만들지 않는다

export interface BlockColumns {
  columns: Column[] // 장비 블록 (틀 순서, 그다음 모르는 블록)
  computed: Column[] // 계산 블록
  hidden: Column[] // 틀에 있지만 보이는 세션 어디에도 없어 숨긴 블록
}

export function blockColumns(rows: SessionRow[]): BlockColumns {
  const present = new Map<string, string>() // 블록 → 화면 이름 (모르는 블록용)
  for (const r of rows) for (const d of r.data_status) if (SHOWN.includes(d.status)) present.set(d.block, d.title)
  const known = new Set([...FRAME, ...COMPUTED].map((c) => c.key))
  const unknown = [...present.keys()]
    .filter((k) => !known.has(k))
    .sort()
    .map((k) => ({ key: k, label: present.get(k) ?? k, computed: false }))
  return {
    columns: [...FRAME.filter((c) => present.has(c.key)), ...unknown],
    computed: COMPUTED.filter((c) => present.has(c.key)),
    hidden: [...FRAME, ...COMPUTED].filter((c) => !present.has(c.key)),
  }
}

export function blockCell(row: SessionRow, key: string): { status: BlockStatus; mark: string; tip: string } {
  const d = row.data_status.find((x) => x.block === key)
  const status = d?.status ?? 'none'
  const tip = d ? `${d.title} — ${MARK[status].text}${d.detail ? `\n${d.detail}` : ''}` : MARK.none.text
  return { status, mark: MARK[status].mark, tip }
}
