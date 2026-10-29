// 세션 하나 — 주소는 /sessions/<sid>. 개요 화면(보드 2)은 다음 단계에서 채운다 — 지금은 요약 · 데이터 현황만.
// 없어진 sid 는 엔진이 알려 준 지금 세션으로 넘긴다 (계약 §3-1 · §6-10): 하나면 넘기고, 여럿이면 고르게, 없으면 알린다

import { useEffect } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'

import { useApi } from '../api'
import { COMPUTED, MARK } from '../sessions/blocks'
import { formatDuration, formatLapTime, formatStart, formatWithUnit, lapsCell, sessionName } from '../sessions/format'
import type { Resolved, SessionPage } from '../types'

// 넘어왔을 때 띠에 붙는 이유 (계약 §6-10 의 why)
const MOVED_WHY: Record<string, string> = {
  replaced: '다른 세션과 합쳐지거나 구간이 바뀌어',
  split: '여러 세션으로 나뉘어',
}

export default function SessionView() {
  const { sid = '' } = useParams()
  const { data, error } = useApi<SessionPage>(`/api/sessions/${encodeURIComponent(sid)}`)
  const navigate = useNavigate()
  const location = useLocation()
  const moved = (location.state as { movedFrom?: Resolved } | null)?.movedFrom
  // 닫으면 기록의 state 도 지운다 — 새로고침해도 다시 뜨지 않게
  const dismissMoved = () => navigate(location.pathname + location.search, { replace: true, state: null })
  const retired = error?.code === 'session_retired' ? (error.detail as Resolved) : null

  useEffect(() => {
    if (retired?.current.length === 1)
      navigate(`/sessions/${encodeURIComponent(retired.current[0])}`, { replace: true, state: { movedFrom: retired } })
  }, [retired, navigate])

  if (retired && retired.current.length !== 1)
    return (
      <div className="page-msg">
        <h1>{retired.current.length > 1 ? '세션이 나뉘었습니다' : '삭제된 세션입니다'}</h1>
        <p>
          <span className="mono-id">{retired.sid}</span>{' '}
          {retired.current.length > 1 ? '세션은 여러 세션으로 나뉘었습니다' : '세션은 삭제되었습니다'}
          {retired.retired_at && ` (${retired.retired_at})`}.
        </p>
        {retired.current.length > 1 && (
          <>
            <p>이어서 볼 세션을 선택하세요.</p>
            <ul>
              {retired.current.map((s) => (
                <li key={s}>
                  <Link to={`/sessions/${encodeURIComponent(s)}`}>{s}</Link>
                </li>
              ))}
            </ul>
          </>
        )}
        <p>
          <Link to="/sessions">세션 목록으로</Link>
        </p>
      </div>
    )
  if (error && !retired)
    return (
      <div className="page-msg">
        <h1>{error.code === 'unknown_session' ? '세션을 찾을 수 없습니다' : '세션을 열 수 없습니다'}</h1>
        <p>{error.message}</p>
        <p>
          <Link to="/sessions">세션 목록으로</Link>
        </p>
      </div>
    )
  if (!data) return <div className="page-msg muted">세션 불러오는 중…</div>

  const s = data.summary
  const name = sessionName(s, { withCar: true })
  const cautions = data.blocks.flatMap((b) => b.cautions.map((c) => `${b.title}: ${c}`))
  const warned = data.blocks.filter((b) => b.status === 'warn' && !COMPUTED.some((c) => c.key === b.block)) // 추정 블록은 늘 ▲ 라 띠에서 뺀다
  return (
    <>
      {moved && (
        <div className="banner info" role="status">
          <span>
            이전 주소(<span className="mono-id">{moved.sid}</span>)의 세션이 {MOVED_WHY[moved.why ?? ''] ?? '바뀌어'} 이 세션으로 이동했습니다.
          </span>
          <button type="button" className="banner-close" aria-label="알림 닫기" onClick={dismissMoved}>
            ×
          </button>
        </div>
      )}
      {warned.length > 0 && (
        <div className="banner">
          <span>주의: {warned.map((b) => b.title).join(' · ')} — 아래 데이터 현황의 ▲ 항목을 확인하세요.</span>
        </div>
      )}
      <div className="session-head">
        <h1>
          {name.main}
          {name.purpose && <span className="muted" style={{ fontWeight: 400 }}> · {name.purpose}</span>}
        </h1>
        <div className="sub">
          <span className="mono-id">{s.sid}</span>
          <span>{s.event_title} · {s.place}</span>
          {s.split_by === 'time' && <span>시각으로 나눈 세션</span>}
          <span>세션 개요 · 랩 표 · 그래프는 구현 중입니다</span>
        </div>
      </div>
      <div className="session-body">
        <section className="panel">
          <h2>요약</h2>
          <dl className="facts">
            <dt>시작</dt>
            <dd>{formatStart(s.start)}</dd>
            <dt>종목</dt>
            <dd>{s.discipline}</dd>
            <dt>드라이버</dt>
            <dd>{s.driver_label || '—'}</dd>
            <dt>주행 시간</dt>
            <dd>{formatDuration(s.duration_s)}</dd>
            <dt>거리</dt>
            <dd>{formatWithUnit(s.distance_km, 2, 'km')}</dd>
            <dt>랩</dt>
            <dd title={lapsCell(s).tip}>{lapsCell(s).text}</dd>
            <dt>최속 랩</dt>
            <dd className={s.best_lap_s !== null ? 'best' : undefined}>
              {formatLapTime(s.best_lap_s)}
              {data.best_lap !== null && ` (${data.best_lap}랩)`}
            </dd>
            <dt>최고 속도</dt>
            <dd>{formatWithUnit(s.stats.vmax_kmh, 1, 'km/h')}</dd>
          </dl>
          {data.notes.length > 0 && (
            <ul>
              {data.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          )}
        </section>
        <section className="panel">
          <h2>데이터 현황</h2>
          <table className="grid">
            <colgroup>
              <col style={{ width: 34 }} />
              <col style={{ width: 220 }} />
              <col />
            </colgroup>
            <tbody>
              {s.data_status.map((d) => (
                <tr key={d.block} style={{ cursor: 'default' }}>
                  <td className={`mark st-${d.status}`} title={MARK[d.status].text}>
                    {MARK[d.status].mark}
                  </td>
                  <td>{d.title}</td>
                  <td className="muted" title={d.detail}>
                    {d.detail}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {cautions.length > 0 && (
            <ul>
              {cautions.map((c) => (
                <li key={c} className="st-warn">
                  {c}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  )
}
