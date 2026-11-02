// 실시간 그래프 한 칸 — 최근 5분을 서버 시계(한국 시각)로. 낡은 값은 엔진이 null 로 주므로 선이 끊긴다
import 'uplot/dist/uPlot.min.css'

import { useEffect, useRef, useState } from 'react'
import uPlot from 'uplot'

import type { LiveTile } from '../types'
import { type ChartBuf, clock, type PanelSeries, WINDOW_S } from './model'

interface Props {
  title: string
  series: PanelSeries[]
  buf: ChartBuf
  now: number
  tiles: Map<string, LiveTile> // 이름 · 단위는 엔진이 준 타일 것을 쓴다
  theme: string
  height: number
}

const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()
const FONT = '10px Pretendard Variable, sans-serif'

export default function LiveChart({ title, series, buf, now, tiles, theme, height }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const plot = useRef<uPlot | null>(null)
  const [width, setWidth] = useState(0)
  const latest = useRef({ buf, now })
  latest.current = { buf, now }

  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(() => setWidth(Math.floor(el.clientWidth)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const data = (b: ChartBuf): uPlot.AlignedData => [b.t, ...series.map((s) => b.series[s.id] ?? b.t.map(() => null))]

  // 축 · 색이 바뀔 때만 새로 만든다 — 값은 아래에서 바꿔 끼운다
  useEffect(() => {
    if (!box.current || width < 50) return
    const text = cssVar('--text-muted')
    const grid = cssVar('--line-faint')
    const right = series.some((s) => s.right)
    const u = new uPlot(
      {
        width,
        height,
        legend: { show: false },
        cursor: { y: false, points: { show: false }, drag: { x: false, y: false } },
        tzDate: (ts) => uPlot.tzDate(new Date(ts * 1e3), 'Asia/Seoul'),
        scales: { x: { time: true }, y: { auto: true }, y2: { auto: true } },
        series: [
          {},
          ...series.map((s) => ({ scale: s.right ? 'y2' : 'y', stroke: cssVar(`--trace-${s.color}`), width: 1.4, dash: s.right ? [5, 4] : undefined, spanGaps: false, points: { show: false } })),
        ],
        axes: [
          { stroke: text, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, size: 22, font: FONT, space: 70, values: (_u, ts) => ts.map((v) => clock(v)) },
          { scale: 'y', stroke: text, size: 44, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, font: FONT },
          { scale: 'y2', side: 1, show: right, stroke: text, size: right ? 44 : 0, grid: { show: false }, ticks: { stroke: grid }, font: FONT },
        ],
      },
      data(latest.current.buf),
      box.current,
    )
    u.setScale('x', { min: latest.current.now - WINDOW_S, max: latest.current.now })
    plot.current = u
    return () => {
      u.destroy()
      plot.current = null
    }
  }, [width, height, theme, series]) // 값(buf · now)은 latest 로 읽는다 — 값마다 다시 만들지 않게

  // 0.5 s 마다 값만 — 오른쪽 끝은 늘 서버의 지금
  useEffect(() => {
    const u = plot.current
    if (!u) return
    u.setData(data(buf), false)
    u.setScale('x', { min: now - WINDOW_S, max: now })
  }, [buf, now])

  return (
    <div className="live-chart">
      <div className="live-chart-head">
        <span className="live-chart-title">{title}</span>
        {series.map((s) => {
          const t = tiles.get(s.id)
          return (
            <span key={s.id} className="live-chart-key">
              <i className={s.right ? 'dashed' : ''} style={{ borderColor: `var(--trace-${s.color})` }} aria-hidden="true" />
              {t?.label ?? s.id} <span className="muted">{t?.unit}</span>
            </span>
          )
        })}
      </div>
      <div ref={box} className="live-chart-plot" style={{ height }} />
    </div>
  )
}
