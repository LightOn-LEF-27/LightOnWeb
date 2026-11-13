// 실시간 화면 — 받은 값을 그리기 위한 손질만 한다. 숫자 · 상태 · 문구는 엔진이 정한다 (계약 §12-4).
// 화면이 스스로 정하는 것은 둘뿐이다: 그래프 창(최근 5분)과, 서버 응답이 끊겼을 때 화면 전체를 낡음으로 돌리는 것.

import { formatValue } from '../overview/model'
import type { LiveEvent, LiveLap, LiveScale, LiveSource, LiveState, LiveStatus, LiveTile, LiveTrack } from '../types'

export const POLL_MS = 500
export const WINDOW_S = 300 // 그래프에 보이는 최근 5분
export const KEEP_S = 600 // 화면이 들고 있는 그래프 값 — 그래프 뷰의 10분 (엔진도 10분까지 준다, CHART_KEEP_S)
export const TRAIL_S = 30 // 트랙맵의 최근 궤적
export const FROZEN_S = 3 // 서버 응답이 이만큼 없으면 화면의 값이 전부 그 전 값 — 색을 빼고 띠를 띄운다

// ---------------------------------------------------------------- 그래프 값 (받은 줄을 이어 붙인다)

export interface ChartBuf {
  t: number[]
  series: Record<string, (number | null)[]>
}

export const emptyBuf = (): ChartBuf => ({ t: [], series: {} })

export const sinceOf = (buf: ChartBuf): number | null => (buf.t.length ? buf.t[buf.t.length - 1] : null)

// since 뒤의 줄만 오므로 겹치지 않는다. 그래도 가진 마지막 t 이하는 버리고, 창(now − keep) 밖은 앞에서 자른다.
// 가진 값이 서버 시계보다 앞서 있으면(서버를 옮겼거나 시계가 바뀜) 처음부터 다시 쌓는다
export function mergeChart(buf: ChartBuf, add: LiveState['chart'], now: number, keep = WINDOW_S): ChartBuf {
  const last = sinceOf(buf)
  if (last !== null && last > now + 5) return mergeChart(emptyBuf(), add, now, keep)
  const keys = [...new Set([...Object.keys(buf.series), ...Object.keys(add.series)])]
  const t = buf.t.slice()
  const series: ChartBuf['series'] = {}
  for (const k of keys) series[k] = buf.series[k] ? buf.series[k].slice() : buf.t.map(() => null)
  add.t.forEach((x, i) => {
    if (last !== null && x <= last) return
    t.push(x)
    for (const k of keys) series[k].push(add.series[k]?.[i] ?? null)
  })
  let cut = 0
  while (cut < t.length && t[cut] < now - keep) cut++
  if (!cut) return { t, series }
  for (const k of keys) series[k] = series[k].slice(cut)
  return { t: t.slice(cut), series }
}

// 트랙맵의 최근 궤적 — 그래프 값에 실려 오는 위치(gps.x_m · y_m)에서 최근 TRAIL_S 만
export function trail(buf: ChartBuf, now: number, span = TRAIL_S): [number, number][] {
  const xs = buf.series['gps.x_m'] ?? []
  const ys = buf.series['gps.y_m'] ?? []
  const out: [number, number][] = []
  buf.t.forEach((t, i) => {
    const x = xs[i], y = ys[i]
    if (t >= now - span && x != null && y != null) out.push([x, y])
  })
  return out
}

// ---------------------------------------------------------------- 트랙맵

// 트랙(기준선 · 달린 궤적)을 다시 받을 간격 — 궤적이 늘 때마다 받지 않게 (계약 §12-4 track_rev, 10 s 에 한 번까지)
export const TRACK_MIN_MS = 10000

// 트랙을 언제 다시 받나 — 기다릴 ms, 받을 필요 없으면 null.
// 처음이면 바로. 못 받았으면(오류) 간격마다 다시. 받은 뒤엔 track_rev 가 받은 궤적의 rev 와 다를 때만
export function trackWait(rev: number | null | undefined, got: LiveTrack | null, lastAt: number | null, now: number): number | null {
  if (lastAt === null) return 0
  const wait = Math.max(0, lastAt + TRACK_MIN_MS - now)
  if (!got) return wait
  if (rev == null || rev === got.path?.rev) return null
  return wait
}

// 달린 궤적 → 끊긴 곳(null)마다 나눈 선들. 점 하나짜리 조각은 뺀다
export function pathRuns(path: LiveTrack['path']): [number, number][][] {
  if (!path) return []
  const runs: [number, number][][] = []
  let cur: [number, number][] = []
  path.x_m.forEach((x, i) => {
    const y = path.y_m[i]
    if (x == null || y == null) {
      if (cur.length > 1) runs.push(cur)
      cur = []
    } else cur.push([x, y])
  })
  if (cur.length > 1) runs.push(cur)
  return runs
}

// 지도 범위 — 기준선 + 달린 궤적 + 최근 꼬리 + 지금 위치를 다 담는다 (콘 코스가 기준선 밖이어도 화면 안에)
export function mapBounds(pts: Iterable<[number, number]>): { minX: number; maxX: number; minY: number; maxY: number } | null {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
  for (const [x, y] of pts) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue
    if (x < minX) minX = x
    if (x > maxX) maxX = x
    if (y < minY) minY = y
    if (y > maxY) maxY = y
  }
  return minX === Infinity ? null : { minX, maxX, minY, maxY }
}

// ---------------------------------------------------------------- 표시

// 서버 응답이 끊기면 모든 값이 그 전 값이다 — 색이 있는 상태도 낡음으로 (낡은 값에는 색을 칠하지 않는다, 설계안 §4-2)
export function shownStatus(status: LiveStatus, frozen: boolean): LiveStatus {
  if (status === 'none') return 'none'
  return frozen ? 'stale' : status
}

export function tileValue(tile: LiveTile): string {
  return tile.value === null ? '—' : formatValue(tile.value, tile.digits)
}

export function ageText(age: number | null): string {
  if (age === null) return ''
  if (age < 10) return `${age.toFixed(1)} s 전`
  if (age < 120) return `${Math.round(age)} s 전`
  return `${Math.round(age / 60)}분 전`
}

export const STATUS_WORD: Partial<Record<LiveStatus, string>> = { danger: '위험', warn: '주의' }

type Tone = 'ok' | 'warn' | 'danger' | 'stale' | 'none'

export function linkView(link: LiveState['link']): { text: string; tone: Tone } {
  if (link.status === 'none' || link.age_s === null) return { text: '수신 없음', tone: 'none' }
  if (link.status === 'ok') return { text: `수신 ${link.age_s.toFixed(1)} s`, tone: 'ok' }
  if (link.status === 'late') return { text: `수신 늦음 ${link.age_s.toFixed(1)} s`, tone: 'warn' }
  return { text: `수신 끊김 ${Math.round(link.since_s ?? link.age_s)} s`, tone: 'danger' }
}

export function rateText(link: LiveState['link']): string {
  return link.rate_10s === null ? '10 s —' : `10 s ${Math.round(link.rate_10s * 100)} %`
}

export function outageText(link: LiveState['link']): string {
  if (!link.outages) return '끊김 없음'
  return `끊김 ${link.outages}번${link.longest_s !== null ? ` · 최장 ${Math.round(link.longest_s)} s` : ''}`
}

// name = 차의 송신 장치 이름 (25Evo = Monolith). '장치' 만으로는 무엇인지 애매하다 (2026-11-03 사용자 지적)
export function deviceView(d: LiveState['device'], name = '장치'): { text: string; tone: Tone } {
  if (d.status === 'on') return { text: `${name} 켜짐`, tone: 'ok' }
  // 브로커가 OFFLINE 을 낸 뒤 메시지 없음 — 전원을 껐는지 휴대전화망이 끊겼는지 모른다 (단정하지 않는다)
  if (d.status === 'disconnected') return { text: `${name} 연결 끊김`, tone: 'none' }
  return { text: `${name} 확인 중`, tone: 'none' }
}

export function receiverView(r: LiveState['receiver']): { text: string; tone: Tone } {
  if (r.status === 'connected') return { text: '수신기', tone: 'ok' }
  if (r.status === 'disconnected') return { text: '수신기 끊김', tone: 'danger' }
  return { text: '수신기 확인 중', tone: 'none' }
}

export const onOff = (v: boolean | null): string => (v === null ? '—' : v ? '켜짐' : '꺼짐')

// 원천 상태 낱말 — 서버 응답이 끊기면 정상도 낡음으로
export function sourceWord(status: LiveSource['status'], frozen: boolean): { text: string; tone: Tone } {
  if (status === 'none') return { text: '수신 없음', tone: 'none' }
  if (frozen || status === 'stale') return { text: '낡음', tone: 'stale' }
  return { text: '정상', tone: 'ok' }
}

// 0 ~ 1 → "95 %" (없으면 —)
export const pctText = (r: number | null | undefined): string => (r == null ? '—' : `${Math.round(r * 100)} %`)

// 부호를 붙인 숫자 (SOC 변화처럼 + · − 가 뜻이 있는 값)
export function signedText(v: number | null | undefined, digits: number): string {
  if (v == null) return '—'
  const t = formatValue(Math.abs(v), digits)
  return v > 0 ? `+${t}` : v < 0 ? `−${t}` : t
}

export const numText = (v: number | null | undefined, digits: number): string => (v == null ? '—' : formatValue(v, digits))

// ---------------------------------------------------------------- 사건 접기

export interface EventRow extends LiveEvent {
  count?: number // 접은 줄 — 같은 원천의 no data 가 몇 번
  longest_s?: number | null
}

const SOURCE_KINDS = new Set(['source_lost', 'source_back'])

// 같은 원천의 no data / data back 이 되풀이되면 한 줄로 접는다 (2026-11-03 사용자 결정 — GPS 위치가 몇 초씩 자주 빠지면 목록을 다 차지한다).
// 원천 사건만 이어진 덩어리 안에서 원천마다 모은다: no data 가 두 번 이상이면 「<가장 최근 no data 문구> ×N · 최장 M s」 한 줄을
// 그 원천의 가장 최근 사건 자리에. 다른 사건(경고 · 수신)이 끼면 덩어리가 끊긴다 — 시간 순서를 흐리지 않게.
// 최장은 엔진이 준 gap_s 로만 (문구에서 읽지 않는다). 사건은 최근 것이 앞 (계약 §12-4)
export function foldEvents(events: LiveEvent[]): EventRow[] {
  const out: EventRow[] = []
  let i = 0
  while (i < events.length) {
    if (!SOURCE_KINDS.has(events[i].kind)) {
      out.push(events[i++])
      continue
    }
    let j = i
    while (j < events.length && SOURCE_KINDS.has(events[j].kind)) j++
    const bySource = new Map<string, LiveEvent[]>()
    for (const e of events.slice(i, j)) bySource.set(e.source ?? '', [...(bySource.get(e.source ?? '') ?? []), e])
    const rows: EventRow[] = []
    for (const group of bySource.values()) {
      const lost = group.filter((e) => e.kind === 'source_lost')
      if (lost.length < 2) {
        rows.push(...group)
        continue
      }
      const gaps = group.map((e) => e.gap_s).filter((g): g is number => typeof g === 'number')
      // kind 은 source_fold — 마지막이 data back 이어도 초록으로 칠하지 않게 (되풀이된 끊김 자체가 볼 거리)
      rows.push({ ...group[0], kind: 'source_fold', text: `${lost[0].text} ×${lost.length}`, count: lost.length, longest_s: gaps.length ? Math.max(...gaps) : null })
    }
    rows.sort((a, b) => b.t - a.t)
    out.push(...rows)
    i = j
  }
  return out
}

export const eventText = (e: EventRow): string =>
  e.count && e.longest_s != null ? `${e.text} · 최장 ${Math.round(e.longest_s)} s` : e.text

// 랩 시간은 GPS 주기(2 Hz)만큼 틀릴 수 있어 소수 한 자리까지만 (엔진도 0.1 s 로 준다)
export function lapText(s: number | null): string {
  if (s === null || !Number.isFinite(s)) return '—'
  const m = Math.floor(s / 60)
  return m ? `${m}:${(s - m * 60).toFixed(1).padStart(4, '0')}` : s.toFixed(1)
}

// 서버 시계(epoch s) → 한국 시각 HH:MM:SS (폰 · 노트북의 시간대 설정과 상관없이)
const KST = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
export const clock = (t: number): string => KST.format(new Date(t * 1000))

// ---------------------------------------------------------------- 그래프 칸

export interface PanelSeries {
  id: string
  label?: string // 이름 (없으면 엔진 타일 이름)
  right?: boolean // 오른쪽 세로축
  color: 0 | 1 | 2 // --trace-<n>
}

export interface Panel {
  key: string
  title: string
  series: PanelSeries[]
}

// 그래프 뷰 (§6-1, 2026-11-12) — 한 시간축에 칸을 쌓는다. 엔진이 그래프 값(chart.series)에 실어 주는 채널만 그려진다 (없으면 "엔진 값 준비 중").
// laps = 그 채널의 랩 값 칸 이름 (계약 §12-4 laps.rows — 세션 랩 표와 같은 이름). 칸의 채널이 모두 랩 값을 받으면 랩마다 꼭짓점, 아니면 시간축 선 (사용자 결정 11-12)
// limits = 한도 · 주의 선을 그 칸(tiles[].scale)에서 가져온다
export type GraphGroup = 'all' | 'drive' | 'battery' | 'powertrain'
export const GRAPH_GROUPS: [GraphGroup, string][] = [['all', '전체'], ['drive', '주행'], ['battery', '배터리'], ['powertrain', '인버터 · 모터']]
export const GRAPH_RANGES: [number, string][] = [[60, '1분'], [300, '5분'], [600, '10분']]

export interface GraphPanel extends Panel {
  groups: GraphGroup[]
  laps?: Record<string, string>
  limits?: string
}

export const GRAPH_PANELS: GraphPanel[] = [
  { key: 'speed', title: '속도 · 회전수', groups: ['drive', 'powertrain'], series: [{ id: 'gps.speed_kmh', color: 2 }, { id: 'motor.rpm', right: true, color: 1 }] },
  { key: 'pedal', title: '가속페달 · 토크 명령', groups: ['drive', 'powertrain'], series: [{ id: 'pedal.apps_pct', color: 0 }, { id: 'pedal.torque_cmd_pct', color: 2 }] },
  { key: 'power', title: '전력 · 전류', groups: ['battery'], series: [{ id: 'battery.power_kw', color: 0 }, { id: 'battery.current_a', right: true, color: 2 }], limits: 'battery.power_kw' },
  { key: 'cellv', title: '셀 최저 전압', groups: ['battery'], series: [{ id: 'bms.cell_min_v', color: 2 }], limits: 'bms.cell_min_v' },
  {
    key: 'cellt', title: '셀 온도', groups: ['battery'], series: [{ id: 'bms.temp_max_c', color: 0 }, { id: 'bms.temp_min_c', color: 2 }],
    laps: { 'bms.temp_max_c': 'cell_t_max_c', 'bms.temp_min_c': 'cell_t_min_c' }, limits: 'bms.temp_max_c',
  },
  {
    key: 'temp', title: '모터 · 인버터(IGBT) 온도', groups: ['powertrain'], series: [{ id: 'motor.t_motor_c', color: 0 }, { id: 'motor.t_igbt_c', color: 2 }],
    laps: { 'motor.t_motor_c': 'motor_t_max_c', 'motor.t_igbt_c': 'inverter_t_max_c' },
  },
]

// 수신 · DAQ 뷰의 원천별 수신률 그래프 — 다섯 칸 모두 0 ~ 100 %, 선 색은 고정 (상태 색은 % 숫자 · 표 막대에만, 사용자 결정).
// 그래프 값 이름은 텔레메트리에 부탁한 것 (게시판 #38fa) — 엔진이 다른 이름으로 주면 여기만 고친다
export const RATE_PANELS: { key: string; title: string; via: string; series: string; source: string | null }[] = [
  { key: 'link', title: 'Monolith', via: '/d', series: 'rate.link', source: null },
  { key: 'bms', title: 'BMS', via: 'CAN', series: 'rate.bms', source: 'bms' },
  { key: 'inverter', title: '인버터', via: 'CAN', series: 'rate.inverter', source: 'inverter' },
  { key: 'pedal', title: 'APPS', via: 'CAN', series: 'rate.pedal', source: 'pedal' },
  { key: 'gps', title: 'GPS', via: '위치 잡음', series: 'rate.gps_fix', source: 'gps' },
]

// ---------------------------------------------------------------- 수신이 꺼졌을 때의 틀

// 서버에 수신 폴더가 없으면(live_off) 엔진 상태가 오지 않는다. 화면 모양만 보이게 '아무것도 안 받은 상태' 를 만든다.
// 이름 · 단위는 엔진 lefdata.live 의 TILES · CHANNELS · SOURCES 를 옮겨 둔 것 — 수신이 켜지면 엔진 것을 쓴다
const OFF_TILES: [string, string, string, number][] = [
  ['gps.speed_kmh', '속도', 'km/h', 0], ['battery.power_kw', '전력', 'kW', 1], ['battery.voltage_v', '팩 전압', 'V', 1],
  ['battery.current_a', '배터리 전류', 'A', 0], ['bms.soc_pct', 'SOC', '%', 1], ['bms.cell_min_v', '셀 최저 전압', 'V', 3],
  ['bms.temp_max_c', '셀 최고 온도', '°C', 0], ['motor.t_motor_c', '모터 온도', '°C', 0], ['motor.t_igbt_c', '인버터(IGBT) 온도', '°C', 0],
  ['pedal.apps_pct', '가속페달', '%', 0], ['lv.voltage_v', 'LV 전압 (추정 환산)', 'V', 1],
  ['bms.temp_min_c', '셀 최저 온도', '°C', 0], ['bms.temp_spread_c', '셀 온도 차', '°C', 0], ['bms.dcl_kw', '방전 출력 한계 (DCL)', 'kW', 1], ['bms.temp_avg_c', '셀 평균 온도', '°C', 0],
  ['bms.temp_max_id', '최고 온도 써미스터 번호', '', 0], ['bms.temp_min_id', '최저 온도 써미스터 번호', '', 0],
  ['bms.bms_heatsink_c', 'BMS 방열판 온도', '°C', 0], ['bms.pack_resistance_ohm', '팩 저항', 'Ω', 3], ['bms.pack_ah', '팩 Ah', 'Ah', 1],
  ['bms.dod_pct', '방전 깊이 (DoD)', '%', 1], ['motor.vdc_v', 'DC 버스 전압', 'V', 0], ['motor.rpm', '회전수 (로거 환산)', 'rpm', 0],
  ['pedal.torque_cmd_pct', '토크 명령', '%', 0], ['pedal.apps1_pct', '가속페달 센서 1', '%', 1], ['pedal.apps2_pct', '가속페달 센서 2', '%', 1],
]
const offTile = ([id, label, unit, digits]: [string, string, string, number]): LiveTile =>
  ({ id, label, unit, sign: '', digits, value: null, age_s: null, status: 'none', alert: null })
const OFF_BY_ID = new Map(OFF_TILES.map((r) => [r[0], offTile(r)]))

// 뷰가 쓰는 칸 — 엔진이 그 칸을 안 보내면(옛 엔진 · 다른 차) 무수신 칸으로 (이름은 위 표, 없으면 id)
export function tileOr(tiles: Map<string, LiveTile>, id: string): LiveTile {
  return tiles.get(id) ?? OFF_BY_ID.get(id) ?? offTile([id, id, '', 0])
}
const OFF_SOURCES: [string, string][] = [['bms', 'BMS'], ['inverter', '인버터'], ['pedal', '가속페달'], ['gps', 'GPS'], ['lv', 'LV']]

export function offState(now = Date.now() / 1000): LiveState {
  return {
    contract: '', now, car: '', source: { replay: false },
    receiver: { status: 'unknown', since: null },
    device: { status: 'unknown', boot_at: null, firmware: null, boots: 0 },
    link: { status: 'none', age_s: null, period_s: 0.5, rate_10s: null, outages: 0, longest_s: null, since_s: null },
    sources: OFF_SOURCES.map(([id, label]) => ({ id, label, status: 'none', age_s: null, rate_10s: null, rate_status: null })),
    tiles: OFF_TILES.map(offTile),
    inverter: { status: 'none', enabled: null, ready: null, errors: [], warnings: [], age_s: null },
    pedal: { status: 'none', faults: [], age_s: null },
    position: null, laps: null, alerts: [], events: [], chart: { t: [], series: {} },
  }
}

// ---------------------------------------------------------------- 타일 모양 (게이지 · 숫자)

// 칸 크기는 뷰가 정한다 (views.tsx) — 값 · 상태 · 문턱은 엔진 것. 게이지는 범위가 있는 칸에만, small 은 숫자만
export type TileKind = 'big' | 'mid' | 'small'

// 추세선을 붙일 칸 — 한계를 모르는 값은 게이지 대신 최근 흐름을 본다 (그래프 값에 실려 오는 채널만)
export const SPARK = new Set(['gps.speed_kmh', 'motor.t_motor_c', 'motor.t_igbt_c'])
export const SPARK_S = 60

// 엔진이 범위 · 문턱(tiles[].scale)을 주기 전까지 쓰는 정해진 값 — 범위가 뻔한 것과 규정 한도만.
// 가정 문턱(셀 최고 55 °C · 셀 최저 3.0 / 2.8 V 등)은 엔진 문턱 파일에만 있으므로 여기 적지 않는다
const FIXED_SCALE: Record<string, LiveScale> = {
  'gps.speed_kmh': { min: 0, max: 120, warn: null, danger: null, dir: '>=' }, // 원형 게이지 눈금만 — 문턱 없음 (Formula 차 속도 범위)
  'bms.soc_pct': { min: 0, max: 100, warn: null, danger: null, dir: '<=' },
  'pedal.apps_pct': { min: 0, max: 100, warn: null, danger: null, dir: '>=' },
  'battery.power_kw': { min: -20, max: 90, warn: null, danger: 80, dir: '>=' }, // 규정 80 kW ([기술] 제68조)
  'bms.temp_max_c': { min: 0, max: 70, warn: null, danger: 60, dir: '>=' }, // 규정 60 °C ([기술] 제62조)
}
export const scaleOf = (t: LiveTile): LiveScale | null => t.scale ?? FIXED_SCALE[t.id] ?? null

// 게이지 위치 0 ~ 1 (범위 밖은 끝에 붙인다)
export function gaugePos(v: number, s: LiveScale): number {
  return s.max > s.min ? Math.min(1, Math.max(0, (v - s.min) / (s.max - s.min))) : 0
}

// 추세선 점 — 최근 span 초의 값을 0 ~ 1 (가로 = 시간, 세로 = 그 구간의 최소 ~ 최대). 끊긴 곳은 null
export function sparkPoints(buf: ChartBuf, id: string, now: number, span = SPARK_S): ([number, number] | null)[] {
  const vs = buf.series[id]
  if (!vs) return []
  const rows: [number, number | null][] = []
  buf.t.forEach((t, i) => t >= now - span && rows.push([t, vs[i]]))
  const ok = rows.map((r) => r[1]).filter((v): v is number => v !== null)
  if (ok.length < 2) return []
  const lo = Math.min(...ok), hi = Math.max(...ok)
  return rows.map(([t, v]) => (v === null ? null : [(t - (now - span)) / span, hi > lo ? (v - lo) / (hi - lo) : 0.5]))
}

// ---------------------------------------------------------------- 연결 불 (게이지 칸 오른쪽 위, §6-1)

// 칸 → 그 값이 오는 원천 (엔진 lefdata.live 의 Ch.source 와 같게 — 채널 이름 앞부분으로)
export function tileSource(id: string): string | null {
  if (id.startsWith('gps.')) return 'gps'
  if (id.startsWith('battery.') || id.startsWith('bms.')) return 'bms'
  if (id.startsWith('motor.')) return 'inverter'
  if (id.startsWith('pedal.')) return 'pedal'
  if (id.startsWith('lv.')) return 'lv'
  return null
}

export type LampTone = 'ok' | 'warn' | 'danger' | 'stale' | 'none'

// 원천의 10 s 수신률 상태(rate_status, 80 / 50 %)를 그대로 (사용자 결정 11-12). 수신률을 안 주는 원천(GPS)은 원천 상태로,
// LV 는 송신 장치 값이라 장치 수신 상태로. 서버 응답이 끊기면 회색, 수신이 꺼진 틀이면 빈 불
export function lampOf(s: LiveState, tileId: string, frozen: boolean, off: boolean): { tone: LampTone; text: string } {
  if (off) return { tone: 'none', text: '수신 꺼짐' }
  const id = tileSource(tileId)
  const src = id ? s.sources.find((x) => x.id === id) : undefined
  let tone: LampTone
  let text: string
  if (id === 'lv') {
    const st = s.link.status
    tone = st === 'ok' ? 'ok' : st === 'late' ? 'warn' : 'danger'
    text = `장치 수신 ${rateText(s.link)}`
  } else if (!src) {
    return { tone: 'none', text: '원천 모름' }
  } else if (src.rate_status) {
    tone = src.rate_status
    text = `${src.label} 10 s 수신률 ${pctText(src.rate_10s)}`
  } else {
    tone = src.status === 'ok' ? 'ok' : src.status === 'stale' ? 'warn' : 'danger'
    text = src.id === 'gps' ? `GPS 위치 잡음 ${pctText(src.fix_10s)}` : `${src.label} ${sourceWord(src.status, false).text}`
  }
  return frozen ? { tone: 'stale', text: `${text} (화면 갱신 멈춤)` } : { tone, text }
}

// ---------------------------------------------------------------- 그래프 손질 (축 · 랩 꼭짓점 · 끊긴 띠)

// 축 눈금 — 위 · 가운데 · 아래 세 값이 모두 읽기 좋은 수(간격의 배수)가 되게 범위를 넓힌다 (값은 그대로, 보이는 범위만).
// 간격 후보 1 · 2 · 2.5 · 4 · 5 × 10^k 중 칸 수가 짝수(가운데가 눈금) · 6 칸 이하이고 범위가 가장 좁은 것. 소수 자리가 늘어나는 후보(0.25 등)는 뺀다
const decimals = (x: number): number => {
  for (let d = 0; d <= 6; d++) if (Math.abs(x * 10 ** d - Math.round(x * 10 ** d)) < 1e-9) return d
  return 6
}
export function niceAxis(lo: number, hi: number): { lo: number; hi: number; digits: number } {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return { lo: 0, hi: 1, digits: 0 }
  if (hi - lo < 1e-9) {
    const pad = Math.abs(hi) > 1 ? Math.abs(hi) * 0.05 : 0.5
    lo -= pad
    hi += pad
  }
  const mag0 = 10 ** Math.floor(Math.log10((hi - lo) / 4))
  let best: { lo: number; hi: number; digits: number } | null = null
  for (const mag of [mag0, mag0 * 10, mag0 * 100]) {
    for (const m of [1, 2, 2.5, 4, 5]) {
      const step = m * mag
      const d = decimals(step)
      if (d > Math.max(0, decimals(mag))) continue
      const a = Math.floor(lo / step + 1e-9)
      let b = Math.ceil(hi / step - 1e-9)
      if ((b - a) % 2) b += 1
      if (b - a > 6) continue
      const r = { lo: Number((a * step).toFixed(d)), hi: Number((b * step).toFixed(d)), digits: d }
      if (!best || r.hi - r.lo < best.hi - best.lo - 1e-9) best = r
    }
  }
  return best ?? { lo, hi, digits: 2 }
}

// 작은 그래프의 세로 범위 — 받은 값 + 한도 · 주의 선(+ 0)을 다 담는다. 받은 값이 없으면 게이지 범위(span)로
export function miniRange(
  values: (number | null)[], marks: (number | null | undefined)[], zero = false, span: [number, number] | null = null,
): { lo: number; hi: number; digits: number } {
  const num = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
  const data = values.filter(num)
  const xs = [...data, ...marks.filter(num)]
  if (!data.length && span) xs.push(...span)
  if (zero) xs.push(0)
  if (!xs.length) return niceAxis(0, 1)
  return niceAxis(Math.min(...xs), Math.max(...xs))
}

// 랩 꼭짓점 — 끝난 랩(t_end, 그 랩 값)과 이번 랩(지금까지의 값). 값이 없는 랩은 건너뛴다. 최근 keep 랩만
export interface LapPoints {
  done: { lap: number; t: number; v: number }[]
  cur: { lap: number; v: number } | null
}
export function lapPoints(laps: LiveState['laps'], key: string, keep = 8): LapPoints {
  const val = (l: LiveLap | null | undefined) => {
    const v = l ? (l as unknown as Record<string, unknown>)[key] : null
    return typeof v === 'number' && Number.isFinite(v) ? v : null
  }
  const done: LapPoints['done'] = []
  for (const l of laps?.rows ?? []) {
    const v = val(l)
    if (v !== null) done.push({ lap: l.lap, t: l.t_end, v })
  }
  const cv = val(laps?.current)
  return { done: done.slice(-keep), cur: cv !== null && laps?.current ? { lap: laps.current.lap, v: cv } : null }
}

// 엔진이 랩 값을 주는 채널인가 — 끝난 랩 중 하나라도 값이 있으면
export const hasLapValues = (laps: LiveState['laps'], key: string): boolean => lapPoints(laps, key).done.length > 0

// 수신이 끊긴 띠 — 모든 시리즈가 비어 있는 줄이 이어진 구간 (엔진은 끊긴 동안의 격자 칸을 빈 값으로 채운다). from 이후만.
// minS 보다 짧은 빈칸(격자 한두 칸 — 메시지가 조금 늦게 온 것)은 띠로 그리지 않는다 (선은 그대로 끊긴다)
// 끊김으로 치는 길이 (s). 장치는 0.5 s 마다 보내고 엔진 격자도 0.5 s 라, 전달이 조금만 밀려도 한 칸씩 빈다.
// 그보다 짧은 빈 칸은 선을 잇고(그림만 — 값을 만들지 않는다), 이 길이 넘게 빈 곳만 끊고 어두운 띠를 깐다
export const GAP_S = 2

// 시리즈 하나의 빈 칸 중 앞뒤 받은 값 사이가 maxS 보다 짧은 것은 undefined (uPlot 이 건너 잇는다), 긴 것은 null (끊는다)
export function bridgeShort(t: number[], vs: (number | null)[], maxS = GAP_S): (number | null | undefined)[] {
  const out: (number | null | undefined)[] = vs.slice()
  let prev = -1
  for (let i = 0; i < vs.length; i++) {
    if (vs[i] == null) continue
    if (prev >= 0 && i - prev > 1 && t[i] - t[prev] < maxS) for (let k = prev + 1; k < i; k++) out[k] = undefined
    prev = i
  }
  return out
}

export function gapBands(buf: ChartBuf, from: number, minS = GAP_S): [number, number][] {
  const keys = Object.keys(buf.series)
  if (!keys.length) return []
  const out: [number, number][] = []
  let start: number | null = null
  let prev: number | null = null
  buf.t.forEach((t, i) => {
    const empty = keys.every((k) => buf.series[k][i] == null)
    if (empty && t >= from) {
      if (start === null) start = prev ?? t
    } else if (start !== null) {
      out.push([start, t])
      start = null
    }
    prev = t
  })
  if (start !== null && prev !== null) out.push([start, prev])
  return out.filter(([a, b]) => b - a >= minS)
}

// 커서 시각의 값 — 가장 가까운 줄 (계산이 아니라 찾기). 그 줄이 비었으면 null
export function valueAt(buf: ChartBuf, id: string, t: number): number | null {
  const ts = buf.t
  if (!ts.length || !buf.series[id]) return null
  let lo = 0
  let hi = ts.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (ts[mid] < t) lo = mid
    else hi = mid
  }
  const i = Math.abs(ts[lo] - t) <= Math.abs(ts[hi] - t) ? lo : hi
  return buf.series[id][i] ?? null
}
