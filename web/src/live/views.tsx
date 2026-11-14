// 25Evo 실시간 뷰 — 계기판 · 그래프 · 배터리 · 인버터 · 모터 · 수신 · DAQ (실시간화면_v1.md §6, 2026-11-12 개편 — 계기판 · 그래프를 나눔).
// 칸 배치만 여기서 정한다 (어느 칸이 어느 뷰인지는 cars.ts VIEW_TILES 와 같게). 값 · 상태 · 문구는 엔진 것

import { type ReactNode, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import type { LiveSource, LiveState, LiveTile, LiveTrack } from '../types'
import { type LiveCar } from './cars'
import { GaugeTile, LapMini, PedalTile, TimeMini } from './gauge'
import LiveChart, { type Limit } from './LiveChart'
import LiveMap from './LiveMap'
import {
  ageText, type ChartBuf, clock, deviceView, eventText, type EventRow, gapBands, GRAPH_GROUPS, GRAPH_PANELS, GRAPH_RANGES, type GraphGroup,
  hasLapValues, lampOf, lapPoints, worstLamp, miniRange, pctText, RATE_PANELS, scaleOf, shownStatus, sourceWord, tileValue,
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

// 칸의 한도 · 주의 (tiles[].scale — 엔진 문턱, 없으면 정해진 규정 한도)
function limitsOf(t: LiveTile): Limit[] {
  const s = scaleOf(t)
  const out: Limit[] = []
  if (s?.warn != null) out.push({ v: s.warn, kind: 'warn' })
  if (s?.danger != null) out.push({ v: s.danger, kind: 'danger' })
  return out
}

// 받은 값이 없을 때 작은 그래프 세로 범위 = 게이지 범위
const spanOf = (t: LiveTile): [number, number] | null => {
  const s = scaleOf(t)
  return s ? [s.min, s.max] : null
}

// 상태 → 작은 그래프 · 꼭짓점 색 (정상은 색 없이)
const TONE_COLOR: Record<string, string> = { ok: 'var(--gauge-fill)', warn: 'var(--warn)', danger: 'var(--danger)', stale: 'var(--text-muted)', none: 'var(--text-muted)' }

// ---------------------------------------------------------------- ① 계기판 — 원형 게이지 + 아래 작은 그래프 (§6-1)

const MINI_S = 60 // 게이지 아래 시간축 그래프 — 최근 1분
const LAP_FALLBACK_S = 300 // 랩 값이 없으면 시간축 선으로 (사용자 결정 11-12) — 온도는 느리게 변해 5분

export function MainView({ c }: { c: ViewCtx }) {
  const lamp = (id: string) => lampOf(c.s, id, c.frozen, c.off)
  const G = (id: string, size: 'big' | 'mid', mini?: ReactNode) => (
    <GaugeTile key={id} t={c.tile(id)} size={size} frozen={c.frozen} lamp={lamp(id)}>
      {mini}
    </GaugeTile>
  )
  const N = (id: string, extra: Partial<Parameters<typeof Tile>[0]> = {}) => <Tile key={id} t={c.tile(id)} kind="small" frozen={c.frozen} {...extra} />
  const win = (id: string) => {
    const vs = c.buf.series[id] ?? []
    return vs.filter((_, i) => c.buf.t[i] >= c.s.now - MINI_S)
  }
  const now = c.s.now

  // 속도 아래 — 가속(초록, 왼쪽 축 %) · 브레이크(빨강, 오른쪽 축 V — 25Evo 는 압력 센서 전압까지만, 11-13 사용자 결정)
  const apps = c.tile('pedal.apps_pct')
  const brake = c.tile('brake.ain1_v')
  const speedMini = (
    <TimeMini buf={c.buf} now={now} span={MINI_S} lines={[{ id: 'pedal.apps_pct', color: 'var(--ok)' }]} ax={{ lo: 0, hi: 100, digits: 0 }} unit="%"
      right={{ id: 'brake.ain1_v', color: 'var(--danger)', ax: miniRange(win('brake.ain1_v'), [], true, spanOf(brake)), unit: brake.unit || 'V' }}
      limits={[]} legend={[{ color: 'var(--ok)', text: '가속' }, { color: 'var(--danger)', text: '브레이크' }]} label={`${apps.label} · ${brake.label}, 최근 1분`} />
  )
  const timeMini = (id: string) => {
    const t = c.tile(id)
    const lim = limitsOf(t)
    return (
      <TimeMini buf={c.buf} now={now} span={MINI_S} lines={[{ id, color: TONE_COLOR.ok }]}
        ax={miniRange(win(id), lim.map((l) => l.v), id === 'battery.power_kw', spanOf(t))}
        unit={t.unit} limits={lim} label={`${t.label}, 최근 1분`} />
    )
  }
  // 셀 최고 온도 아래 — 랩마다 꼭짓점 (엔진 laps.rows[].cell_t_max_c), 랩 값이 없으면 최근 5분 선
  const tmax = c.tile('bms.temp_max_c')
  const tlim = limitsOf(tmax)
  const tpts = lapPoints(c.s.laps, 'cell_t_max_c')
  const tempMini =
    tpts.done.length || tpts.cur ? (
      <LapMini pts={tpts} ax={miniRange([...tpts.done.map((d) => d.v), tpts.cur?.v ?? null], tlim.map((l) => l.v), false, spanOf(tmax))} unit={tmax.unit} limits={tlim}
        color={TONE_COLOR.ok} curColor={TONE_COLOR[shownStatus(tmax.status, c.frozen)]} label={`${tmax.label}, 랩마다 최고`} />
    ) : (
      <TimeMini buf={c.buf} now={now} span={LAP_FALLBACK_S} lines={[{ id: 'bms.temp_max_c', color: TONE_COLOR.ok }]}
        ax={miniRange((c.buf.series['bms.temp_max_c'] ?? []).filter((_, i) => c.buf.t[i] >= now - LAP_FALLBACK_S), tlim.map((l) => l.v), false, spanOf(tmax))}
        unit={tmax.unit} limits={tlim} label={`${tmax.label}, 최근 5분`} />
    )

  return (
    <div className="lv lv-main">
      <div className="lv-col lv-mapcol">
        <section className="panel live-map">
          <h2>트랙맵</h2>
          <MapBox c={c} height={c.narrow ? 260 : 360} />
          <LapStrip laps={c.s.laps} />
        </section>
        <RecentLaps laps={c.s.laps} />
      </div>
      <div className="lv-col lv-gaugecol">
        <div className="lv-gauges big">
          {G('gps.speed_kmh', 'big', speedMini)}
          {G('battery.power_kw', 'big', timeMini('battery.power_kw'))}
          {G('bms.cell_min_v', 'big', timeMini('bms.cell_min_v'))}
          {G('bms.temp_max_c', 'big', tempMini)}
        </div>
        <div className="lv-gauges mid">
          {G('motor.t_motor_c', 'mid')}
          {G('motor.t_igbt_c', 'mid')}
          <PedalTile accel={apps} brake={brake} sensors={[c.tile('pedal.apps1_v'), c.tile('pedal.apps2_v')]} frozen={c.frozen} lamp={worstLamp(lamp('pedal.apps_pct'), lamp('brake.ain1_v'))} />
          {G('bms.soc_pct', 'mid')}
        </div>
        <div className="lv-tiles c4">
          {N('bms.dcl_kw')}
          {N('bms.temp_spread_c')}
          {N('motor.rpm', { sign: true })}
          {N('lv.voltage_v')}
        </div>
        <div className="lv-pair">
          <LapEnergy laps={c.s.laps} />
          <InverterLine inv={c.s.inverter} frozen={c.frozen} />
        </div>
        <EventLine rows={c.events} car={c.car} />
      </div>
    </div>
  )
}

// 사건 — 계기판에선 한 줄 (마지막 사건). 전체는 수신 · DAQ 뷰 (사용자 결정 11-11 — 사건은 DAQ 가 본다)
function EventLine({ rows, car }: { rows: EventRow[]; car: LiveCar }) {
  const e = rows[0]
  return (
    <div className="live-eventline">
      <span className="muted">마지막 사건</span>
      {e ? (
        <>
          <span className="mono muted">{clock(e.t)}</span>
          <span className={`live-event ${e.kind}`}>{eventText(e)}</span>
        </>
      ) : (
        <span className="muted">오늘 사건 없음</span>
      )}
      <Link className="live-eventline-go" to={`/live/${car.id}/link`}>
        전체 — 수신 · DAQ 뷰
      </Link>
    </div>
  )
}

function MapBox({ c, height }: { c: ViewCtx; height: number }) {
  const pos = c.s.position
  if (!c.track) return <p className="panel-msg muted" style={{ minHeight: 60 }}>{c.trackMsg}</p>
  // on_line false = 최근 달린 궤적이 기준선에서 멀리 벗어남 (콘 코스가 다름) → 기준선을 흐리게, 달린 궤적이 바탕. 같은 코스 판정은 아님
  const off = c.track.course?.on_line === false
  return (
    <div className="live-map-box">
      <LiveMap track={c.track} trail={c.path} position={pos} fresh={!c.frozen && !!pos && pos.age_s <= 2} dimLine={off} theme={c.theme} height={height} />
      {off && <span className="live-map-note">기준선에서 벗어남 · 달린 궤적 기준</span>}
    </div>
  )
}

// ---------------------------------------------------------------- ② 그래프 — 한 시간축에 칸을 쌓는다 (§6-1)

export function GraphView({ c }: { c: ViewCtx }) {
  const [range, setRange] = useState(300)
  const [group, setGroup] = useState<GraphGroup>('all')
  const [cursor, setCursor] = useState<number | null>(null)
  const panels = GRAPH_PANELS.filter((p) => group === 'all' || p.groups.includes(group))
  const now = c.s.now
  const bands = useMemo(() => gapBands(c.buf, now - range), [c.buf, now, range])
  // 칸마다 랩 꼭짓점 — 칸의 채널이 모두 랩 값을 받을 때만 (아니면 시간축 선)
  const lapsOf = useMemo(() => {
    const out: Record<string, Record<string, ReturnType<typeof lapPoints>> | null> = {}
    for (const p of GRAPH_PANELS) {
      if (!p.laps) continue
      const keys = p.series.map((x) => p.laps![x.id])
      out[p.key] = keys.every((k) => k && hasLapValues(c.s.laps, k))
        ? Object.fromEntries(p.series.map((x) => [x.id, lapPoints(c.s.laps, p.laps![x.id], 1000)]))
        : null
    }
    return out
  }, [c.s.laps])
  const limitsOfPanel = useMemo(() => {
    const out: Record<string, Limit[]> = {}
    for (const p of GRAPH_PANELS) out[p.key] = p.limits ? limitsOf(c.tile(p.limits)) : []
    return out
  }, [c.s.tiles])
  const h = c.narrow ? 110 : 120

  return (
    <div className="lv lv-graph">
      <div className="live-graphbar">
        <div className="live-seg" role="group" aria-label="구간">
          <span className="muted">구간</span>
          {GRAPH_RANGES.map(([s, name]) => (
            <button key={s} type="button" className={range === s ? 'on' : ''} aria-pressed={range === s} onClick={() => setRange(s)}>
              {name}
            </button>
          ))}
        </div>
        <div className="live-seg" role="group" aria-label="묶음">
          <span className="muted">묶음</span>
          {GRAPH_GROUPS.map(([g, name]) => (
            <button key={g} type="button" className={group === g ? 'on' : ''} aria-pressed={group === g} onClick={() => setGroup(g)}>
              {name}
            </button>
          ))}
        </div>
        <span className="live-graphbar-cursor muted">{cursor !== null ? `커서 ${clock(cursor)}` : '지금 값'}</span>
      </div>
      <section className="panel live-charts live-graphs">
        {panels.map((p, i) => (
          <LiveChart
            key={p.key}
            title={p.title}
            series={p.series}
            buf={c.buf}
            now={now}
            tiles={c.tiles}
            theme={c.theme}
            height={h}
            windowS={range}
            sync="live-graph"
            bands={bands}
            limits={limitsOfPanel[p.key]}
            laps={lapsOf[p.key] ?? null}
            xAxis={i === panels.length - 1}
            side
            cursor={cursor}
            onCursor={setCursor}
          />
        ))}
      </section>
    </div>
  )
}

// ---------------------------------------------------------------- ③ 배터리 팩

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
        </div>
        <div className="lv-tiles c3">
          {T('bms.temp_max_c')}
          {T('bms.temp_min_c')}
          {T('bms.temp_spread_c')}
        </div>
        <section className="panel">
          <h2>BMS 기타</h2>
          <Facts items={['bms.bms_heatsink_c', 'bms.pack_resistance_ohm', 'bms.pack_ah', 'bms.dod_pct'].map(c.tile)} frozen={c.frozen} />
        </section>
      </div>
      <div className="lv-col">
        <BatteryLaps laps={c.s.laps} />
        <GraphLink c={c} what="전력 · 셀 전압 · 셀 온도" />
      </div>
    </div>
  )
}

// 그래프는 그래프 뷰로 모았다 (§6-1)
function GraphLink({ c, what }: { c: ViewCtx; what: string }) {
  return (
    <p className="live-graphlink muted">
      {what} 그래프 — <Link to={`/live/${c.car.id}/graph`}>그래프 뷰</Link>
    </p>
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

// ---------------------------------------------------------------- ④ 인버터 · 모터

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
          {T('pedal.apps_pct', { children: <AppsDetail s1={c.tile('pedal.apps1_pct')} s2={c.tile('pedal.apps2_pct')} v1={c.tile('pedal.apps1_v')} v2={c.tile('pedal.apps2_v')} pedal={c.s.pedal} frozen={c.frozen} /> })}
          {T('pedal.torque_cmd_pct', { sign: true })}
        </div>
      </div>
      <div className="lv-col">
        <InverterPanel inv={c.s.inverter} frozen={c.frozen} />
        <GraphLink c={c} what="속도 · 회전수 · 가속페달 · 온도" />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- ⑤ 수신 · DAQ

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
        <dt>부품 상태</dt>
        <dd>
          <PartsState errors={s.device.errors} fatal={s.device.fatal} tone={tone} />
        </dd>
        <dt>수신기</dt>
        <dd>
          <span className={`live-flag ${tone(s.receiver.status === 'connected' ? 'ok' : s.receiver.status === 'disconnected' ? 'danger' : 'none')}`}>
            <span className="live-dot" aria-hidden="true" />
            {rx}
          </span>
        </dd>
        <dt>깨진 줄 · 버린 레코드 · 모르는 토픽</dt>
        <dd>
          {s.source.bad_rows ?? 0} · {s.source.bad_records ?? 0} · {s.source.unknown_topics ?? 0}
        </dd>
      </dl>
    </section>
  )
}

// Monolith 가 /d 에 실어 보내는 부품 상태 — 치명(예: SD)이면 값이 다 버려질 수 있다 (엔진 경고 W-DEV). /d 전엔 null
function PartsState({ errors, fatal, tone }: { errors?: string[] | null; fatal?: string[] | null; tone: (t: string) => string }) {
  if (fatal == null && errors == null) return <>—</>
  const f = fatal ?? [], e = (errors ?? []).filter((n) => !f.includes(n))
  if (!f.length && !e.length) return <span className={`live-flag ${tone('ok')}`}><span className="live-dot" aria-hidden="true" />이상 없음</span>
  return (
    <>
      {f.length > 0 && <span className={`live-flag ${tone('warn')}`}><span className="live-dot" aria-hidden="true" />치명 {f.join(' · ')}</span>}
      {f.length > 0 && e.length > 0 && ' · '}
      {e.length > 0 && `오류 ${e.join(' · ')}`}
    </>
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
