// 실시간 트랙맵 — 경기장 기준선 · 계측선 위에 최근 궤적과 지금 위치. 위치가 낡았으면 점을 회색 빈 원으로
import { useEffect, useRef, useState } from 'react'

import type { LiveTrack } from '../types'

interface Props {
  track: LiveTrack
  trail: [number, number][]
  position: { x_m: number; y_m: number } | null
  fresh: boolean
  theme: string
  height: number
}

const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()

export default function LiveMap({ track, trail, position, fresh, theme, height }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [width, setWidth] = useState(0)

  useEffect(() => {
    const c = canvas.current
    if (!c) return
    const ro = new ResizeObserver(() => setWidth(Math.floor(c.clientWidth)))
    ro.observe(c)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    const c = canvas.current
    const { x_m: xs, y_m: ys } = track.line
    if (!c || width < 20 || !xs.length) return
    const dpr = window.devicePixelRatio || 1
    c.width = width * dpr
    c.height = height * dpr
    const ctx = c.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, width, height)
    const pad = 14
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys)
    const k = Math.min((width - 2 * pad) / Math.max(maxX - minX, 1), (height - 2 * pad) / Math.max(maxY - minY, 1))
    const ox = (width - k * (maxX - minX)) / 2
    const oy = (height - k * (maxY - minY)) / 2
    const at = (x: number, y: number): [number, number] => [ox + (x - minX) * k, height - (oy + (y - minY) * k)] // 북쪽이 위
    const line = (pts: [number, number][], color: string, w: number) => {
      ctx.strokeStyle = color
      ctx.lineWidth = w
      ctx.lineCap = 'round'
      ctx.lineJoin = 'round'
      ctx.beginPath()
      pts.forEach(([x, y], i) => (i ? ctx.lineTo(...at(x, y)) : ctx.moveTo(...at(x, y))))
      ctx.stroke()
    }

    line(xs.map((x, i) => [x, ys[i]]), cssVar('--raised') || '#2a2f36', 6)
    // 계측선 — 진행 방위(북 = 0, 시계방향)에 수직인 짧은 선
    const g = track.gate
    const r = (g.heading_deg * Math.PI) / 180
    const [cx, cy] = at(g.x, g.y)
    ctx.strokeStyle = cssVar('--text-strong') || '#fff'
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.moveTo(cx - Math.cos(r) * 11, cy - Math.sin(r) * 11)
    ctx.lineTo(cx + Math.cos(r) * 11, cy + Math.sin(r) * 11)
    ctx.stroke()

    const accent = cssVar('--trace-0') || '#f08a3c'
    if (trail.length > 1) line(trail, fresh ? accent : cssVar('--text-muted') || '#8e959e', 3)
    if (position) {
      const [px, py] = at(position.x_m, position.y_m)
      ctx.beginPath()
      ctx.arc(px, py, 6, 0, Math.PI * 2)
      ctx.lineWidth = 2
      if (fresh) {
        ctx.fillStyle = accent
        ctx.fill()
        ctx.strokeStyle = cssVar('--text-strong') || '#fff'
      } else {
        ctx.strokeStyle = cssVar('--text-muted') || '#8e959e' // 낡은 위치 — 칠하지 않는다
      }
      ctx.stroke()
    }
  }, [track, trail, position, fresh, width, height, theme])

  return <canvas ref={canvas} className="live-map-canvas" style={{ height }} aria-label="트랙맵 · 지금 위치" role="img" />
}
