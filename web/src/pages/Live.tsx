// 실시간 화면 (계약 §12, 실시간화면_v1.md) — 차량을 고르고 그 차의 뷰(메인 · 배터리 · 인버터 · 모터 · 수신 · DAQ)를 본다.
// 0.5 s 마다 받은 상태를 그리기만 한다. 다시 계산하지 않는다. 분기는 id · status · level · kind 로만 (문구는 엔진 것).
// 뷰를 바꿔도 이 화면은 그대로 있어 그래프 값(최근 5분)이 이어진다. 서버 응답이 끊기면 화면 전체를 낡음으로 돌린다

import { type JSX, useContext, useEffect, useMemo, useState } from 'react'
import { Link, Navigate, NavLink, useNavigate, useParams } from 'react-router-dom'

import { CARS, carOf, DEFAULT_CAR, type LiveCar, VIEW_NAMES, type ViewKey } from '../live/cars'
import { foldEvents, offState, SPARK, sparkPoints, tileOr, trail } from '../live/model'
import { AlertBand, StatusStrip } from '../live/parts'
import { useLive } from '../live/useLive'
import { useLiveTrack } from '../live/useLiveTrack'
import { BatteryView, GraphView, LinkView, MainView, PowertrainView, type ViewCtx } from '../live/views'
import { ThemeContext } from '../themeContext'

const VIEWS: Record<ViewKey, (p: { c: ViewCtx }) => JSX.Element> = {
  main: MainView,
  graph: GraphView,
  battery: BatteryView,
  powertrain: PowertrainView,
  link: LinkView,
}

function useNarrow(): boolean {
  const q = '(max-width: 860px)'
  const [narrow, setNarrow] = useState(() => window.matchMedia(q).matches)
  useEffect(() => {
    const m = window.matchMedia(q)
    const on = () => setNarrow(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
  return narrow
}

export default function Live() {
  const { car: carParam, view: viewParam } = useParams()
  const car = carOf(carParam)
  const theme = useContext(ThemeContext)
  const narrow = useNarrow()
  const live = useLive(car?.live === 'on')
  const { buf, error, frozenFor } = live
  // 수신이 꺼져 있으면 아무것도 안 받은 틀을 회색으로 보인다 (켜지면 다음 묻기에서 넘어간다)
  const off = !live.state && error?.code === 'live_off'
  const state = useMemo(() => live.state ?? (off ? offState() : null), [live.state, off])
  const frozen = live.frozen && !off
  const track = useLiveTrack(!!live.state, live.state?.track_rev) // 달린 궤적이 늘면 다시 받는다 (10 s 에 한 번까지)
  const tiles = useMemo(() => new Map((state?.tiles ?? []).map((t) => [t.id, t])), [state])
  const path = useMemo(() => (state ? trail(buf, state.now) : []), [buf, state])
  const events = useMemo(() => foldEvents(state?.events ?? []), [state])

  if (!carParam) return <Navigate to={`/live/${DEFAULT_CAR}`} replace />
  if (!car)
    return (
      <div className="page-msg">
        <h1>모르는 차량 · {carParam}</h1>
        <p>
          <Link to={`/live/${DEFAULT_CAR}`}>{DEFAULT_CAR} 실시간으로</Link>
        </p>
      </div>
    )
  const views = car.views ?? []
  const view = views.find((v) => v === viewParam)
  if (car.live === 'on' && !view) return <Navigate to={`/live/${car.id}/${views[0]}`} replace />

  const nav = <CarNav car={car} views={views} />
  if (car.live !== 'on') return <div className="live">{nav}<NotLive car={car} /></div>

  if (!state) {
    return (
      <div className="live">
        {nav}
        {error ? (
          <div className="page-msg">
            <h1>실시간 값을 불러올 수 없음</h1>
            <p>{error.message}</p>
          </div>
        ) : (
          <div className="page-msg muted">실시간 값 불러오는 중…</div>
        )}
      </div>
    )
  }

  const ctx: ViewCtx = {
    s: state, car, buf, frozen, off, theme, narrow, tiles, events, path,
    tile: (id) => tileOr(tiles, id),
    spark: (id) => (SPARK.has(id) ? sparkPoints(buf, id, state.now) : null),
    track: track.data ?? null,
    trackMsg: off ? '수신이 켜지면 경기장 · 지금 위치 표시' : track.error ? track.error.message : '경기장 불러오는 중…',
  }
  const View = VIEWS[view!]
  return (
    <div className={`live live-${view}`}>
      {off && <div className="live-band off">{error?.message} · 화면 틀만 표시</div>}
      {state.source.replay && <div className="live-band replay">재생 중 — 실제 주행 아님</div>}
      {frozen && (
        <div className="live-band frozen">
          화면 갱신 멈춤 — 서버 응답 {Math.round(frozenFor ?? 0)} s 없음{error ? ` (${error.message})` : ''} · 아래 값은 그 전 값
        </div>
      )}
      {!off && state.car && state.car !== car.id && <div className="live-band off">이 서버가 받는 차량은 {state.car} — 아래 값은 {state.car} 것</div>}
      {nav}
      <StatusStrip s={state} car={car} frozen={frozen} />
      <AlertBand s={state} car={car} view={view!} frozen={frozen} />
      <View c={ctx} />
    </div>
  )
}

// 차량 고르기 · 뷰 탭 (워크북처럼 — 차를 고르고 그 안에서 뷰)
function CarNav({ car, views }: { car: LiveCar; views: ViewKey[] }) {
  const go = useNavigate()
  return (
    <div className="live-nav">
      <label className="live-car">
        <span className="muted">차량</span>
        <select value={car.id} onChange={(e) => go(`/live/${e.target.value}`)}>
          {CARS.map((c) => (
            <option key={c.id} value={c.id}>
              {c.id}
            </option>
          ))}
        </select>
      </label>
      {views.length > 0 && (
        <nav className="live-views" aria-label="뷰">
          {views.map((v) => (
            <NavLink key={v} to={`/live/${car.id}/${v}`} className={({ isActive }) => `live-view-tab${isActive ? ' active' : ''}`}>
              {VIEW_NAMES[v]}
            </NavLink>
          ))}
        </nav>
      )}
    </div>
  )
}

// 실시간으로 못 받는 차 — LEF-26 송출 불가, LEF-27 구현 중 (2026-11-03 사용자 결정)
function NotLive({ car }: { car: LiveCar }) {
  return (
    <div className="live-notlive">
      <div className="live-notlive-title">{car.live === 'none' ? `${car.id} 실시간 송출 불가` : `${car.id} 실시간`}</div>
      <div className="muted">{car.live === 'none' ? '준비 중' : '구현 중'}</div>
      {car.plan && <div className="muted live-notlive-plan">예정 뷰 · {car.plan}</div>}
      {car.live === 'none' && <Link to="/sessions">{car.id} 주행 기록은 세션 목록에서</Link>}
    </div>
  )
}
