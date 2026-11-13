// 계기판 칸 (§6-1, 2026-11-12 사용자 결정) — 원형 게이지 · 연결 불 · 게이지 아래 작은 그래프.
// 그리기만 한다. 값 · 상태 · 범위 · 문턱은 엔진 것 (tiles[].scale, 없으면 model.scaleOf 의 정해진 값). 분기는 id · status 로만

import type { ReactNode } from 'react'

import { formatValue } from '../overview/model'
import type { LiveScale, LiveStatus, LiveTile } from '../types'
import type { Limit } from './LiveChart'
import { bridgeShort, type ChartBuf, gaugePos, type LampTone, type LapPoints, scaleOf, shownStatus, STATUS_WORD, tileValue } from './model'
import { TileAge } from './parts'

// ---------------------------------------------------------------- 연결 불

export function Lamp({ tone, text }: { tone: LampTone; text: string }) {
  return <i className={`live-lamp-dot ${tone}`} role="img" aria-label={`연결 · ${text}`} title={text} />
}

// ---------------------------------------------------------------- 원형 게이지 (270°)

const CX = 60
const CY = 62
const R = 48
const C = 2 * Math.PI * R
const A = C * 0.75 // 270°
const at = (p: number, r: number): [number, number] => {
  const a = ((135 + p * 270) * Math.PI) / 180
  return [CX + r * Math.cos(a), CY + r * Math.sin(a)]
}
const [SX, SY] = at(0, R)
const [EX, EY] = at(1, R)
const TRACK = `M ${SX.toFixed(2)} ${SY.toFixed(2)} A ${R} ${R} 0 1 1 ${EX.toFixed(2)} ${EY.toFixed(2)}`

function Tick({ p, kind }: { p: number; kind: 'warn' | 'danger' | 'zero' }) {
  const [x1, y1] = at(p, 54)
  const [x2, y2] = at(p, 60)
  return <line className={`live-rg-tick ${kind}`} x1={x1} y1={y1} x2={x2} y2={y2} />
}

// 채움은 0(범위에 0 이 있으면) 또는 최소에서 지금 값까지. 바깥 눈금 = 주의(노랑) · 한도(빨강)
export function RoundGauge({ t, s, st }: { t: LiveTile; s: LiveScale; st: LiveStatus }) {
  const base = s.min < 0 && s.max > 0 ? gaugePos(0, s) : 0
  const p = t.value === null ? null : gaugePos(t.value, s)
  const a = p === null ? 0 : Math.min(base, p)
  const b = p === null ? 0 : Math.max(base, p)
  const text = tileValue(t)
  const fs = text.length <= 3 ? 28 : text.length === 4 ? 26 : text.length === 5 ? 23 : 20
  const fmt = (x: number) => formatValue(x, Number.isInteger(x) ? 0 : 1)
  const label = [t.label, t.value === null ? '값 없음' : `${text} ${t.unit}`, `범위 ${fmt(s.min)} ~ ${fmt(s.max)}`,
    s.warn !== null ? `주의 ${fmt(s.warn)}` : '', s.danger !== null ? `한도 ${fmt(s.danger)}` : ''].filter(Boolean).join(', ')
  return (
    <svg className={`live-rg ${st}`} viewBox="0 0 120 114" role="img" aria-label={label}>
      <path className="live-rg-track" d={TRACK} />
      {p !== null && b - a > 0.001 && (
        <circle className="live-rg-fill" cx={CX} cy={CY} r={R} strokeDasharray={`${((b - a) * A).toFixed(2)} ${C.toFixed(2)}`}
          strokeDashoffset={(-a * A).toFixed(2)} transform={`rotate(135 ${CX} ${CY})`} />
      )}
      {base > 0 && <Tick p={base} kind="zero" />}
      {s.warn !== null && <Tick p={gaugePos(s.warn, s)} kind="warn" />}
      {s.danger !== null && <Tick p={gaugePos(s.danger, s)} kind="danger" />}
      <text className="live-rg-value" x={CX} y={68} style={{ fontSize: fs }}>{text}</text>
      <text className="live-rg-unit" x={CX} y={84}>{t.value === null ? '' : t.unit}</text>
      <text className="live-rg-end" x={24} y={111}>{fmt(s.min)}</text>
      <text className="live-rg-end" x={96} y={111}>{fmt(s.max)}</text>
    </svg>
  )
}

// 계기판 칸 — 이름 · 상태 낱말 · 연결 불, 원형 게이지(범위가 없으면 숫자만), 지난 초, 아래 덧붙임(작은 그래프)
export function GaugeTile({ t, size, frozen, lamp, children }: {
  t: LiveTile
  size: 'big' | 'mid'
  frozen: boolean
  lamp: { tone: LampTone; text: string }
  children?: ReactNode
}) {
  const st = shownStatus(t.status, frozen)
  const word = STATUS_WORD[st]
  const scale = scaleOf(t)
  return (
    <div className={`live-gtile ${size} ${st}`} title={[t.id, t.sign && `부호: ${t.sign}`].filter(Boolean).join(' · ')}>
      <div className="live-gtile-label">
        <span className="name">{t.label}</span>
        {word && <span className="live-tile-word">{word}</span>}
        <Lamp tone={lamp.tone} text={lamp.text} />
      </div>
      {scale ? (
        <RoundGauge t={t} s={scale} st={st} />
      ) : (
        <div className="live-gtile-num">
          {tileValue(t)}
          {t.value !== null && <span className="live-tile-unit">{t.unit}</span>}
        </div>
      )}
      <TileAge st={st} age={t.age_s} frozen={frozen} />
      {children}
    </div>
  )
}

// ---------------------------------------------------------------- 페달 (2026-11-13 사용자 결정)
// 원을 반으로 — 왼쪽 반원 = 브레이크(빨강), 오른쪽 반원 = 가속(초록). 둘 다 아래에서 위로 찬다

const GAP = 9 // 위 · 아래 틈 (도)
const pt = (deg: number): string => {
  const a = (deg * Math.PI) / 180
  return `${(CX + R * Math.cos(a)).toFixed(2)} ${(CY + R * Math.sin(a)).toFixed(2)}`
}
const HALF = {
  right: `M ${pt(90 - GAP)} A ${R} ${R} 0 0 0 ${pt(-90 + GAP)}`, // 아래 → 오른쪽 → 위
  left: `M ${pt(90 + GAP)} A ${R} ${R} 0 0 1 ${pt(270 - GAP)}`, // 아래 → 왼쪽 → 위
}

function Half({ side, t, s, st }: { side: 'left' | 'right'; t: LiveTile; s: LiveScale | null; st: LiveStatus }) {
  const p = t.value === null || !s ? null : gaugePos(t.value, s)
  return (
    <g className={`live-pg-side ${side === 'left' ? 'brake' : 'accel'} ${st}`}>
      <path className="live-rg-track" d={HALF[side]} />
      {p !== null && p > 0.001 && <path className="live-rg-fill" d={HALF[side]} pathLength={1} strokeDasharray={`${p.toFixed(4)} 1`} />}
    </g>
  )
}

// 가운데 — 이름(반원 색) 위, 값 아래
function PedalLine({ y, name, side, t, st }: { y: number; name: string; side: 'accel' | 'brake'; t: LiveTile; st: LiveStatus }) {
  return (
    <g className={`live-pg-line ${side} ${st}`}>
      <text className="live-pg-name" x={CX} y={y - 15}>{name}</text>
      <text x={CX} y={y}>
        <tspan className="live-pg-value">{tileValue(t)}</tspan>
        {t.value !== null && <tspan className="live-pg-unit" dx={2}>{t.unit}</tspan>}
      </text>
    </g>
  )
}

// 페달 칸 — 이름 「페달」, 불은 두 원천 중 나쁜 쪽, 상태 낱말 · 지난 초는 가속 기준 (APPS 고장이 여기로 온다)
export function PedalTile({ accel, brake, frozen, lamp }: {
  accel: LiveTile
  brake: LiveTile
  frozen: boolean
  lamp: { tone: LampTone; text: string }
}) {
  const sa = shownStatus(accel.status, frozen)
  const sb = shownStatus(brake.status, frozen)
  const word = STATUS_WORD[sa]
  const sca = scaleOf(accel)
  const scb = scaleOf(brake)
  const fmt = (t: LiveTile) => (t.value === null ? '값 없음' : `${tileValue(t)} ${t.unit}`)
  return (
    <div className={`live-gtile mid ${sa}`} title={`${accel.id} · ${brake.id}`}>
      <div className="live-gtile-label">
        <span className="name">페달</span>
        {word && <span className="live-tile-word">{word}</span>}
        <Lamp tone={lamp.tone} text={lamp.text} />
      </div>
      <svg className="live-rg live-pg" viewBox="0 0 120 114" role="img" aria-label={`페달, 가속 ${fmt(accel)}, 브레이크 ${fmt(brake)}`}>
        <Half side="left" t={brake} s={scb} st={sb} />
        <Half side="right" t={accel} s={sca} st={sa} />
        <PedalLine y={54} name="가속" side="accel" t={accel} st={sa} />
        <PedalLine y={88} name="브레이크" side="brake" t={brake} st={sb} />
      </svg>
      <TileAge st={sa} age={accel.age_s} frozen={frozen} />
    </div>
  )
}

// ---------------------------------------------------------------- 게이지 아래 작은 그래프

export interface Axis {
  lo: number
  hi: number
  digits: number
}

const W = 200
const H = 60
const yOf = (v: number, ax: Axis) => H - 2 - ((v - ax.lo) / (ax.hi - ax.lo || 1)) * (H - 4)

// 틀 — 왼쪽 세로 눈금(위 · 가운데 · 아래, 맨 위에 단위), 그림, 아래 가로 눈금, 범례
function MiniFrame({ ax, unit, xLabels, legend, label, right, children }: {
  ax: Axis
  unit: string
  xLabels: string[]
  legend?: { color: string; text: string; dashed?: boolean }[]
  label: string
  right?: { ax: Axis; unit: string; color: string } // 오른쪽 세로 눈금 (단위가 다른 선 하나)
  children: ReactNode
}) {
  const f = (v: number) => formatValue(v, ax.digits)
  const fr = (v: number) => formatValue(v, right?.ax.digits ?? 0)
  return (
    <div className={`live-mini${right ? ' r2' : ''}`}>
      <div className="live-mini-y" aria-hidden="true">
        <span>
          {f(ax.hi)}
          {unit && ` ${unit}`}
        </span>
        <span>{f((ax.lo + ax.hi) / 2)}</span>
        <span>{f(ax.lo)}</span>
      </div>
      <svg className="live-mini-plot" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={label}>
        <line className="live-mini-grid" x1={0} y1={H / 2} x2={W} y2={H / 2} />
        {children}
      </svg>
      {right && (
        <div className="live-mini-y right" aria-hidden="true" style={{ color: right.color }}>
          <span>
            {fr(right.ax.hi)}
            {right.unit && ` ${right.unit}`}
          </span>
          <span>{fr((right.ax.lo + right.ax.hi) / 2)}</span>
          <span>{fr(right.ax.lo)}</span>
        </div>
      )}
      <div className="live-mini-x" aria-hidden="true">
        {xLabels.map((x, i) => (
          <span key={i}>{x}</span>
        ))}
      </div>
      {legend && legend.length > 0 && (
        <div className="live-mini-legend">
          {legend.map((l) => (
            <span key={l.text}>
              <i className={l.dashed ? 'dashed' : ''} style={{ borderColor: l.color }} aria-hidden="true" />
              {l.text}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function LimitLines({ limits, ax }: { limits: Limit[]; ax: Axis }) {
  return (
    <>
      {limits.map((l) => (
        <line key={`${l.kind}${l.v}`} className={`live-mini-limit ${l.kind}`} x1={0} x2={W} y1={yOf(l.v, ax)} y2={yOf(l.v, ax)} />
      ))}
    </>
  )
}

// 시간축 — 최근 span 초. GAP_S 넘게 빈 곳만 끊는다 (짧은 빈 칸은 잇는다)
export function TimeMini({ buf, now, span, lines, ax, unit, limits, legend, label, right }: {
  buf: ChartBuf
  now: number
  span: number
  lines: { id: string; color: string }[]
  ax: Axis
  unit: string
  limits: Limit[]
  legend?: { color: string; text: string }[]
  label: string
  right?: { id: string; color: string; ax: Axis; unit: string } // 오른쪽 축에 그리는 선 (단위가 다를 때)
}) {
  const from = now - span
  const runs = (id: string, a: Axis = ax): string[] => {
    const vs = bridgeShort(buf.t, buf.series[id] ?? buf.t.map(() => null))
    const out: string[] = []
    let cur: string[] = []
    buf.t.forEach((t, i) => {
      const v = vs[i]
      if (t < from || v === undefined) return
      if (v === null) {
        if (cur.length > 1) out.push(cur.join(' '))
        cur = []
      } else cur.push(`${(((t - from) / span) * W).toFixed(1)},${yOf(v, a).toFixed(1)}`)
    })
    if (cur.length > 1) out.push(cur.join(' '))
    return out
  }
  const half = (x: number) => (Number.isInteger(x) ? String(x) : x.toFixed(1))
  const xl = span >= 120 ? [`−${half(span / 60)}분`, `−${half(span / 120)}`, '지금'] : [`−${span} s`, `−${half(span / 2)}`, '지금']
  return (
    <MiniFrame ax={ax} unit={unit} xLabels={xl} legend={legend} label={label} right={right && { ax: right.ax, unit: right.unit, color: right.color }}>
      <LimitLines limits={limits} ax={ax} />
      {lines.map((l) => runs(l.id).map((r, i) => <polyline key={`${l.id}${i}`} className="live-mini-line" points={r} style={{ stroke: l.color }} />))}
      {right && runs(right.id, right.ax).map((r, i) => <polyline key={`r${i}`} className="live-mini-line" points={r} style={{ stroke: right.color }} />)}
    </MiniFrame>
  )
}

// 랩 꼭짓점 — 끝난 랩은 점 · 선, 이번 랩은 점선 + 상태 색 점. 가로 = 랩 순서
export function LapMini({ pts, ax, unit, limits, color, curColor, label }: {
  pts: LapPoints
  ax: Axis
  unit: string
  limits: Limit[]
  color: string
  curColor: string
  label: string
}) {
  const n = pts.done.length + (pts.cur ? 1 : 0)
  const xOf = (i: number) => ((i + 0.5) / Math.max(n, 1)) * W
  const done = pts.done.map((d, i) => [xOf(i), yOf(d.v, ax)] as const)
  const cur = pts.cur ? ([xOf(pts.done.length), yOf(pts.cur.v, ax)] as const) : null
  const last = done.length ? done[done.length - 1] : null
  const laps = [...pts.done.map((d) => d.lap), ...(pts.cur ? [pts.cur.lap] : [])]
  const xl = laps.length ? (laps.length > 1 ? [`랩 ${laps[0]}`, `${laps[laps.length - 1]}`] : [`랩 ${laps[0]}`]) : ['랩 —']
  return (
    <MiniFrame ax={ax} unit={unit} xLabels={xl} label={label}>
      <LimitLines limits={limits} ax={ax} />
      {done.length > 1 && <polyline className="live-mini-line" points={done.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ')} style={{ stroke: color }} />}
      {last && cur && <polyline className="live-mini-line dashed" points={`${last[0]},${last[1]} ${cur[0]},${cur[1]}`} style={{ stroke: color }} />}
      {done.length > 0 && <path className="live-mini-dot" d={done.map(([x, y]) => `M${x.toFixed(1)} ${y.toFixed(1)}h0`).join('')} style={{ stroke: color }} />}
      {cur && <path className="live-mini-dot" d={`M${cur[0].toFixed(1)} ${cur[1].toFixed(1)}h0`} style={{ stroke: curColor }} />}
    </MiniFrame>
  )
}
