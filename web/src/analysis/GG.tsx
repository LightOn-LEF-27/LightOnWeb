// g-g — 가로 = 좌우 가속도(좌선회를 왼쪽에), 세로 = 앞뒤 가속도(가속 위 · 제동 아래), 기준 랩의 표본마다 점 하나, 고리 0.5 · 1.0 · 1.5 g.
// 커서 A 의 순간을 흰 점 + 지난 1 초 꼬리로. 아래 진행 막대 · ▶ 로 커서 A 를 옮긴다 (사이트_설계.md §7)
// 값은 엔진이 0.5 s 이동평균한 것 (카메라 가속도계처럼 떨리는 값 — smooth_s 는 엔진이 한다)

import { useEffect, useRef, useState } from 'react'

import type { SeriesResponse, TrackResponse } from '../types'
import type { ZoomGroup } from './Chart'
import { nearestIndex, trackIndex, xOfTrackPoint } from './lookup'
import type { XMode } from './state'

interface Props {
  gg: SeriesResponse // x = 세션 시간, imu.lat_g · imu.long_g
  track: TrackResponse | null // 커서(거리축) ↔ 시각을 잇는 데 쓴다
  x: XMode
  lapStart: number
  lapEnd: number
  a: number | null
  onA: (x: number) => void
  group: ZoomGroup
  theme: string
}

const SIZE = 200
const SCALE = 1.6 // 바깥 고리 1.5 g 가 들어가게

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

export default function GG(p: Props) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const [playing, setPlaying] = useState(false)
  const lat = p.gg.series.find((s) => s.channel_id === 'imu.lat_g')?.values ?? []
  const lon = p.gg.series.find((s) => s.channel_id === 'imu.long_g')?.values ?? []
  const ts = p.gg.x.values

  // 그래프의 x(커서 A) ↔ 세션 시간
  const timeOf = (x: number): number =>
    p.x === 'time' ? x + p.lapStart : p.track ? p.track.t_s[trackIndex(p.track, x, p.x, p.lapStart)] : x + p.lapStart
  const xOfTime = (t: number): number =>
    p.x === 'time' ? t - p.lapStart : p.track ? xOfTrackPoint(p.track, nearestIndex(p.track.t_s, t), p.x, p.lapStart) : t - p.lapStart
  const tA = p.a == null ? null : timeOf(p.a)

  useEffect(() => {
    const c = canvas.current
    if (!c) return
    const dpr = window.devicePixelRatio || 1
    const w = c.clientWidth
    c.width = w * dpr
    c.height = SIZE * dpr
    const ctx = c.getContext('2d')!
    ctx.scale(dpr, dpr)
    const cx = w / 2, cy = SIZE / 2, r = (SIZE / 2 - 8) / SCALE
    const px = (la: number, lo: number): [number, number] => [cx - la * r, cy - lo * r] // 좌선회(+)를 왼쪽에, 가속(+)을 위에
    const line = cssVar('--line') || '#3d434b'
    const muted = cssVar('--text-muted') || '#8e959e'
    ctx.clearRect(0, 0, w, SIZE)
    ctx.strokeStyle = line
    ctx.lineWidth = 1
    for (const g of [0.5, 1.0, 1.5]) {
      ctx.beginPath()
      ctx.arc(cx, cy, g * r, 0, Math.PI * 2)
      ctx.stroke()
    }
    ctx.beginPath()
    ctx.moveTo(cx - 1.5 * r, cy); ctx.lineTo(cx + 1.5 * r, cy)
    ctx.moveTo(cx, cy - 1.5 * r); ctx.lineTo(cx, cy + 1.5 * r)
    ctx.stroke()
    ctx.fillStyle = muted
    ctx.font = '10px sans-serif'
    ctx.fillText('가속 ↑', cx + 4, 11)
    ctx.fillText('제동 ↓', cx + 4, SIZE - 4)
    ctx.fillText('← 좌선회', 2, cy - 4)
    ctx.fillText('우선회 →', w - 52, cy - 4)
    ctx.fillText('1.5 g', cx + 1.5 * r * 0.71 + 2, cy - 1.5 * r * 0.71)
    // 기준 랩의 점 (흐리게)
    ctx.fillStyle = cssVar('--trace-0') || '#f08a3c'
    ctx.globalAlpha = 0.25
    for (let i = 0; i < ts.length; i++) {
      if (lat[i] == null || lon[i] == null) continue
      const [x, y] = px(lat[i]!, lon[i]!)
      ctx.fillRect(x - 1, y - 1, 2, 2)
    }
    ctx.globalAlpha = 1
    if (tA == null) return
    // 지난 1 초 꼬리 + 지금 점
    const i1 = nearestIndex(ts, tA)
    const i0 = nearestIndex(ts, tA - 1)
    ctx.strokeStyle = cssVar('--text-strong') || '#fff'
    ctx.lineWidth = 1.5
    ctx.beginPath()
    let pen = false
    for (let i = i0; i <= i1; i++) {
      if (lat[i] == null || lon[i] == null) {
        pen = false
        continue
      }
      const [x, y] = px(lat[i]!, lon[i]!)
      if (pen) ctx.lineTo(x, y)
      else ctx.moveTo(x, y)
      pen = true
    }
    ctx.stroke()
    if (lat[i1] != null && lon[i1] != null) {
      const [x, y] = px(lat[i1]!, lon[i1]!)
      ctx.fillStyle = cssVar('--text-strong') || '#fff'
      ctx.beginPath()
      ctx.arc(x, y, 4.5, 0, Math.PI * 2)
      ctx.fill()
    }
  }, [p.gg, tA, p.theme]) // 커서 A 의 시각 · 데이터 · 테마가 바뀔 때만 다시 그린다

  // ▶ 재생 — 커서 A 를 실제 시간대로 앞으로 (화면은 1 초에 15 번 갱신)
  useEffect(() => {
    if (!playing) return
    let t = tA ?? p.lapStart
    let last = performance.now()
    const id = setInterval(() => {
      const now = performance.now()
      t += (now - last) / 1000
      last = now
      if (t >= p.lapEnd) {
        setPlaying(false)
        t = p.lapEnd
      }
      p.onA(xOfTime(t))
    }, 1000 / 15)
    return () => clearInterval(id)
  }, [playing]) // 재생을 켜고 끌 때만 — 도는 동안의 커서는 이 안에서 옮긴다

  const span = Math.max(p.lapEnd - p.lapStart, 0.001)
  return (
    <section className="side-block">
      <h2>g-g · 기준 랩 · 점 하나 = 한 순간의 가속도</h2>
      <canvas ref={canvas} className="gg-canvas" style={{ height: SIZE }} role="img" aria-label="g-g 그림" />
      <div className="gg-bar">
        <button type="button" className="btn btn-sm" aria-label={playing ? '멈춤' : '재생'} onClick={() => setPlaying(!playing)}>
          {playing ? '❚❚' : '▶'}
        </button>
        <input type="range" aria-label="커서 A 위치" min={0} max={1000} value={tA == null ? 0 : Math.round(((tA - p.lapStart) / span) * 1000)}
               onChange={(e) => p.onA(xOfTime(p.lapStart + (Number(e.target.value) / 1000) * span))} />
        <span className="muted">{tA == null ? '' : `${(tA - p.lapStart).toFixed(1)} s`}</span>
      </div>
    </section>
  )
}
