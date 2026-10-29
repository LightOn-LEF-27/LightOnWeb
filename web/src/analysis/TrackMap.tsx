// 트랙맵 — 기준 랩 주행선(세션 전체면 전체 궤적)을 고른 색 기준 하나로 칠한다. 계측선 · 구간 경계, 마우스 · 커서 A · B 점.
// 색 기준 (사이트_설계.md §7): 구간 시간(기본 — 보라 최속 · 초록 평균보다 빠름 · 빨강 느림) · 시간 차(비교 1) · 채널 값

import { useEffect, useRef } from 'react'

import type { DeltaResponse, SectorsResponse, TrackResponse } from '../types'
import type { ZoomGroup } from './Chart'
import { channelColors, deltaColors, paceColors, trackIndex } from './lookup'
import type { XMode } from './state'

export type MapMode = 'pace' | 'delta' | `ch:${string}`

interface Props {
  track: TrackResponse // 기준 랩 (또는 세션 전체)
  colored?: TrackResponse | null // 채널 색일 때 color 가 붙은 같은 주행선
  mode: MapMode
  sectors: SectorsResponse | null
  delta: DeltaResponse | null
  refLap: number | null
  x: XMode
  lapStart: number
  group: ZoomGroup
  cursorA: number | null
  cursorB: number | null
  theme: string
  height: number
}

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

export default function TrackMap(p: Props) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const base = useRef<ImageData | null>(null) // 주행선까지 그린 그림 — 마우스가 움직이면 점만 다시 얹는다
  const geo = useRef<{ px: (i: number) => [number, number] } | null>(null)

  // 주행선 · 계측선 · 구간 경계
  useEffect(() => {
    const c = canvas.current
    if (!c) return
    const dpr = window.devicePixelRatio || 1
    const w = c.clientWidth
    const h = p.height
    c.width = w * dpr
    c.height = h * dpr
    const ctx = c.getContext('2d')!
    ctx.scale(dpr, dpr)
    ctx.clearRect(0, 0, w, h)
    const { x_m: xs, y_m: ys } = p.track
    if (!xs.length) return
    const pad = 14
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys)
    const k = Math.min((w - 2 * pad) / Math.max(maxX - minX, 1), (h - 2 * pad) / Math.max(maxY - minY, 1))
    const ox = (w - k * (maxX - minX)) / 2
    const oy = (h - k * (maxY - minY)) / 2
    const at = (x: number, y: number): [number, number] => [ox + (x - minX) * k, h - (oy + (y - minY) * k)] // 북쪽이 위
    geo.current = { px: (i) => at(xs[i], ys[i]) }

    let colors: (string | null)[] | null = null
    if (p.mode === 'pace' && p.sectors && p.refLap !== null) colors = paceColors(p.track, p.sectors, p.refLap)
    else if (p.mode === 'delta' && p.delta) colors = deltaColors(p.track, p.delta.mini.edges_ref_m, p.delta.mini.delta_s)
    else if (p.mode.startsWith('ch:') && p.colored?.color) colors = channelColors(p.colored.color.values, p.colored.color.levels).colors
    const plain = cssVar('--text-muted') || '#8e959e'
    ctx.lineWidth = 3.5
    ctx.lineCap = 'round'
    for (let i = 1; i < xs.length; i++) {
      const col = colors ? colors[i] : plain
      if (!col) continue // 값이 없는 점은 비운다
      ctx.strokeStyle = col
      ctx.beginPath()
      ctx.moveTo(...at(xs[i - 1], ys[i - 1]))
      ctx.lineTo(...at(xs[i], ys[i]))
      ctx.stroke()
    }
    // 계측선 · 구간 경계 — 진행 방위에 수직인 짧은 선
    const tick = (x: number, y: number, deg: number, len: number, color: string, width: number) => {
      const r = (deg * Math.PI) / 180
      const [cx, cy] = at(x, y)
      const dx = Math.cos(r) * len, dy = Math.sin(r) * len // 화면에서 진행 방향의 수직 (북 = 0 → 화면 위)
      ctx.strokeStyle = color
      ctx.lineWidth = width
      ctx.beginPath()
      ctx.moveTo(cx - dx, cy - dy)
      ctx.lineTo(cx + dx, cy + dy)
      ctx.stroke()
    }
    const strong = cssVar('--text-strong') || '#fff'
    const g = p.track.gate
    tick(g.x_m, g.y_m, g.heading_deg, 11, strong, 2)
    for (const s of p.sectors?.sectors.slice(1) ?? []) tick(s.x_m, s.y_m, s.heading_deg, 7, cssVar('--text') || '#ccc', 1.5)
    base.current = ctx.getImageData(0, 0, c.width, c.height)
  }, [p.track, p.colored, p.mode, p.sectors, p.delta, p.refLap, p.height, p.theme])

  // 점 — 마우스(점선 원) · 커서 B(회색) · 커서 A(흰색)
  useEffect(() => {
    const c = canvas.current
    if (!c) return
    const ctx = c.getContext('2d')!
    const dpr = window.devicePixelRatio || 1
    const dot = (x: number | null, fill: string | null, stroke: string, dashed = false) => {
      if (x == null || !geo.current) return
      const [px, py] = geo.current.px(trackIndex(p.track, x, p.x, p.lapStart))
      ctx.save()
      ctx.setLineDash(dashed ? [2, 2] : [])
      ctx.lineWidth = 2
      ctx.strokeStyle = stroke
      ctx.beginPath()
      ctx.arc(px, py, 5, 0, Math.PI * 2)
      if (fill) {
        ctx.fillStyle = fill
        ctx.fill()
      }
      ctx.stroke()
      ctx.restore()
    }
    const draw = (hover: number | null) => {
      if (!base.current) return
      ctx.putImageData(base.current, 0, 0)
      ctx.save()
      ctx.scale(dpr, dpr)
      const muted = cssVar('--text-muted') || '#9aa1aa'
      dot(hover, null, muted, true)
      dot(p.cursorB, cssVar('--bg') || '#121417', muted)
      dot(p.cursorA, cssVar('--text-strong') || '#fff', cssVar('--bg') || '#121417')
      ctx.restore()
    }
    draw(null)
    p.group.hover.add(draw)
    return () => void p.group.hover.delete(draw)
  }, [p.track, p.cursorA, p.cursorB, p.x, p.lapStart, p.group, p.mode, p.colored, p.sectors, p.delta, p.height, p.theme])

  return <canvas ref={canvas} className="track-canvas" style={{ height: p.height }} aria-label="트랙맵" role="img" />
}
