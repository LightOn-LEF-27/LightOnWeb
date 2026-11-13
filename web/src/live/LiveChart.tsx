// 실시간 그래프 한 칸 — 최근 N분을 서버 시계(한국 시각)로. 낡은 값은 엔진이 null 로 주므로 선이 끊긴다.
// 엔진이 그래프 값에 아직 싣지 않는 채널이면 "엔진 값 준비 중" (화면이 값을 지어내지 않는다).
// 그래프 뷰(§6-1)에서는: 칸끼리 커서를 맞추고(sync), 수신이 끊긴 띠 · 한도 · 주의 점선을 그리고, 랩 값이 있으면 랩마다 꼭짓점(끝난 랩 = 점 · 선, 이번 랩 = 점선)
import 'uplot/dist/uPlot.min.css'

import { type ReactNode, useEffect, useRef, useState } from 'react'
import uPlot from 'uplot'

import { formatValue } from '../overview/model'
import type { LiveTile } from '../types'
import { bridgeShort, type ChartBuf, clock, type LapPoints, type PanelSeries, valueAt, WINDOW_S } from './model'

export interface Limit {
  v: number
  kind: 'warn' | 'danger'
}

interface Props {
  title: string
  series: PanelSeries[]
  buf: ChartBuf
  now: number
  tiles: Map<string, LiveTile> // 이름 · 단위는 엔진이 준 타일 것을 쓴다
  theme: string
  height: number
  pct?: boolean // 0 ~ 1 값을 0 ~ 100 % 고정 눈금으로 (수신률)
  keys?: boolean // 머리줄에 계열 이름을 보일지 (기본 보임)
  extra?: ReactNode // 머리줄 오른쪽
  windowS?: number // 보이는 창 (기본 5분)
  sync?: string // 같은 값끼리 커서를 맞춘다
  bands?: [number, number][] // 수신이 끊긴 구간 — 어두운 띠
  limits?: Limit[] // 왼쪽 축 값의 한도 · 주의 — 점선, 세로 범위에 넣는다
  laps?: Record<string, LapPoints> | null // 시리즈 id → 랩 꼭짓점. 주면 시간축 선 대신 랩마다 꼭짓점
  xAxis?: boolean // 시각 눈금 숫자 (기본 보임)
  side?: boolean // 왼쪽에 이름 · 지금 값 칸 (그래프 뷰)
  cursor?: number | null // 커서 시각 — 왼쪽 값이 그 시각 값
  onCursor?: (t: number | null) => void
}

const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()
const FONT = '10px Pretendard Variable, sans-serif'

export default function LiveChart(p: Props) {
  const { title, series, buf, now, tiles, theme, height, pct = false, keys = true, extra, windowS = WINDOW_S, sync, xAxis = true, side = false } = p
  const box = useRef<HTMLDivElement>(null)
  const plot = useRef<uPlot | null>(null)
  const [width, setWidth] = useState(0)
  const latest = useRef(p)
  latest.current = p
  const lapMode = !!p.laps

  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(() => setWidth(Math.floor(el.clientWidth)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const missing = !lapMode && buf.t.length > 0 && series.every((s) => !(s.id in buf.series))

  // 축 · 색 · 모양이 바뀔 때만 새로 만든다 — 값 · 띠 · 한도는 latest 로 읽는다
  useEffect(() => {
    if (!box.current || width < 50) return
    const text = cssVar('--text-muted')
    const grid = cssVar('--line-faint')
    const right = series.some((s) => s.right)
    const band = cssVar('--chart-gap')
    const warn = cssVar('--warn')
    const danger = cssVar('--danger')
    const lapLine = cssVar('--line-soft')
    const draws: uPlot.Series[] = []
    for (const s of series) {
      const stroke = cssVar(`--trace-${s.color}`)
      const scale = s.right ? 'y2' : 'y'
      if (lapMode) {
        draws.push({ scale, stroke, width: 1.6, spanGaps: true, points: { show: true, size: 6, fill: stroke, stroke } })
        draws.push({ scale, stroke, width: 1.4, dash: [3, 4], spanGaps: false, points: { show: true, size: 6, fill: cssVar('--panel'), stroke } })
      } else {
        draws.push({ scale, stroke, width: 1.4, dash: s.right ? [5, 4] : undefined, spanGaps: false, points: { show: false } })
      }
    }
    // 세로 범위에 한도 · 주의 선을 넣는다
    const yRange: uPlot.Range.Function = (_u, mn, mx) => {
      const lim = (latest.current.limits ?? []).map((l) => l.v)
      const xs = [mn, mx, ...lim].filter((v): v is number => v != null && Number.isFinite(v))
      if (!xs.length) return [0, 1]
      return uPlot.rangeNum(Math.min(...xs), Math.max(...xs), 0.1, true)
    }
    const u = new uPlot(
      {
        width,
        height,
        legend: { show: false },
        cursor: {
          y: false,
          points: { show: false },
          drag: { x: false, y: false },
          ...(sync ? { sync: { key: sync } } : {}),
        },
        tzDate: (ts) => uPlot.tzDate(new Date(ts * 1e3), 'Asia/Seoul'),
        scales: { x: { time: true }, y: pct ? { auto: false, range: [0, 1.05] } : { auto: true, range: yRange }, y2: { auto: true } },
        series: [{}, ...draws],
        axes: [
          {
            stroke: text, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, size: xAxis ? 22 : 4, font: FONT, space: 70,
            values: (_u, ts) => ts.map((v) => (xAxis ? clock(v) : '')),
          },
          {
            scale: 'y', stroke: text, size: 44, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, font: FONT,
            ...(pct ? { splits: () => [0, 0.5, 1], values: (_u: uPlot, vs: number[]) => vs.map((v) => `${Math.round(v * 100)} %`) } : {}),
          },
          { scale: 'y2', side: 1, show: right, stroke: text, size: right ? 44 : 0, grid: { show: false }, ticks: { stroke: grid }, font: FONT },
        ],
        hooks: {
          // 끊긴 띠 · 랩 끝 세로선 — 선보다 먼저
          drawAxes: [
            (uu) => {
              const { ctx, bbox } = uu
              ctx.save()
              ctx.fillStyle = band
              for (const [a, b] of latest.current.bands ?? []) {
                const x0 = Math.max(bbox.left, uu.valToPos(a, 'x', true))
                const x1 = Math.min(bbox.left + bbox.width, uu.valToPos(b, 'x', true))
                if (x1 > x0) ctx.fillRect(x0, bbox.top, Math.max(1, x1 - x0), bbox.height)
              }
              const laps = latest.current.laps
              if (laps) {
                const ends = new Set<number>()
                for (const lp of Object.values(laps)) for (const d of lp.done) ends.add(d.t)
                ctx.strokeStyle = lapLine
                ctx.lineWidth = 1
                for (const t of ends) {
                  const x = Math.round(uu.valToPos(t, 'x', true)) + 0.5
                  if (x < bbox.left || x > bbox.left + bbox.width) continue
                  ctx.beginPath()
                  ctx.moveTo(x, bbox.top)
                  ctx.lineTo(x, bbox.top + bbox.height)
                  ctx.stroke()
                }
              }
              ctx.restore()
            },
          ],
          // 한도 · 주의 점선 — 선 위에
          draw: [
            (uu) => {
              const lims = latest.current.limits ?? []
              if (!lims.length) return
              const { ctx, bbox } = uu
              ctx.save()
              ctx.setLineDash([5, 4])
              ctx.lineWidth = 1.6 * (window.devicePixelRatio || 1)
              for (const l of lims) {
                const y = Math.round(uu.valToPos(l.v, 'y', true)) + 0.5
                if (y < bbox.top || y > bbox.top + bbox.height) continue
                ctx.strokeStyle = l.kind === 'danger' ? danger : warn
                ctx.beginPath()
                ctx.moveTo(bbox.left, y)
                ctx.lineTo(bbox.left + bbox.width, y)
                ctx.stroke()
              }
              ctx.restore()
            },
          ],
          setCursor: [
            (uu) => {
              const cb = latest.current.onCursor
              if (!cb) return
              const left = uu.cursor.left
              cb(left == null || left < 0 ? null : uu.posToVal(left, 'x'))
            },
          ],
        },
      },
      chartData(latest.current),
      box.current,
    )
    u.setScale('x', { min: latest.current.now - windowS, max: latest.current.now })
    plot.current = u
    return () => {
      u.destroy()
      plot.current = null
    }
  }, [width, height, theme, series, pct, lapMode, sync, xAxis, windowS])

  // 0.5 s 마다 값만 — 오른쪽 끝은 늘 서버의 지금
  useEffect(() => {
    const u = plot.current
    if (!u) return
    u.setData(chartData(latest.current), false)
    u.setScale('x', { min: now - windowS, max: now })
  }, [buf, now, windowS, p.laps, p.bands, p.limits])

  const head = (
    <div className="live-chart-head">
      <span className="live-chart-title">{title}</span>
      {keys &&
        series.length > 1 &&
        series.map((s) => {
          const t = tiles.get(s.id)
          return (
            <span key={s.id} className="live-chart-key">
              <i className={s.right ? 'dashed' : ''} style={{ borderColor: `var(--trace-${s.color})` }} aria-hidden="true" />
              {s.label ?? t?.label ?? s.id} <span className="muted">{t?.unit}</span>
            </span>
          )
        })}
      {keys && series.length === 1 && <span className="muted">{tiles.get(series[0].id)?.unit}</span>}
      {extra && <span className="live-chart-extra">{extra}</span>}
    </div>
  )
  const plotBox = (
    <div className="live-chart-box" style={{ height }}>
      <div ref={box} className="live-chart-plot" style={{ height }} />
      {missing && <div className="live-chart-missing muted">엔진 값 준비 중</div>}
    </div>
  )
  if (!side)
    return (
      <div className="live-chart">
        {head}
        {plotBox}
      </div>
    )
  // 그래프 뷰 — 왼쪽에 이름 · 값 (커서가 있으면 그 시각 값, 없으면 지금 값)
  return (
    <div className="live-chart lc-side">
      <div className="live-chart-side">
        <div className="live-chart-title">{title}</div>
        {series.map((s) => {
          const t = tiles.get(s.id)
          const v = p.cursor != null ? cursorValue(p, s.id, p.cursor) : t?.value ?? null
          return (
            <div key={s.id} className="live-chart-val">
              <i className={s.right && !lapMode ? 'dashed' : ''} style={{ borderColor: `var(--trace-${s.color})` }} aria-hidden="true" />
              <span className="name">{s.label ?? t?.label ?? s.id}</span>
              <strong>{v == null ? '—' : formatValue(v, t?.digits ?? 1)}</strong>
              <span className="unit">{t?.unit}</span>
            </div>
          )
        })}
        {lapMode && <div className="live-chart-note muted">랩별 최고</div>}
      </div>
      {plotBox}
    </div>
  )
}

// 커서 시각의 값 — 시간축이면 가장 가까운 줄, 랩 꼭짓점이면 그 시각 뒤에 처음 끝난 랩 (없으면 이번 랩)
function cursorValue(p: Props, id: string, t: number): number | null {
  if (!p.laps) return valueAt(p.buf, id, t)
  const lp = p.laps[id]
  if (!lp) return null
  return lp.done.find((d) => d.t >= t)?.v ?? lp.cur?.v ?? null
}

// uPlot 에 줄 값 — 시간축이면 받은 그래프 값 (GAP_S 보다 짧은 빈 칸은 잇는다). 랩 꼭짓점이면 x = 끝난 랩 시각들 + 지금
function chartData(p: Props): uPlot.AlignedData {
  const { buf, series } = p
  if (!p.laps) return [buf.t, ...series.map((s) => bridgeShort(buf.t, buf.series[s.id] ?? buf.t.map(() => null)))]
  const laps = p.laps
  const ends = [...new Set(series.flatMap((s) => laps[s.id]?.done.map((d) => d.t) ?? []))].sort((a, b) => a - b)
  const xs = [...ends, p.now]
  const out: (number | null)[][] = []
  for (const s of series) {
    const lp = laps[s.id]
    const at = new Map((lp?.done ?? []).map((d) => [d.t, d.v]))
    const done = xs.map((x, i) => (i < ends.length ? at.get(x) ?? null : null))
    const last = lp?.done.length ? lp.done[lp.done.length - 1] : null
    const cur = xs.map((x, i) => (i === xs.length - 1 ? lp?.cur?.v ?? null : last && x === last.t ? last.v : null))
    out.push(done, cur)
  }
  return [xs, ...out]
}
