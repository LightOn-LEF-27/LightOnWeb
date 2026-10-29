// 랩 표 — 열 순서 · 자릿수는 엔진이 준 그대로 (랩 · 랩타임 · S1 ~ S3 · …). 보라 = 세션 최속 (랩타임 · 구간)

import type { LapTable as Table } from '../types'
import { lapCell, lapFooter } from './model'

export default function LapTable({ table, empty }: { table: Table; empty: string | null }) {
  if (empty || table.rows.length === 0)
    return (
      <section className="panel">
        <h2>랩</h2>
        <p className="panel-msg muted">{empty ?? '랩이 없습니다.'}</p>
      </section>
    )
  const cols = table.columns
  const s = table.sectors
  return (
    <section className="panel">
      <h2>랩</h2>
      <div className="lap-scroll">
        <table className="grid lap-table">
          <thead>
            <tr className="cols">
              <th className="num lap-no">랩</th>
              {cols.map((c) => (
                <th key={c.key} className="num" title={[c.label, ...(c.cautions ?? [])].join('\n')}>
                  {c.label}
                  {c.unit && <span className="unit">{c.unit}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((r) => (
              <tr key={r.lap} style={{ cursor: 'default' }}>
                <td className="num lap-no">{r.lap}</td>
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
          {s && (
            <tfoot>
              <tr>
                <td className="num lap-no muted" title={`완주 랩 ${s.n_laps}개의 평균`}>
                  평균
                </td>
                {cols.map((c) => (
                  <td key={c.key} className="num muted">
                    {lapFooter(table, c).mean}
                  </td>
                ))}
              </tr>
              <tr>
                <td className="num lap-no muted" title="구간마다 최속을 더한 값">
                  이론
                </td>
                {cols.map((c) => (
                  <td key={c.key} className="num muted">
                    {lapFooter(table, c).theoretical}
                  </td>
                ))}
              </tr>
            </tfoot>
          )}
        </table>
      </div>
      <div className="legend lap-legend">
        <span>
          <span className="best">보라</span> 세션 최속 (랩타임 · 구간)
        </span>
        <span>S1 ~ S3 · 구간 시간</span>
        {s && <span>평균 · 완주 랩 {s.n_laps}개 · 이론 · 구간 최속의 합</span>}
      </div>
      {table.notes.length > 0 && (
        <ul className="plain notes muted">
          {table.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </section>
  )
}
