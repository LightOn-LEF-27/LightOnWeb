// 아래 랩 표 — ● 기준 하나 · ☑ 비교 최대 2. 색은 그 랩의 그래프 색. "세션 전체" 는 시간축으로 세션을 통째로 본다

import { lapCell } from '../overview/model'
import type { LapTable } from '../types'
import { MAX_CMP } from './state'

interface Props {
  table: LapTable
  refLap: number | 'all'
  cmp: number[]
  colors: string[]
  onRef: (lap: number | 'all') => void
  onCmp: (laps: number[]) => void
}

const SHOW = ['lap_time_s', 's1_s', 's2_s', 's3_s', 'vmax_kmh', 'net_wh', 'i_max_a', 'cell_t_max_c']

export default function LapPicker({ table, refLap, cmp, colors, onRef, onCmp }: Props) {
  const cols = table.columns.filter((c) => SHOW.includes(c.key))
  const colorOf = (lap: number) => (lap === refLap ? colors[0] : cmp.includes(lap) ? colors[cmp.indexOf(lap) + 1] : 'transparent')
  const toggle = (lap: number) => onCmp(cmp.includes(lap) ? cmp.filter((c) => c !== lap) : [...cmp, lap].slice(-MAX_CMP))
  return (
    <section className="lap-picker" aria-label="랩 고르기">
      <div className="lap-picker-head">
        <strong>랩</strong>
        <span className="muted">● 기준 하나 · ☑ 비교 최대 {MAX_CMP}</span>
        <span style={{ flex: 1 }} />
        <span className="muted">
          <span className="best">보라</span> 세션 최속 (랩타임 · 구간)
        </span>
      </div>
      <div className="lap-picker-scroll">
        <table className="grid lap-table">
          <thead>
            <tr className="cols">
              <th className="swatch" aria-label="색" />
              <th>기준</th>
              <th>비교</th>
              <th className="num">랩</th>
              {cols.map((c) => (
                <th key={c.key} className="num">
                  {c.label}
                  {c.unit && <span className="unit">{c.unit}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className={refLap === 'all' ? 'picked' : undefined}>
              <td className="swatch">{refLap === 'all' && <span style={{ background: colors[0] }} />}</td>
              <td>
                <input type="radio" name="ref" aria-label="세션 전체를 기준으로" checked={refLap === 'all'} onChange={() => onRef('all')} />
              </td>
              <td />
              <td className="num muted" colSpan={cols.length + 1} style={{ textAlign: 'left' }}>
                세션 전체 (시간축)
              </td>
            </tr>
            {table.rows.map((r) => (
              <tr key={r.lap} className={r.lap === refLap || cmp.includes(r.lap) ? 'picked' : undefined}>
                <td className="swatch">
                  <span style={{ background: colorOf(r.lap) }} />
                </td>
                <td>
                  <input type="radio" name="ref" aria-label={`랩 ${r.lap} 을 기준으로`} checked={r.lap === refLap} onChange={() => onRef(r.lap)} />
                </td>
                <td>
                  <input type="checkbox" aria-label={`랩 ${r.lap} 비교`} checked={cmp.includes(r.lap)} disabled={r.lap === refLap || refLap === 'all'}
                         onChange={() => toggle(r.lap)} />
                </td>
                <td className="num">{r.lap}</td>
                {cols.map((c) => {
                  const cell = lapCell(r, c)
                  return (
                    <td key={c.key} className={`num${cell.best ? ' best' : ''}`}>
                      {cell.text}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
