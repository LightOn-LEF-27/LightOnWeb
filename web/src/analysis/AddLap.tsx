// "+ 추가" 창 — 다른 세션의 랩을 비교로 더한다. 같은 frame(같은 경기장 · 계측선)의 세션만 고를 수 있고, 다른 코스는 흐리게 (거리축에 겹칠 수 없다).
// 세션 → 랩 순서로 고르고, 드라이버로 거를 수 있다 (사이트_설계.md §7)

import { useState } from 'react'

import { useApi } from '../api'
import { lapCell } from '../overview/model'
import { sessionName } from '../sessions/format'
import type { LapTable, SessionRow } from '../types'

interface Props {
  sessions: SessionRow[]
  current: SessionRow
  onPick: (sid: string, lap: number) => void
  onClose: () => void
}

export default function AddLap({ sessions, current, onPick, onClose }: Props) {
  const [driver, setDriver] = useState('')
  const [sid, setSid] = useState<string | null>(null)
  const laps = useApi<LapTable>(sid ? `/api/sessions/${encodeURIComponent(sid)}/laps` : null)
  const withLaps = sessions.filter((s) => s.sid !== current.sid && (s.laps ?? 0) > 0)
  const drivers = [...new Set(withLaps.map((s) => s.driver_label).filter(Boolean))].sort()
  const shown = withLaps.filter((s) => !driver || s.driver_label === driver)
  const same = (s: SessionRow) => !!current.frame_id && s.frame_id === current.frame_id
  const sorted = [...shown].sort((a, b) => Number(same(b)) - Number(same(a)))
  const lapCol = laps.data?.columns.find((c) => c.key === 'lap_time_s')

  return (
    <div className="modal-back" role="presentation" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="다른 세션의 랩 추가" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <strong>다른 세션의 랩 추가</strong>
          <span className="muted">같은 코스(계측선)의 세션만 겹칠 수 있습니다</span>
          <span style={{ flex: 1 }} />
          <label htmlFor="drv" className="muted">
            드라이버
          </label>
          <select id="drv" className="field" value={driver} onChange={(e) => setDriver(e.target.value)}>
            <option value="">전부</option>
            {drivers.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
          <button type="button" className="banner-close" aria-label="닫기" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="modal-body">
          <ul className="plain modal-list">
            {sorted.map((s) => {
              const n = sessionName(s, { withCar: true })
              const ok = same(s)
              return (
                <li key={s.sid}>
                  <button type="button" className={`modal-item${sid === s.sid ? ' on' : ''}`} disabled={!ok} onClick={() => setSid(s.sid)}
                          title={ok ? s.sid : '다른 코스 — 거리축에 겹칠 수 없습니다'}>
                    <span>{n.main}</span>
                    <span className="muted">{n.purpose}</span>
                    <span className="muted">{s.laps}랩</span>
                  </button>
                </li>
              )
            })}
            {sorted.length === 0 && <li className="muted panel-msg">고를 수 있는 세션이 없습니다.</li>}
          </ul>
          <div className="modal-laps">
            {!sid && <p className="panel-msg muted">왼쪽에서 세션을 고르세요.</p>}
            {sid && !laps.data && <p className="panel-msg muted">랩 불러오는 중…</p>}
            {laps.data && lapCol && (
              <table className="grid">
                <tbody>
                  {laps.data.rows.map((r) => {
                    const cell = lapCell(r, lapCol)
                    return (
                      <tr key={r.lap} onClick={() => onPick(sid!, r.lap)}>
                        <td className="num">랩 {r.lap}</td>
                        <td className={`num${cell.best ? ' best' : ''}`}>{cell.text}</td>
                        <td>
                          <button type="button" className="btn btn-sm">
                            더하기
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
