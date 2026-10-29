// 가운데 그래프 — 축(채널 1 ~ 2)을 위아래로 쌓는다. 축 머리에 채널 이름 · 마우스 위치의 값(랩마다 색) · 오른쪽 축 더하기 · 빼기

import { useEffect, useMemo, useRef, useState } from 'react'

import type { Channel, DeltaResponse, QualityItem, SeriesResponse } from '../types'
import Chart, { type ZoomGroup } from './Chart'
import type { XMode } from './state'
import { axisData, DELTA, shadeRanges, type Trace } from './traces'

interface Props {
  axes: string[][]
  channels: Map<string, Channel>
  traces: Trace[]
  responses: Map<string, SeriesResponse>
  deltas: Map<string, DeltaResponse>
  x: XMode
  quality: QualityItem[]
  colors: string[]
  theme: string
  group: ZoomGroup
  onRemove: (axis: number) => void
  onAdd: (axis: number, channel: string) => void
}

const HEAD_W = 170
const X_AXIS_H = 24
const MIN_H = 64

export default function Plots(p: Props) {
  const area = useRef<HTMLDivElement>(null)
  const xRead = useRef<HTMLSpanElement>(null)
  const [size, setSize] = useState({ w: 800, h: 500 })
  useEffect(() => {
    if (!area.current) return
    const r = area.current.getBoundingClientRect() // 처음 그릴 때 바로 — 크기 감지가 늦게 와도 영역을 채우게
    setSize({ w: r.width, h: r.height })
    const ro = new ResizeObserver(([e]) => setSize({ w: e.contentRect.width, h: e.contentRect.height }))
    ro.observe(area.current)
    return () => ro.disconnect()
  }, [])
  useEffect(() => {
    const show = (v: number | null) => {
      if (xRead.current) xRead.current.textContent = v == null ? '' : `마우스 ${v.toFixed(p.x === 'time' ? 2 : 1)} ${p.x === 'time' ? 's' : 'm'}`
    }
    p.group.hover.add(show)
    return () => void p.group.hover.delete(show)
  }, [p.group, p.x])

  const refResp = p.responses.get('ref')
  const refLap = p.traces[0]?.lap ?? null
  const n = Math.max(p.axes.length, 1)
  const h = Math.max(MIN_H, Math.floor((size.h - X_AXIS_H - 1) / n))
  const xLabel = p.x === 'distance' ? '기준 거리 (계측선부터, 기준 랩 주행선 위) m' : refLap === null ? '세션 시간 s' : '랩 시작부터 시간 s'

  return (
    <div className="plots">
      <div className="plots-area" ref={area}>
        {p.axes.length === 0 && <p className="panel-msg muted">왼쪽 채널 목록에서 채널을 누르면 그래프가 생깁니다.</p>}
        {p.axes.map((chs, i) => (
          <AxisRow key={chs.join(',')} index={i} ids={chs} {...p} width={Math.max(200, size.w - HEAD_W)} height={h + (i === p.axes.length - 1 ? X_AXIS_H : 0)}
                   showX={i === p.axes.length - 1} refResp={refResp} refLap={refLap} />
        ))}
      </div>
      <div className="plots-foot">
        <span>{xLabel}</span>
        <span ref={xRead} />
        <span style={{ flex: 1 }} />
        <span className="muted">클릭 = 커서 A · Shift + 클릭 = B · 끌기 = 확대 · 두 번 클릭 = 처음 범위 · 옅은 띠 = 줄인 칸의 최소 ~ 최대 · 빈 곳은 선을 끊음</span>
      </div>
    </div>
  )
}

function AxisRow(p: Props & { index: number; ids: string[]; width: number; height: number; showX: boolean; refResp?: SeriesResponse; refLap: number | null }) {
  const [readout, setReadout] = useState<HTMLDivElement | null>(null)
  const chs = useMemo(() => p.ids.map((id) => p.channels.get(id)).filter((c): c is Channel => !!c), [p.ids, p.channels])
  const data = useMemo(() => axisData(p.traces, p.responses, p.ids, p.x, p.deltas), [p.traces, p.responses, p.ids, p.x, p.deltas])
  const shade = useMemo(() => shadeRanges(p.quality, p.ids, p.x, p.refResp, p.refLap), [p.quality, p.ids, p.x, p.refResp, p.refLap])
  const others = [...p.channels.values()].filter((c) => !p.ids.includes(c.channel_id))

  return (
    <div className="axis-row" style={{ height: p.height }}>
      <div className="axis-head" style={{ width: HEAD_W }}>
        <div className="axis-title">
          {chs.map((c, k) => (
            <div key={c.channel_id} className="axis-ch" title={[c.channel_id, c.sign && `부호: ${c.sign}`, ...c.cautions].filter(Boolean).join('\n')}>
              <span className={`axis-line${k ? ' dashed' : ''}`} aria-hidden="true" />
              <span className="axis-name">{c.label}</span>
              <span className="muted">{c.unit}</span>
              {c.cautions.length > 0 && <span className="st-warn">▲</span>}
            </div>
          ))}
          <button type="button" className="axis-x" aria-label="이 축 빼기" title="이 축 빼기" onClick={() => p.onRemove(p.index)}>
            ×
          </button>
        </div>
        <div className="axis-read" ref={setReadout}>
          {chs.map((c, k) => (
            <div key={c.channel_id}>
              {data.meta.map((m, i) =>
                m.kind === 'v' && m.axis === k ? (
                  <span key={i} data-s={i + 1} data-d={c.digits} style={{ color: p.colors[m.trace.slot] }}>
                    —
                  </span>
                ) : null,
              )}
            </div>
          ))}
        </div>
        {chs.length === 1 && (
          <select className="field axis-add" aria-label="오른쪽 세로축에 채널 더하기" value=""
                  onChange={(e) => e.target.value && p.onAdd(p.index, e.target.value)}>
            <option value="">+ 오른쪽 축</option>
            {others.map((c) => (
              <option key={c.channel_id} value={c.channel_id}>
                {c.label} ({c.unit || '-'})
              </option>
            ))}
          </select>
        )}
      </div>
      {data.meta.length > 0 ? (
        <Chart channels={chs} axis={data} colors={p.colors} width={p.width} height={p.height} showX={p.showX} shade={shade} group={p.group}
               theme={p.theme} readout={readout} />
      ) : (
        <p className="panel-msg muted">{p.ids.includes(DELTA) ? '시간 차는 거리축에서 비교 랩을 고르면 나옵니다.' : '이 랩에는 이 채널의 값이 없습니다.'}</p>
      )}
    </div>
  )
}
