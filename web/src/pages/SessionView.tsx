// 보드 2 · 세션 개요 — 주소는 /sessions/<sid>. 주의 띠 · 요약 · 데이터 현황(펼치면 원본 → 코드 → 결과물) · 랩 표 · 미보유 데이터.
// 없어진 sid 는 엔진이 알려 준 지금 세션으로 넘긴다 (계약 §3-1 · §6-10): 하나면 넘기고, 여럿이면 고르게, 없으면 알린다

import { useEffect } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import { useApi } from '../api'
import DataStatus from '../overview/DataStatus'
import { Toast, useDownload } from '../overview/Download'
import LapTable from '../overview/LapTable'
import { bandItems, lapEmptyText, summaryRows } from '../overview/model'
import { formatStart, sessionName } from '../sessions/format'
import type { LapTable as Laps, QualityItem, Resolved, SessionPage } from '../types'

// 넘어왔을 때 띠에 붙는 이유 (계약 §6-10 의 why)
const MOVED_WHY: Record<string, string> = {
  replaced: '다른 세션과 합쳐지거나 구간이 바뀌어',
  split: '여러 세션으로 나뉘어',
}

export default function SessionView() {
  const { sid = '' } = useParams()
  const base = `/api/sessions/${encodeURIComponent(sid)}`
  const { data, error } = useApi<SessionPage>(base)
  const laps = useApi<Laps>(`${base}/laps`)
  const quality = useApi<QualityItem[]>(`${base}/quality`)
  const [params, setParams] = useSearchParams()
  const { download, notice, dismiss } = useDownload()
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
  const name = sessionName(s)
  const date = s.start ? s.start.slice(0, 10) : ''
  const band = bandItems(quality.data ?? [], s.data_status)
  const missing = data.missing.filter((m) => m.status === 'seek')
  const openBlock = params.get('block')
  const toggleBlock = (b: string) => {
    const next = new URLSearchParams(params)
    if (openBlock === b) next.delete('block')
    else next.set('block', b)
    setParams(next, { replace: true, state: location.state })
  }

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
      {band.length > 0 && (
        <div className="banner caution" role="note">
          <strong className="st-warn">▲ 주의 {band.length}</strong>
          <ul className="plain">
            {band.map((b) => (
              <li key={`${b.title}-${b.message}`}>
                {b.title} — {b.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="session-head">
        <nav className="crumbs" aria-label="위치">
          <Link to="/sessions">세션</Link> / {s.car}
          {date && ` / ${date}`}
          {s.day_order ? ` / R${s.day_order}` : ''}
        </nav>
        <div className="head-row">
          <h1>
            {name.main}
            {name.purpose && <span className="muted" style={{ fontWeight: 400 }}> · {name.purpose}</span>}
          </h1>
          <Link className="btn btn-primary" to={`/analysis?sid=${encodeURIComponent(s.sid)}`}>
            분석 화면 열기
          </Link>
        </div>
        <div className="sub">
          <span className="mono-id">{s.sid}</span>
          {s.split_by === 'time' && <span>시각으로 나눈 세션</span>}
        </div>
      </div>
      <div className="overview">
        <div className="overview-top">
          <div className="overview-left">
            <section className="panel">
              <h2>요약</h2>
              <dl className="facts">
                {summaryRows(data, formatStart(s.start, { seconds: true })).map((r) => (
                  <div key={r.k} className="fact">
                    <dt>{r.k}</dt>
                    <dd className={r.best ? 'best' : undefined}>{r.v}</dd>
                  </div>
                ))}
              </dl>
              {data.notes.length > 0 && (
                <ul className="plain notes">
                  {data.notes.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              )}
              <p className="made muted">
                세션 정보 {formatStart(data.made.built_at)} · {data.made.commit}
              </p>
            </section>
            {missing.length > 0 && (
              <section className="panel">
                <h2>미보유 데이터</h2>
                <ul className="plain missing">
                  {missing.map((m) => (
                    <li key={m.data}>
                      <span className="st-seek">✕</span> <strong>{m.data}</strong>
                      <p>{m.reason}</p>
                      {m.ask && <p className="muted">찾는 분: {m.ask}</p>}
                    </li>
                  ))}
                </ul>
                <p className="panel-msg muted">가진 분은 DAQ 파트에 알려 주세요.</p>
              </section>
            )}
          </div>
          <DataStatus statuses={s.data_status} blocks={data.blocks} quality={quality.data ?? []} open={openBlock}
                      onToggle={toggleBlock} onDownload={download} />
        </div>
        {laps.data ? (
          <LapTable table={laps.data} empty={lapEmptyText(data)} />
        ) : (
          <section className="panel">
            <h2>랩</h2>
            <p className="panel-msg muted">{laps.error ? `랩 표를 불러오지 못했습니다 — ${laps.error.message}` : '랩 표 불러오는 중…'}</p>
          </section>
        )}
      </div>
      <Toast notice={notice} onClose={dismiss} />
    </>
  )
}
