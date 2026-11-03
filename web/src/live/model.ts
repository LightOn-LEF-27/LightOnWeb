// 실시간 화면 — 받은 값을 그리기 위한 손질만 한다. 숫자 · 상태 · 문구는 엔진이 정한다 (계약 §12-4).
// 화면이 스스로 정하는 것은 둘뿐이다: 그래프 창(최근 5분)과, 서버 응답이 끊겼을 때 화면 전체를 낡음으로 돌리는 것.

import { formatValue } from '../overview/model'
import type { LiveEvent, LiveScale, LiveSource, LiveState, LiveStatus, LiveTile } from '../types'

export const POLL_MS = 500
export const WINDOW_S = 300 // 그래프에 보이는 최근 5분
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

// 뷰마다 최근 5분 그래프 — 엔진이 그래프 값(chart.series)에 실어 주는 채널만 그려진다 (없으면 "엔진 값 준비 중")
export const PANELS: Record<'battery' | 'powertrain', Panel[]> = {
  battery: [
    { key: 'power', title: '전력 · 전류', series: [{ id: 'battery.power_kw', color: 0 }, { id: 'battery.current_a', right: true, color: 2 }] },
    { key: 'cellv', title: '셀 최저 전압', series: [{ id: 'bms.cell_min_v', color: 2 }] },
    { key: 'cellt', title: '셀 온도', series: [{ id: 'bms.temp_max_c', color: 0 }, { id: 'bms.temp_min_c', color: 2 }] },
  ],
  powertrain: [
    { key: 'speed', title: '속도 · RPM', series: [{ id: 'gps.speed_kmh', color: 2 }, { id: 'motor.rpm', right: true, color: 1 }] },
    { key: 'pedal', title: 'APPS · 토크 명령', series: [{ id: 'pedal.apps_pct', color: 0 }, { id: 'pedal.torque_cmd_pct', color: 2 }] },
    { key: 'temp', title: '온도', series: [{ id: 'motor.t_motor_c', color: 0 }, { id: 'motor.t_igbt_c', color: 2 }] },
  ],
}

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
  ['bms.temp_min_c', '셀 최저 온도', '°C', 0], ['bms.dcl_kw', '방전 출력 한계 (DCL)', 'kW', 1], ['bms.temp_avg_c', '셀 평균 온도', '°C', 0],
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
