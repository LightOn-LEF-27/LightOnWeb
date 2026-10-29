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
  where?: string
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
