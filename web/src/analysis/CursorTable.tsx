// 커서 값 표 — 커서 A 에서 축에 그린 채널마다 랩별 값, 그리고 기준 랩의 A − B. 값은 그 자리에 가장 가까운 칸 그대로

import { formatValue } from '../overview/model'
import type { Channel, DeltaResponse, SeriesResponse } from '../types'
import { nearestIndex, valueAt } from './lookup'
import type { XMode } from './state'
import { DELTA, xShift, type Trace } from './traces'

interface Props {
  channels: Channel[] // 축에 그린 순서
  traces: Trace[]
  responses: Map<string, SeriesResponse>
  deltas: Map<string, DeltaResponse>
  x: XMode
  colors: string[]
  a: number | null
  b: number | null
}

export default function CursorTable({ channels, traces, responses, deltas, x, colors, a, b }: Props) {
  const unit = x === 'time' ? 's' : 'm'
  const val = (t: Trace, ch: string, at: number | null): number | null => {
    if (at == null) return null
    if (ch === DELTA) {
      const d = deltas.get(t.key)
      if (!d) return t.slot === 0 ? 0 : null
      return d.delta_s[nearestIndex(d.x.values, at)] ?? null
    }
    const r = responses.get(t.key)
    return r ? valueAt(r, ch, at, xShift(r, x, t.lap)) : null
  }
  return (
    <section className="side-block">
      <h2>
        커서 값 · {a == null ? 'A 없음' : `A ${a.toFixed(x === 'time' ? 2 : 1)} ${unit}`}
        {b != null && ` · B ${b.toFixed(x === 'time' ? 2 : 1)} ${unit}`}
      </h2>
      {a == null ? (
        <p className="panel-msg muted">그래프를 클릭하면 커서 A 가 놓입니다 (Shift + 클릭 = B).</p>
      ) : (
        <table className="grid cursor-table">
          <thead>
            <tr className="cols">
              <th>채널 · 커서 A</th>
              {traces.map((t, i) => (
                <th key={t.key} className="num" style={{ color: colors[t.slot] }}>
                  {i === 0 ? '기준' : `비교 ${i}`}
                </th>
              ))}
              {b != null && <th className="num">A − B</th>}
            </tr>
          </thead>
          <tbody>
            {channels.map((c) => {
              const ref = traces[0]
              const va = val(ref, c.channel_id, a)
              const vb = val(ref, c.channel_id, b)
              return (
                <tr key={c.channel_id} style={{ cursor: 'default' }}>
                  <td title={c.channel_id}>
                    {c.label} <span className="muted">{c.unit}</span>
                  </td>
                  {traces.map((t) => (
                    <td key={t.key} className="num">
                      {formatValue(val(t, c.channel_id, a), c.digits)}
                    </td>
                  ))}
                  {b != null && <td className="num muted">{va != null && vb != null ? formatValue(va - vb, c.digits) : '—'}</td>}
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </section>
  )
}
