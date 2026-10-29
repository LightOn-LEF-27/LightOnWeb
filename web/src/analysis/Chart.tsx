// 그래프 축 하나 (uPlot). 같은 화면의 축들은 커서를 같이 움직이고(sync), 한 축을 확대하면 다른 축도 같이 확대된다(ZoomGroup).
// 화면이 하는 것은 그리기 · 확대 · 이동 · 색뿐 — 값은 엔진이 준 그대로 (최소 · 최대는 옅은 띠로)

import 'uplot/dist/uPlot.min.css'

import { useEffect, useRef } from 'react'
import uPlot from 'uplot'

import { formatValue } from '../overview/model'
import type { Channel } from '../types'
import type { AxisData } from './traces'

export interface ZoomGroup {
  charts: Set<uPlot>
  syncing: boolean
  range: { min: number; max: number } | null // null = 처음 범위
  onCursorX?: (x: number | null) => void
}

export const newZoomGroup = (): ZoomGroup => ({ charts: new Set(), syncing: false, range: null })

const SYNC_KEY = 'lefsite-analysis'

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

function alpha(hex: string, a: number): string {
  const m = hex.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i)
  return m ? `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${a})` : hex
}

function extent(u: uPlot): [number, number] {
  const xs = u.data[0]
  return [xs[0] as number, xs[xs.length - 1] as number]
}

// 확대 초기화 — 모든 축을 처음 범위로
export function resetZoom(group: ZoomGroup): void {
  group.range = null
  group.syncing = true
  group.charts.forEach((u) => {
    const [min, max] = extent(u)
    if (Number.isFinite(min) && Number.isFinite(max)) u.setScale('x', { min, max })
  })
  group.syncing = false
}

interface Props {
  channels: Channel[] // 1 ~ 2 (둘째 = 오른쪽 세로축)
  axis: AxisData
  colors: string[] // 계열 색 (기준 · 비교 1 · 비교 2)
  width: number
  height: number
  showX: boolean
  shade: [number, number][]
  group: ZoomGroup
  theme: string
  readout: HTMLElement | null // 마우스 위치의 값을 쓸 곳 (축 머리)
}

export default function Chart({ channels, axis, colors, width, height, showX, shade, group, theme, readout }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const plot = useRef<uPlot | null>(null)

  useEffect(() => {
    if (!box.current || axis.meta.length === 0) return
    const text = cssVar('--text-muted')
    const grid = cssVar('--line-faint')
    const warn = alpha(cssVar('--warn') || '#d4b13c', 0.16)
    const series: uPlot.Series[] = [{}]
    const bands: uPlot.Band[] = []
    axis.meta.forEach((m, i) => {
      const color = colors[m.trace.slot]
      const scale = m.axis ? 'y2' : 'y'
      if (m.kind === 'v') {
        const stepped = channels[m.axis]?.levels !== undefined
        series.push({ scale, stroke: color, width: 1.4, dash: m.axis ? [5, 4] : undefined, spanGaps: false, points: { show: false },
                      paths: stepped ? uPlot.paths.stepped!({ align: 1 }) : undefined })
        bands.push({ series: [i + 3, i + 2], fill: alpha(color, 0.13) }) // 최대 ↔ 최소 (값 다음 두 칸)
      } else {
        series.push({ scale, stroke: 'transparent', width: 0, spanGaps: false, points: { show: false } })
      }
    })
    let ready = false
    const opts: uPlot.Options = {
      width,
      height,
      legend: { show: false },
      series,
      bands,
      scales: { x: { time: false }, y: { auto: true }, y2: { auto: true } },
      cursor: { sync: { key: SYNC_KEY }, drag: { x: true, y: false, setScale: true }, points: { show: false }, y: false },
      axes: [
        { show: showX, stroke: text, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, size: 24, font: '10px Pretendard Variable, sans-serif' },
        { scale: 'y', stroke: text, size: 54, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, font: '10px Pretendard Variable, sans-serif' },
        // 둘째 채널이 없어도 오른쪽 자리를 비워 둔다 — 축마다 그림 영역의 왼쪽 · 오른쪽 끝이 맞게
        { scale: 'y2', side: 1, stroke: text, size: 48, grid: { show: false }, ticks: { show: channels.length > 1, stroke: grid },
          values: channels.length > 1 ? undefined : () => [], font: '10px Pretendard Variable, sans-serif' },
      ],
      hooks: {
        drawClear: [
          (u) => {
            if (!shade.length) return
            const { ctx, bbox } = u
            ctx.save()
            ctx.fillStyle = warn
            for (const [a, b] of shade) {
              const x0 = Math.max(bbox.left, u.valToPos(a, 'x', true))
              const x1 = Math.min(bbox.left + bbox.width, u.valToPos(b, 'x', true))
              if (x1 > x0) ctx.fillRect(x0, bbox.top, x1 - x0, bbox.height)
            }
            ctx.restore()
          },
        ],
        setScale: [
          (u, key) => {
            if (key !== 'x' || !ready || group.syncing) return
            const { min, max } = u.scales.x
            if (min == null || max == null) return
            const [lo, hi] = extent(u)
            group.range = min <= lo && max >= hi ? null : { min, max }
            group.syncing = true
            group.charts.forEach((o) => o !== u && o.setScale('x', { min, max }))
            group.syncing = false
          },
        ],
        setCursor: [
          (u) => {
            const idx = u.cursor.idx
            group.onCursorX?.(idx == null ? null : (u.data[0][idx] as number))
            if (!readout) return
            const spans = readout.querySelectorAll<HTMLElement>('[data-s]')
            spans.forEach((el) => {
              const s = Number(el.dataset.s)
              const i = u.cursor.idxs?.[s]
              const v = i == null ? null : (u.data[s][i] as number | null | undefined)
              el.textContent = v == null ? '—' : formatValue(v, Number(el.dataset.d))
            })
          },
        ],
      },
    }
    const u = new uPlot(opts, axis.data, box.current)
    plot.current = u
    group.charts.add(u)
    ready = true
    if (group.range) u.setScale('x', group.range)
    return () => {
      group.charts.delete(u)
      u.destroy()
      plot.current = null
    }
  }, [axis, channels, colors, showX, shade, group, theme, readout]) // 크기만 바뀌면 다시 만들지 않는다 (아래 setSize)

  useEffect(() => {
    plot.current?.setSize({ width, height })
  }, [width, height])

  return <div ref={box} className="chart-plot" />
}
