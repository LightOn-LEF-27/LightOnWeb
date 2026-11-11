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
  run_no: string | null // v0.12 — 일정표의 R번호 (일정표와 짝이 맞은 세션만)
  purpose: string | null // v0.12 — 일정표의 목적
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

// §12-4 실시간 state() — 0.5 s 마다 (GET /api/live/state?since=). 화면은 그리기만 하고 다시 계산하지 않는다
export type LiveStatus = 'danger' | 'warn' | 'ok' | 'stale' | 'none'
export type LiveLevel = 'danger' | 'warn' | 'info'

// 게이지 범위 · 문턱 (계약 §12-4 tiles[].scale — 엔진이 주면 그것을, 없으면 화면의 정해진 값만)
export interface LiveScale {
  min: number
  max: number
  warn: number | null
  danger: number | null
  dir: '>=' | '<=' // 문턱을 넘는 쪽 — >= 높을수록 위험 (온도 · 전력), <= 낮을수록 위험 (셀 전압 · SOC)
}

export interface LiveTile {
  id: string
  label: string
  unit: string
  sign: string
  digits: number
  value: number | null
  age_s: number | null
  status: LiveStatus
  alert: string | null
  scale?: LiveScale | null
}

export interface LiveState {
  contract: string
  now: number // 서버 시계 (epoch s)
  car: string
  source: { replay: boolean; bad_rows?: number; unknown_topics?: number; bad_records?: number } // bad_records (v0.16) = 머리 · 체크섬이 안 맞아 버린 레코드
  receiver: { status: 'connected' | 'disconnected' | 'unknown'; since: number | null }
  device: {
    status: 'on' | 'off' | 'disconnected' | 'unknown'; boot_at: number | null; firmware: string | null; boots: number
    errors?: string[] | null; fatal?: string[] | null // v0.16 — Monolith 부품 상태 비트 이름, /d 를 받기 전엔 null
  }
  link: { status: 'ok' | 'late' | 'lost' | 'none'; age_s: number | null; period_s: number; rate_10s: number | null; outages: number; longest_s: number | null; since_s: number | null }
  sources: LiveSource[]
  tiles: LiveTile[]
  inverter: { status: LiveStatus; enabled: boolean | null; ready: boolean | null; errors: string[]; warnings: string[]; age_s: number | null }
  pedal?: { status: LiveStatus; faults: string[]; age_s: number | null } // v0.16 — APPS 보드 고장
  position: { frame_id: string | null; x_m: number; y_m: number; age_s: number } | null
  track_rev?: number | null // v0.16 (11-11) — 달린 궤적에 더한 점 수, 늘기만 한다. 바뀌면 /api/live/track 을 다시 받는다. 경기장이 없으면 null
  laps: {
    count: number
    current_s: number | null
    last_s: number | null
    best_s: number | null
    basis: string
    rows?: LiveLap[] // v0.16 — 끝난 랩, 앞 랩부터
    current?: LiveLap | null // 지금 도는 랩 (t_end = 지금)
    energy_basis?: string
  } | null
  alerts: { id: string; level: LiveLevel; text: string; value: number | null; since: number | null }[]
  events: LiveEvent[]
  chart: { t: number[]; series: Record<string, (number | null)[]> }
}

// 원천 — bms · inverter · pedal 은 10 s 수신률(새 CAN 프레임), gps 는 위치를 잡은 비율 (v0.16)
export interface LiveSource {
  id: string
  label: string
  status: 'ok' | 'stale' | 'none'
  age_s: number | null
  rate_10s?: number | null
  rate_status?: 'ok' | 'warn' | 'danger' | null
  fix_10s?: number | null
}

// 랩 한 줄 (§12-4 laps.rows — 세션 랩 표와 같은 칸 이름). cover < 1 이면 전력 값에 빈 곳이 있어 net_wh 가 작다
export interface LiveLap {
  lap: number
  t_start: number
  t_end: number
  lap_time_s: number
  net_wh: number | null
  cover: number | null
  soc_start_pct: number | null
  soc_end_pct: number | null
  soc_delta_pct: number | null
  cell_v_min: number | null
  cell_t_max_c: number | null
  speed_max_kmh?: number | null
  lap_delta_s?: number | null // 그 랩 − 바로 앞 랩, 빠르면 −. 첫 랩 · 도는 랩은 null (v0.16)
}

// 사건 — 최근 것이 앞. source_lost · source_back 은 source 칸에 원천 id, gap_s = 끊겨 있던 초 (있으면)
export interface LiveEvent {
  t: number
  kind: string
  text: string
  source?: string
  gap_s?: number | null
}

// §12-5 실시간 track() — 경기장 기준선 · 계측선 (get_track 과 같은 좌표계)
export interface LiveTrack {
  frame_id: string | null
  line: { x_m: number[]; y_m: number[] }
  gate: { x: number; y: number; heading_deg: number; half_width_m: number | null }
  // v0.16 (11-11) — 그날 달린 궤적 (주행 중만 3 m 마다, null = 선 끊김) · 기준선 근처를 달리는지 (같은 코스 판정 아님)
  path?: { x_m: (number | null)[]; y_m: (number | null)[]; rev: number; step_m: number; min_kmh: number }
  course?: { share: number | null; on_line: boolean | null; tol_m: number; need: number; window_m: number | null }
}
