// 실시간 트랙맵 — 경기장 기준선 · 계측선, 그날 달린 궤적(바탕선), 최근 30 s 꼬리 · 지금 위치. 위치가 낡았으면 점을 회색 빈 원으로.
// 범위는 기준선 + 달린 궤적 + 꼬리 + 지금 위치를 다 담게 (콘 코스가 기준선 밖이어도 화면 안에)
import { useEffect, useRef, useState } from 'react'

import type { LiveTrack } from '../types'
import { mapBounds, pathRuns } from './model'

interface Props {
  track: LiveTrack
  trail: [number, number][]
  position: { x_m: number; y_m: number } | null
  fresh: boolean
  dimLine?: boolean // 기준선에서 벗어나 달리는 중 — 기준선을 흐리게
  theme: string
  height: number
}

const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()

export default function LiveMap({ track, trail, position, fresh, dimLine = false, theme, height }: Props) {
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
    const ref: [number, number][] = xs.map((x, i) => [x, ys[i]])
    const runs = pathRuns(track.path)
    const box = mapBounds([...ref, ...runs.flat(), ...trail, ...(position ? [[position.x_m, position.y_m] as [number, number]] : [])])
    if (!box) return
    const { minX, maxX, minY, maxY } = box
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

    ctx.globalAlpha = dimLine ? 0.35 : 1
    line(ref, cssVar('--raised') || '#2a2f36', 6)
    ctx.globalAlpha = 1
    // 그날 달린 궤적 — 기준선 위에 가는 선 (끊긴 곳은 잇지 않는다)
    const muted = cssVar('--text-muted') || '#8e959e'
    ctx.globalAlpha = 0.6
    for (const r of runs) line(r, muted, 1.5)
    ctx.globalAlpha = 1
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
    if (trail.length > 1) line(trail, fresh ? accent : muted, 3)
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
        ctx.strokeStyle = muted // 낡은 위치 — 칠하지 않는다
      }
      ctx.stroke()
    }
  }, [track, trail, position, fresh, dimLine, width, height, theme])

  return <canvas ref={canvas} className="live-map-canvas" style={{ height }} aria-label="트랙맵 · 지금 위치" role="img" />
}
