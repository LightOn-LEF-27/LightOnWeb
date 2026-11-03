// 25Evo 실시간 뷰 넷 — 메인 · 배터리 · 인버터 · 모터 · 수신 · DAQ (와이어프레임 v21, 2026-11-03 사용자 확정).
// 칸 배치만 여기서 정한다 (어느 칸이 어느 뷰인지는 cars.ts VIEW_TILES 와 같게). 값 · 상태 · 문구는 엔진 것

import { Link } from 'react-router-dom'

import type { LiveSource, LiveState, LiveTile, LiveTrack } from '../types'
import { type LiveCar } from './cars'
import LiveChart from './LiveChart'
import LiveMap from './LiveMap'
import {
  ageText, type ChartBuf, clock, deviceView, type EventRow, PANELS, pctText, RATE_PANELS, shownStatus, sourceWord,
  tileValue,
} from './model'
import {
  AppsDetail, BatteryLaps, EventList, Facts, InverterLine, InverterPanel, LapEnergy, LapStrip, RateBar, RecentLaps, Tile, TileAge,
} from './parts'

export interface ViewCtx {
  s: LiveState
  car: LiveCar
  buf: ChartBuf
  frozen: boolean
  off: boolean // 수신 꺼짐 — 틀만
  theme: string
  narrow: boolean // 폰
  tiles: Map<string, LiveTile>
  tile: (id: string) => LiveTile // 없으면 무수신 칸
  spark: (id: string) => ([number, number] | null)[] | null
  events: EventRow[] // 접은 사건
  track: LiveTrack | null
  trackMsg: string
  path: [number, number][]
}

// ---------------------------------------------------------------- ① 메인 — 드라이버와 소통

export function MainView({ c }: { c: ViewCtx }) {
  const T = (id: string, kind: 'big' | 'mid' | 'small' = 'mid') => <Tile key={id} t={c.tile(id)} kind={kind} frozen={c.frozen} spark={c.spark(id)} />
  return (
    <div className="lv lv-main">
      <div className="lv-col">
        <section className="panel live-map">
          <h2>트랙맵</h2>
          <MapBox c={c} height={c.narrow ? 260 : 380} />
          <LapStrip laps={c.s.laps} />
        </section>
        <RecentLaps laps={c.s.laps} />
      </div>
      <div className="lv-col">
        <div className="lv-tiles c2">
          {T('gps.speed_kmh', 'big')}
          {T('battery.power_kw', 'big')}
          {T('bms.soc_pct', 'big')}
          {T('bms.temp_max_c', 'big')}
        </div>
        <div className="lv-tiles c2">
          {T('motor.t_motor_c')}
          {T('motor.t_igbt_c')}
        </div>
        <div className="lv-tiles c4">
          {T('bms.cell_min_v')}
          {T('bms.dcl_kw')}
          {T('pedal.apps_pct')}
          {T('lv.voltage_v')}
        </div>
        <div className="lv-pair">
          <LapEnergy laps={c.s.laps} />
          <InverterLine inv={c.s.inverter} frozen={c.frozen} />
        </div>
        <EventList rows={c.events} limit={5} more={<Link to={`/live/${c.car.id}/link`}>전체 — 수신 · DAQ 뷰</Link>} />
      </div>
    </div>
  )
}

function MapBox({ c, height }: { c: ViewCtx; height: number }) {
  const pos = c.s.position
  if (!c.track) return <p className="panel-msg muted" style={{ minHeight: 60 }}>{c.trackMsg}</p>
  return <LiveMap track={c.track} trail={c.path} position={pos} fresh={!c.frozen && !!pos && pos.age_s <= 2} theme={c.theme} height={height} />
}

// ---------------------------------------------------------------- ② 배터리 팩

export function BatteryView({ c }: { c: ViewCtx }) {
  const T = (id: string, kind: 'big' | 'mid' | 'small' = 'mid') => <Tile key={id} t={c.tile(id)} kind={kind} frozen={c.frozen} spark={c.spark(id)} />
  return (
    <div className="lv lv-two">
      <div className="lv-col">
        <div className="lv-tiles c2">
          {T('bms.soc_pct', 'big')}
          {T('battery.power_kw', 'big')}
        </div>
        <div className="lv-tiles c3">
          {T('battery.voltage_v')}
          {T('battery.current_a')}
          {T('bms.dcl_kw')}
        </div>
        <div className="lv-tiles c2">
          {T('bms.cell_min_v')}
          <ThermTile c={c} />
          {T('bms.temp_max_c')}
          {T('bms.temp_min_c')}
        </div>
        <section className="panel">
          <h2>BMS 기타</h2>
          <Facts items={['bms.bms_heatsink_c', 'bms.pack_resistance_ohm', 'bms.pack_ah', 'bms.dod_pct'].map(c.tile)} frozen={c.frozen} />
        </section>
      </div>
      <div className="lv-col">
        <Charts c={c} panels={PANELS.battery} />
        <BatteryLaps laps={c.s.laps} />
      </div>
    </div>
  )
}

// 써미스터 — 가장 뜨거운 · 차가운 번호와 평균 (0x6D0 슬롯이라 드물게 온다)
function ThermTile({ c }: { c: ViewCtx }) {
  const hi = c.tile('bms.temp_max_id'), lo = c.tile('bms.temp_min_id'), avg = c.tile('bms.temp_avg_c')
  const st = shownStatus(hi.status, c.frozen)
  return (
    <div className={`live-tile mid ${st}`} title="bms.temp_max_id · bms.temp_min_id · bms.temp_avg_c">
      <div className="live-tile-label">
        <span>써미스터</span>
      </div>
      <div className="live-tile-value">
        {hi.value === null ? '—' : `#${tileValue(hi)}`}
        <span className="live-tile-unit">가장 뜨거움</span>
      </div>
      <div className="live-tile-sub">
        {lo.value === null ? '—' : `#${tileValue(lo)}`} 가장 차가움 · {avg.label} {tileValue(avg)}
        {avg.value !== null && ` ${avg.unit}`}
      </div>
      <TileAge st={st} age={hi.age_s} frozen={c.frozen} />
    </div>
  )
}

function Charts({ c, panels }: { c: ViewCtx; panels: (typeof PANELS)['battery'] }) {
  return (
    <section className="panel live-charts">
      <h2>최근 5분</h2>
      {panels.map((p) => (
        <LiveChart key={p.key} title={p.title} series={p.series} buf={c.buf} now={c.s.now} tiles={c.tiles} theme={c.theme} height={c.narrow ? 110 : 130} />
      ))}
    </section>
  )
}

// ---------------------------------------------------------------- ③ 인버터 · 모터

export function PowertrainView({ c }: { c: ViewCtx }) {
  const T = (id: string, extra: Partial<Parameters<typeof Tile>[0]> = {}) => (
    <Tile key={id} t={c.tile(id)} kind="mid" frozen={c.frozen} spark={c.spark(id)} {...extra} />
  )
  return (
    <div className="lv lv-two">
      <div className="lv-col">
        <div className="lv-tiles c2">
          {T('motor.t_motor_c')}
          {T('motor.t_igbt_c')}
          {T('motor.vdc_v')}
          {T('motor.rpm', { sign: true })}
        </div>
        <div className="lv-tiles c2">
          {T('pedal.apps_pct', { children: <AppsDetail s1={c.tile('pedal.apps1_pct')} s2={c.tile('pedal.apps2_pct')} pedal={c.s.pedal} frozen={c.frozen} /> })}
          {T('pedal.torque_cmd_pct', { sign: true })}
        </div>
        <InverterPanel inv={c.s.inverter} frozen={c.frozen} />
      </div>
      <div className="lv-col">
        <Charts c={c} panels={PANELS.powertrain} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- ④ 수신 · DAQ

const LINK_WORD: Record<LiveState['link']['status'], [string, string]> = {
  ok: ['정상', 'ok'], late: ['늦음', 'warn'], lost: ['끊김', 'danger'], none: ['수신 없음', 'none'],
}

export function LinkView({ c }: { c: ViewCtx }) {
  const { s, frozen } = c
  const dev = c.car.device ?? '장치'
  const linkTone = frozen && s.link.status !== 'none' ? 'stale' : LINK_WORD[s.link.status][1]
  return (
    <div className="lv lv-two">
      <div className="lv-col">
        <div className="lv-tiles c4">
          <Stat label={`마지막 수신 (${dev})`} tone={linkTone} value={s.link.age_s === null ? '—' : s.link.age_s.toFixed(1)} unit={s.link.age_s === null ? '' : 's 전'} />
          <Stat label={`10 s 수신률 (${dev})`} tone={linkTone} value={s.link.rate_10s == null ? '—' : String(Math.round(s.link.rate_10s * 100))} unit={s.link.rate_10s == null ? '' : '%'} />
          <Stat label="오늘 끊김" tone="plain" value={String(s.link.outages)} unit={`번${s.link.longest_s !== null ? ` · 최장 ${Math.round(s.link.longest_s)} s` : ''}`} />
          <Tile t={c.tile('lv.voltage_v')} kind="mid" frozen={frozen} />
        </div>
        <SourcesTable c={c} />
        <DevicePanel c={c} />
        <EventList rows={c.events} />
      </div>
      <div className="lv-col">
        <RateCharts c={c} />
      </div>
    </div>
  )
}

// 숫자 칸 (엔진 타일이 아닌 수신 값 — 마지막 수신 · 수신률 · 끊김)
function Stat({ label, value, unit, tone }: { label: string; value: string; unit: string; tone: string }) {
  return (
    <div className={`live-tile mid ${tone}`}>
      <div className="live-tile-label">
        <span>{label}</span>
      </div>
      <div className="live-tile-value">
        {value}
        {unit && <span className="live-tile-unit">{unit}</span>}
      </div>
    </div>
  )
}

// 원천별 — 상태 · 마지막 · 10 s 수신률 · 지금 값. 송신 장치(/d) 줄이 맨 위
function SourcesTable({ c }: { c: ViewCtx }) {
  const { s, frozen } = c
  const v = (id: string) => {
    const t = c.tile(id)
    return t.value === null ? '—' : `${tileValue(t)}${t.unit ? ` ${t.unit}` : ''}`
  }
  const now: Record<string, string> = {
    bms: `${v('battery.voltage_v')} · ${v('battery.current_a')} · SOC ${v('bms.soc_pct')}`,
    inverter: `모터 ${v('motor.t_motor_c')} · IGBT ${v('motor.t_igbt_c')}`,
    pedal: v('pedal.apps_pct'),
    gps: v('gps.speed_kmh'),
    lv: v('lv.voltage_v'),
  }
  const [lw, lt] = LINK_WORD[s.link.status]
  const linkTone = frozen && s.link.status !== 'none' ? 'stale' : lt
  const rate = (src: LiveSource) => {
    if (src.id === 'gps') return <span className="muted">위치 잡음 {pctText(src.fix_10s)}</span>
    if (src.id === 'lv') return <span className="muted">{c.car.device ?? '장치'} 와 같음</span>
    return <RateBar r={src.rate_10s} status={src.rate_status ?? null} frozen={frozen} />
  }
  return (
    <section className="panel">
      <h2>원천별</h2>
      <table className="live-table live-sources-table">
        <thead>
          <tr>
            <th>원천</th>
            <th>상태</th>
            <th>마지막</th>
            <th>10 s 수신률</th>
            <th>지금 값</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>
              <span className={`live-flag ${linkTone}`}>
                <span className="live-dot" aria-hidden="true" />
                {c.car.device ?? '장치'}
              </span>
            </td>
            <td>{frozen && s.link.status !== 'none' ? '낡음' : lw}</td>
            <td>{ageText(s.link.age_s) || '—'}</td>
            <td>
              <RateBar r={s.link.rate_10s} status="plain" frozen={frozen} />
            </td>
            <td className="muted">/d {s.link.period_s} s 주기</td>
          </tr>
          {s.sources.map((src) => {
            const w = sourceWord(src.status, frozen)
            return (
              <tr key={src.id}>
                <td>
                  <span className={`live-flag ${w.tone}`}>
                    <span className="live-dot" aria-hidden="true" />
                    {src.label}
                  </span>
                </td>
                <td className={w.tone === 'ok' ? '' : 'muted'}>{w.text}</td>
                <td>{ageText(src.age_s) || '—'}</td>
                <td>{rate(src)}</td>
                <td className="muted">{now[src.id] ?? ''}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="live-note muted">CAN 원천은 새 프레임 기준 · 원천이 다 같이 떨어지면 무선에서 CAN 묶음을 잃은 것, 하나만 떨어지면 그 장치 쪽</p>
    </section>
  )
}

function DevicePanel({ c }: { c: ViewCtx }) {
  const { s } = c
  const name = c.car.device ?? '장치'
  const dev = deviceView(s.device, name)
  const rx =
    s.receiver.status === 'connected' ? `연결${s.receiver.since ? ` · ${clock(s.receiver.since)}부터` : ''}`
    : s.receiver.status === 'disconnected' ? `끊김${s.receiver.since ? ` · ${clock(s.receiver.since)}부터` : ''}`
    : '확인 중'
  const tone = (t: string) => (c.frozen && t !== 'none' ? 'stale' : t)
  return (
    <section className="panel">
      <h2>장치 · 수신기</h2>
      <dl className="live-facts wide">
        <dt>장치 ({name})</dt>
        <dd>
          <span className={`live-flag ${tone(dev.tone)}`}>
            <span className="live-dot" aria-hidden="true" />
            {dev.text}
          </span>
          {s.device.boots > 0 && ` · 부팅 ${s.device.boots}번`}
          {s.device.boot_at !== null && ` (마지막 ${clock(s.device.boot_at)})`}
        </dd>
        <dt>펌웨어</dt>
        <dd>{s.device.firmware ?? '—'}</dd>
        <dt>수신기</dt>
        <dd>
          <span className={`live-flag ${tone(s.receiver.status === 'connected' ? 'ok' : s.receiver.status === 'disconnected' ? 'danger' : 'none')}`}>
            <span className="live-dot" aria-hidden="true" />
            {rx}
          </span>
        </dd>
        <dt>깨진 줄 · 모르는 토픽</dt>
        <dd>
          {s.source.bad_rows ?? 0} · {s.source.unknown_topics ?? 0}
        </dd>
      </dl>
    </section>
  )
}

// 원천별 수신률 (최근 5분) — 칸마다 0 ~ 100 % 같은 눈금, 선 색 고정. % 숫자만 상태 색
function RateCharts({ c }: { c: ViewCtx }) {
  const src = new Map(c.s.sources.map((x) => [x.id, x]))
  return (
    <section className="panel live-charts">
      <h2>원천별 수신률 (최근 5분, 10 s 창)</h2>
      {RATE_PANELS.map((p) => {
        const x = p.source ? src.get(p.source) : null
        const r = p.source === null ? c.s.link.rate_10s : p.source === 'gps' ? x?.fix_10s : x?.rate_10s
        const tone = c.frozen ? 'stale' : p.source === null || p.source === 'gps' ? 'plain' : x?.rate_status ?? 'none'
        return (
          <LiveChart
            key={p.key}
            title={p.title}
            series={[{ id: p.series, color: 2 }]}
            buf={c.buf}
            now={c.s.now}
            tiles={c.tiles}
            theme={c.theme}
            height={c.narrow ? 80 : 104}
            pct
            keys={false}
            extra={
              <>
                <span className="muted">{p.via}</span>
                <span className={`live-rate-num ${tone}`}>{pctText(r)}</span>
              </>
            }
          />
        )
      })}
      <p className="live-note muted">다섯 줄이 같이 떨어지면 무선 끊김 · 한 줄만 낮으면 그 장치 쪽. % 색: 80 % 이상 정상 · 50 % 미만 위험</p>
    </section>
  )
}
