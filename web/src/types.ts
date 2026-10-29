// 엔진 계약(분석엔진_인터페이스.md, v0.9)의 JSON 모양 — 화면이 쓰는 것만. 서버는 엔진 JSON 을 그대로 넘긴다.

export type BlockStatus = 'have' | 'warn' | 'seek' | 'none'

export interface DataStatus {
  block: string
  title: string
  status: BlockStatus
  detail: string
}

// §6-1 list_sessions 의 한 줄 (= get_session 의 summary)
export interface SessionRow {
  sid: string
  title: string
  name: string
  car: string
  event_id: string
  event_kind: string
  event_title: string
  discipline: string
  place: string
  driver_label: string
  start: string | null // ISO 8601 + 시간대. 모르면 null
  duration_s: number
  distance_km: number | null
  laps: number | null // 0 = 랩이 없는 세션, null = 코스 기록 없음
  best_lap_s: number | null
  stats: Record<string, number | null>
  frame_id: string | null
  split_by: 'gps' | 'time'
  has_track: boolean
  blocks: Record<BlockStatus, string[]>
  data_status: DataStatus[]
  day_order: number | null // v0.9 — 그 차 · 그날 몇 번째 (화면용, 바뀔 수 있다)
}

export interface FileRef {
  file_id: string | null
  label: string
  name: string
  size_bytes?: number
  note?: string
  where?: string // file_id 가 null 이면 "있는 곳" 만 적힌 원본 (내려받을 수 없다)
  content_type?: string
  rows?: number // 결과물(세션 구간 CSV)의 행 수
}

// 변환된 표가 있는 곳 — 내려받기 대상은 아니다 (§6-2 v0.3)
export interface Output {
  label: string
  name: string
  note: string
  where: string
}

// §6-2 get_session 의 블록 하나
export interface Block {
  block: string
  title: string
  status: BlockStatus
  device: string
  facts: string[]
  checks: string[]
  cautions: string[]
  raw: FileRef[]
  outputs?: Output[]
  exports: FileRef[]
  code: { key: string; title: string; role: string }[]
  channels: string[]
}

export interface Missing {
  block?: string
  data: string
  status: BlockStatus
  reason: string
  ask: string
}

export interface SessionPage {
  summary: SessionRow
  notes: string[]
  blocks: Block[]
  missing: Missing[]
  best_lap: number | null
  made: { built_at: string; commit: string; command: string }
}

// §6-5 get_laps — 열은 엔진이 세션마다 정한다 (온도 · 에너지 열은 그 블록이 있을 때만)
export interface LapColumn {
  key: string
  label: string
  unit: string
  digits: number
  block: string
  chart?: boolean
  cautions?: string[]
}

export type SectorKey = 's1_s' | 's2_s' | 's3_s'

export interface LapRow {
  lap: number
  t_start_s: number
  t_end_s: number
  best: boolean
  best_sectors: number[] // 이 랩이 세션 최속인 구간 번호 (1 ~ 3)
  [key: string]: number | boolean | number[] | null
}

export interface LapTable {
  columns: LapColumn[]
  rows: LapRow[]
  sectors: {
    best: Record<SectorKey, { value: number; lap: number }>
    mean: Record<SectorKey, number>
    n_laps: number
    theoretical_best_s: number
  } | null
  notes: string[]
  best_lap: number | null
}

// §6-7 get_quality — 위치(구간)가 있는 품질 경고
export interface QualityItem {
  level: 'info' | 'warn' | 'error'
  code: string
  block: string
  message: string
  t_ranges: [number, number][] | null
}

// §6-10 resolve_session · §8 session_retired 의 detail
export interface Resolved {
  sid: string
  why: 'replaced' | 'split' | 'removed' | null
  retired_at: string | null
  replaced_by: string[]
  current: string[]
}

export type Role = 'admin' | 'lead' | 'member'

export interface Me {
  email: string
  name: string
  role: Role
  role_label: string
  permissions: string[]
  auth: 'dev' | 'google'
}

export interface Info {
  site: { version: string; auth: 'dev' | 'google' }
  connector: 'fake' | 'real'
  engine: {
    contract: string
    engine: { name: string; version: string }
    store: { format: number; sessions: number; retired_sids: number }
  }
}
