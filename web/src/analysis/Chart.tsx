// 그래프 축 하나 (uPlot). 같은 화면의 축들은 커서를 같이 움직이고(sync), 한 축을 확대하면 다른 축도 같이 확대된다(ZoomGroup).
// 화면이 하는 것은 그리기 · 확대 · 이동 · 색뿐 — 값은 엔진이 준 그대로 (최소 · 최대는 옅은 띠로)

import 'uplot/dist/uPlot.min.css'

import { useEffect, useRef } from 'react'
import uPlot from 'uplot'

import { formatValue } from '../overview/model'
import type { Channel } from '../types'
import { centerZero } from './lookup'
import type { AxisData } from './traces'

export interface ZoomGroup {
  charts: Set<uPlot>
  syncing: boolean
  range: { min: number; max: number } | null // null = 처음 범위
  full: { min: number; max: number } | null // 처음 범위 (확대해서 다시 받은 값은 좁아서 따로 기억한다)
  onRange?: (r: { min: number; max: number } | null) => void // 확대 범위가 바뀌면 — 화면이 그 범위만 다시 받는다
  listeners: Set<() => void> // 범위가 바뀌면 다시 그릴 것 (아래 이동 막대)
  hover: Set<(x: number | null) => void> // 마우스 위치를 받는 곳 (x 읽기 · 트랙맵 · g-g) — 렌더 없이 바로 그린다
  cursorA: number | null // 클릭 = A, Shift + 클릭 = B (그래프의 x 단위)
  cursorB: number | null
  onPick?: (x: number, which: 'a' | 'b') => void
}

export const newZoomGroup = (): ZoomGroup => ({ charts: new Set(), syncing: false, range: null, full: null, listeners: new Set(), hover: new Set(),
                                                 cursorA: null, cursorB: null })

// 커서 A · B 가 바뀌면 모든 축을 다시 그린다 (선만 — 값은 그대로)
export function redrawCursors(group: ZoomGroup): void {
  group.charts.forEach((u) => u.redraw(false, false))
}

const SYNC_KEY = 'lefsite-analysis'
const Y2_SIZE = 48 // 오른쪽 세로축 폭

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

function alpha(hex: string, a: number): string {
  const m = hex.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i)
  return m ? `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${a})` : hex
}

function extent(u: uPlot, group: ZoomGroup): [number, number] {
  if (group.full) return [group.full.min, group.full.max]
  const xs = u.data[0]
  return [xs[0] as number, xs[xs.length - 1] as number]
}

// 확대 초기화 — 모든 축을 처음 범위로 (좁게 다시 받은 값이면 처음 값을 다시 받는다)
export function resetZoom(group: ZoomGroup): void {
  group.range = null
  group.syncing = true
  group.charts.forEach((u) => {
    const [min, max] = extent(u, group)
    if (Number.isFinite(min) && Number.isFinite(max)) u.setScale('x', { min, max })
  })
  group.syncing = false
  group.onRange?.(null)
  group.listeners.forEach((f) => f())
}

// 한 축의 확대를 모든 축 · 화면에 알린다 (처음 범위를 덮으면 확대 없음으로)
function share(u: uPlot | null, group: ZoomGroup, min: number, max: number): void {
  const full = group.full
  group.range = full && min <= full.min && max >= full.max ? null : { min, max }
  group.syncing = true
  group.charts.forEach((o) => o !== u && o.setScale('x', { min, max }))
  group.syncing = false
  group.onRange?.(group.range)
  group.listeners.forEach((f) => f())
}

// 확대하지 않았으면 모든 축을 처음 범위로 — 축마다 자기 값의 끝으로 잡으면 계열이 빠진 축만 범위가 달라진다
export function fitFull(group: ZoomGroup): void {
  const full = group.full
  if (group.range || !full) return
  group.charts.forEach((u) => (u.scales.x.min !== full.min || u.scales.x.max !== full.max) && u.setScale('x', full))
}

// 보는 범위를 정한다 (이동 막대 · 버튼) — 처음 범위 밖으로는 나가지 않게 밀어 넣는다
export function setRange(group: ZoomGroup, min: number, max: number): void {
  const full = group.full
  if (full) {
    const w = Math.min(max - min, full.max - full.min)
    if (min < full.min) (min = full.min), (max = full.min + w)
    if (max > full.max) (max = full.max), (min = full.max - w)
  }
  share(null, group, min, max)
}

// 옆으로 넘기기 — 보는 폭의 frac 만큼 (− 왼쪽 · + 오른쪽)
export function panBy(group: ZoomGroup, frac: number): void {
  const r = group.range
  if (!r) return
  const d = (r.max - r.min) * frac
  setRange(group, r.min + d, r.max + d)
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
  const built = useRef<AxisData | null>(null) // 그래프에 지금 들어 있는 값 — 값만 바뀌면 다시 만들지 않고 바꿔 끼운다
  const latest = useRef({ axis, shade })
  latest.current = { axis, shade }
  // 계열 구성(어느 랩 · 채널 · 축)이 같으면 같은 그래프 — 값만 바뀐 것
  const shape = JSON.stringify(axis.meta.map((m) => [m.trace.key, m.trace.slot, m.channel, m.axis, m.kind]))

  useEffect(() => {
    const axis = latest.current.axis
    if (!box.current || axis.meta.length === 0) return
    const text = cssVar('--text-muted')
    const grid = cssVar('--line-faint')
    const warn = alpha(cssVar('--warn') || '#d4b13c', 0.16)
    const strong = cssVar('--text-strong') || '#ffffff'
    const zero = alpha(cssVar('--text-muted') || '#8e959e', 0.55)
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
    // 마우스 자리의 값 — 그래프 위, 마우스 선 옆에 (모든 축이 같이)
    const tip = document.createElement('div')
    tip.className = 'chart-tip'
    const showTip = (u: uPlot) => {
      const left = u.cursor.left
      if (u.cursor.idx == null || left == null || left < 0) return void (tip.style.display = 'none')
      const meta = (built.current ?? axis).meta
      tip.replaceChildren(
        ...channels.map((c, k) => {
          const row = document.createElement('div')
          if (channels.length > 1) row.append(Object.assign(document.createElement('span'), { className: 'tip-name', textContent: c.label }))
          meta.forEach((m, i) => {
            if (m.kind !== 'v' || m.axis !== k) return
            const j = u.cursor.idxs?.[i + 1]
            const v = j == null ? null : (u.data[i + 1][j] as number | null | undefined)
            const sw = document.createElement('i')
            sw.style.background = colors[m.trace.slot]
            row.append(sw, v == null ? '—' : (c.levels?.find((l) => l.value === v)?.label ?? formatValue(v, c.digits)))
          })
          if (c.unit) row.append(Object.assign(document.createElement('span'), { className: 'tip-unit', textContent: c.unit }))
          return row
        }),
      )
      tip.style.display = 'block'
      const w = tip.offsetWidth
      tip.style.left = `${left + 10 + w > u.over.clientWidth ? left - 10 - w : left + 10}px`
    }
    let ready = false
    const opts: uPlot.Options = {
      width,
      height,
      legend: { show: false },
      series,
      bands,
      scales: { x: { time: false }, y: { auto: true, range: zeroRange(channels[0]) }, y2: { auto: true, range: zeroRange(channels[1]) } },
      cursor: { sync: { key: SYNC_KEY }, drag: { x: true, y: false, setScale: true }, points: { show: false }, y: false },
      // 오른쪽 축은 채널이 없으면 uPlot 이 숨기므로, 그 폭만큼 여백을 직접 준다 — 축마다 그림 영역의 왼쪽 · 오른쪽 끝이 맞게
      // (자동 여백에 맡기면 x 눈금을 그리는 맨 아래 축만 오른쪽이 좁아져 커서 선이 어긋난다)
      padding: [null, channels.length > 1 ? 0 : Y2_SIZE, null, 0],
      axes: [
        { show: showX, stroke: text, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, size: 24, font: '10px Pretendard Variable, sans-serif' },
        { scale: 'y', stroke: text, size: 54, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, font: '10px Pretendard Variable, sans-serif' },
        { scale: 'y2', side: 1, stroke: text, size: Y2_SIZE, grid: { show: false }, ticks: { stroke: grid }, font: '10px Pretendard Variable, sans-serif' },
      ],
      hooks: {
        drawClear: [
          (u) => {
            const { ctx, bbox } = u
            ctx.save()
            ctx.fillStyle = warn
            for (const [a, b] of latest.current.shade) {
              const x0 = Math.max(bbox.left, u.valToPos(a, 'x', true))
              const x1 = Math.min(bbox.left + bbox.width, u.valToPos(b, 'x', true))
              if (x1 > x0) ctx.fillRect(x0, bbox.top, x1 - x0, bbox.height)
            }
            // 0 선 — 값이 + · − 를 오가면 (오른쪽 축은 점선)
            ctx.strokeStyle = zero
            ctx.lineWidth = devicePixelRatio
            for (const key of channels.length > 1 ? ['y', 'y2'] : ['y']) {
              const { min, max } = u.scales[key]
              if (min == null || max == null || !(min < 0 && max > 0)) continue
              const py = Math.round(u.valToPos(0, key, true))
              ctx.setLineDash(key === 'y2' ? [5 * devicePixelRatio, 4 * devicePixelRatio] : [])
              ctx.beginPath()
              ctx.moveTo(bbox.left, py)
              ctx.lineTo(bbox.left + bbox.width, py)
              ctx.stroke()
            }
            ctx.restore()
          },
        ],
        draw: [
          (u) => {
            const { ctx, bbox } = u
            const line = (x: number | null, label: string, dashed: boolean) => {
              if (x == null) return
              const px = u.valToPos(x, 'x', true)
              if (px < bbox.left || px > bbox.left + bbox.width) return
              ctx.save()
              ctx.strokeStyle = dashed ? text : strong
              ctx.lineWidth = 1
              ctx.setLineDash(dashed ? [4, 3] : [])
              ctx.beginPath()
              ctx.moveTo(px, bbox.top)
              ctx.lineTo(px, bbox.top + bbox.height)
              ctx.stroke()
              ctx.fillStyle = dashed ? text : strong
              ctx.font = `${10 * devicePixelRatio}px sans-serif`
              ctx.fillText(label, px + 3 * devicePixelRatio, bbox.top + 10 * devicePixelRatio)
              ctx.restore()
            }
            line(group.cursorB, 'B', true)
            line(group.cursorA, 'A', false)
          },
        ],
        setScale: [
          (u, key) => {
            if (key !== 'x' || !ready || group.syncing) return
            const { min, max } = u.scales.x
            if (min == null || max == null) return
            share(u, group, min, max)
          },
        ],
        setCursor: [
          (u) => {
            const idx = u.cursor.idx
            const hx = idx == null || u.cursor.left == null || u.cursor.left < 0 ? null : u.posToVal(u.cursor.left, 'x')
            group.hover.forEach((f) => f(hx))
            showTip(u)
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
    u.over.appendChild(tip)
    plot.current = u
    built.current = axis
    group.charts.add(u)
    ready = true
    const now = group.range ?? group.full
    if (now) {
      group.syncing = true // 다시 만든 축에 지금 범위를 씌울 뿐 — 다시 알리지 않는다 (다시 받기가 되풀이되지 않게)
      u.setScale('x', now)
      group.syncing = false
    }
    // 클릭(끌지 않고) = 커서 A, Shift + 클릭 = B. 끌면 확대라 커서를 옮기지 않는다
    let downX = 0
    const down = (e: MouseEvent) => (downX = e.clientX)
    const click = (e: MouseEvent) => {
      if (Math.abs(e.clientX - downX) > 3 || !group.onPick) return
      group.onPick(u.posToVal(e.offsetX, 'x'), e.shiftKey ? 'b' : 'a')
    }
    // 휠 = 마우스 자리를 가운데로 확대 · 축소 (처음 범위 밖으로는 나가지 않는다). Shift + 휠 · 가로 밀기 = 옆으로 넘기기
    const wheel = (e: WheelEvent) => {
      e.preventDefault()
      const { min, max } = u.scales.x
      if (min == null || max == null) return
      if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        const d = e.shiftKey ? e.deltaY : e.deltaX
        return panBy(group, Math.sign(d) * 0.15)
      }
      const at = u.posToVal(e.offsetX, 'x')
      const k = e.deltaY < 0 ? 0.8 : 1.25
      const [lo, hi] = extent(u, group)
      const nMin = Math.max(lo, at - (at - min) * k)
      const nMax = Math.min(hi, at + (max - at) * k)
      if (nMax - nMin > 1e-6) u.setScale('x', { min: nMin, max: nMax })
    }
    u.over.addEventListener('mousedown', down)
    u.over.addEventListener('click', click)
    u.over.addEventListener('wheel', wheel, { passive: false })
    return () => {
      u.over.removeEventListener('mousedown', down)
      u.over.removeEventListener('click', click)
      u.over.removeEventListener('wheel', wheel)
      group.charts.delete(u)
      u.destroy()
      plot.current = null
      built.current = null
    }
  }, [shape, channels, colors, showX, group, theme, readout]) // 크기 · 값만 바뀌면 다시 만들지 않는다 (아래 setData · setSize)

  // 값만 바뀜 (확대해서 다시 받음 등) — 그래프를 그대로 두고 값을 바꿔 끼운다. 보는 x 범위는 그대로, 세로축은 새 값에 맞춘다
  useEffect(() => {
    const u = plot.current
    if (!u || built.current === axis) return
    built.current = axis
    u.setData(axis.data, false)
    const xs = axis.data[0] as number[]
    const r = group.range ?? group.full ?? (xs.length ? { min: xs[0], max: xs[xs.length - 1] } : null)
    if (r) u.setScale('x', r) // x 가 같아도 이걸로 세로축을 다시 잰다 (uPlot 은 x 를 정할 때 보이는 값으로 세로축을 잡는다)
  }, [axis, group])

  useEffect(() => {
    plot.current?.redraw(false, false)
  }, [shade])

  useEffect(() => {
    plot.current?.setSize({ width, height })
  }, [width, height])

  return <div ref={box} className="chart-plot" />
}

// 0 을 가운데 두는 채널의 세로축 범위 — 위아래 같은 폭 (lookup.centerZero)
function zeroRange(c: Channel | undefined): uPlot.Scale.Range | undefined {
  if (!centerZero(c)) return undefined
  return (_u, min, max) => {
    if (min == null || max == null) return [min, max]
    const m = Math.max(Math.abs(min), Math.abs(max)) || 1
    const [lo, hi] = uPlot.rangeNum(-m, m, 0.1, true)
    const h = Math.max(-(lo ?? -m), hi ?? m)
    return [-h, h]
  }
}
