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
  default_views?: { time: string[]; distance: string[]; map: string | null; gg: boolean } // 처음 열 때 그릴 채널 (엔진 제안)
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

// §6-3 list_channels — 채널 하나
export interface Channel {
  channel_id: string
  block: string
  label: string
  unit: string
  sign: string
  rate_hz: number
  samples?: number
  source: { recording_id: string | null; device: string }
  derived: boolean
  derivation: string
  t_range: [number, number]
  cautions: string[]
  digits: number
  levels?: { value: number; label: string }[] // 정해진 값만 갖는 채널 (주행 상태 추정)
}

// §6-4 get_series — 모든 계열이 같은 x 를 쓴다. 빈 곳은 null (선을 끊는다)
export interface SeriesResponse {
  sid: string
  x: { kind: 'time' | 'distance' | 'ref_distance'; unit: string; values: number[]; lap: number | null; frame_id: string | null }
  series: { channel_id: string; unit: string; values: (number | null)[]; y_min: (number | null)[]; y_max: (number | null)[]; samples: number }[]
  laps: { lap: number; x_start: number; x_end: number }[]
  reduced: { from_points: number; to_points: number; method: string; smooth_s: number | null }
}

// §6-6 get_track — 랩의 주행선 (첫 점 · 끝 점 = 계측선). lap="all" 이면 세션 전체 궤적 (ref_m 없음)
export interface TrackResponse {
  sid: string
  lap: number | null
  t_s: number[]
  x_m: number[]
  y_m: number[]
  dist_m: number[]
  ref_m: number[] | null
  gate: { x_m: number; y_m: number; heading_deg: number; half_width_m: number }
  frame_id: string | null
  direction: string
  frame: string
  color?: { channel_id: string; unit: string; values: (number | null)[]; smooth_s: number | null; levels?: { value: number; label: string }[] }
}

// §6-8 get_sectors — 구간 · 미니 구간 (경계는 기준 거리 위, frame 마다 고정)
export interface SectorsResponse {
  sid: string
  frame_id: string | null
  sectors: { sector: number; ref_m0: number; ref_m1: number; x_m: number; y_m: number; heading_deg: number }[]
  mini: { mini_m: number; edges_ref_m: number[]; count: number }
  laps: { lap: number; sector_s: number[]; mini_s: number[] }[]
  best: { sector_s: number[]; sector_lap: number[]; mini_s: number[]; mini_lap: number[] } | null
  mean: { sector_s: number[]; mini_s: number[]; n_laps: number } | null
}

// §6-9 get_delta — + 면 lap 이 ref 보다 그만큼 늦다
export interface DeltaResponse {
  lap: { sid: string; lap: number }
  ref: { sid: string; lap: number }
  frame_id: string
  x: { kind: 'ref_distance'; unit: string; values: number[] }
  delta_s: number[]
  mini: { mini_m: number; edges_ref_m: number[]; delta_s: number[] }
  on_track: { values: number[] }
  total_s: number
  lap_time_s: { lap: number; ref: number }
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
