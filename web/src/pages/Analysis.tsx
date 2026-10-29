// 보드 3 · 분석 — 도구 줄(세션 · 기준 · 비교 · 가로축 · 확대 초기화 · 링크 복사), 왼쪽 채널 목록, 가운데 그래프 축,
// 오른쪽 트랙맵 · 커서 값 · g-g, 아래 랩 표. 상태는 주소에 (analysis/state.ts), 커서 A · B 와 트랙맵 색은 이 화면에서만

import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'

import { getJson, useApi } from '../api'
import ChannelList from '../analysis/ChannelList'
import { newZoomGroup, redrawCursors, resetZoom } from '../analysis/Chart'
import CursorTable from '../analysis/CursorTable'
import GG from '../analysis/GG'
import LapPicker from '../analysis/LapPicker'
import Plots from '../analysis/Plots'
import TrackMap, { type MapMode } from '../analysis/TrackMap'
import { addToAxis, lastAnalysis, MAX_AXES, readState, recentSessions, remember, resolveState, toggleChannel, writeState, type AnalysisState, type XMode } from '../analysis/state'
import { DELTA, tracesFor, type Trace } from '../analysis/traces'
import { Toast, type Notice } from '../overview/Download'
import { formatLapTime, sessionName } from '../sessions/format'
import { ThemeContext } from '../themeContext'
import type { Channel, DeltaResponse, LapTable, QualityItem, Resolved, SectorsResponse, SeriesResponse, SessionPage, SessionRow, TrackResponse } from '../types'

const MAX_POINTS = 2000
const MAX_CHANNELS = 16 // 엔진이 한 번에 받는 채널 수 (계약 F5)
const DEFAULT_AXES = 6

// 시간 차 — 엔진의 get_delta 를 그리는 화면용 채널 (거리축 · 비교 랩이 있을 때만 값이 있다)
const DELTA_CHANNEL: Channel = {
  channel_id: DELTA, block: 'calc', label: '시간 차 (비교 − 기준)', unit: 's', sign: '+ = 비교 랩이 그만큼 늦다', rate_hz: 0,
  source: { recording_id: null, device: '엔진 계산 (get_delta)' }, derived: true, derivation: '기준 거리마다 누적 시간 차', t_range: [0, 0],
  cautions: [], digits: 3,
}

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

// 처음 열 때의 축 — 엔진이 블록마다 제안한 채널(default_views)을 블록 순서대로, 채널 하나에 축 하나
function defaultAxes(page: SessionPage, x: XMode, available: Set<string>): string[][] {
  const pick = (k: 'time' | 'distance') => page.blocks.flatMap((b) => b.default_views?.[k] ?? [])
  const ids = [...new Set([...(x === 'distance' ? pick('distance') : []), ...pick('time')])].filter((c) => available.has(c))
  return ids.slice(0, DEFAULT_AXES).map((c) => [c])
}

// 계열마다 그래프 값 한 번씩 (같은 채널 묶음) — 주소가 바뀌면 앞선 요청은 끊는다
function useSeriesSet(sid: string, traces: Trace[], channels: string[], x: XMode) {
  const [state, setState] = useState<{ data: Map<string, SeriesResponse>; error: string | null; loading: boolean }>({ data: new Map(), error: null, loading: false })
  const key = JSON.stringify([sid, traces.map((t) => [t.key, t.lap]), channels, x])
  useEffect(() => {
    if (!channels.length || !traces.length) return setState({ data: new Map(), error: null, loading: false })
    const ctl = new AbortController()
    setState((s) => ({ ...s, loading: true, error: null }))
    Promise.all(
      traces.map((t) => {
        // 거리축은 기준 거리(ref_distance) — 시간 차 · 구간 · 트랙맵과 같은 축이라 랩끼리 위치가 맞는다 (계약 §4 · F5)
        const q = new URLSearchParams([...channels.map((c): [string, string] => ['ch', c]), ['x', x === 'distance' ? 'ref_distance' : 'time'],
                                       ['max_points', String(MAX_POINTS)]])
        if (t.lap !== null) q.set('lap', String(t.lap))
        return getJson<SeriesResponse>(`/api/sessions/${encodeURIComponent(sid)}/series?${q}`, ctl.signal).then((r) => [t.key, r] as const)
      }),
    ).then(
      (pairs) => setState({ data: new Map(pairs), error: null, loading: false }),
      (e: unknown) => !ctl.signal.aborted && setState({ data: new Map(), error: e instanceof Error ? e.message : String(e), loading: false }),
    )
    return () => ctl.abort()
  }, [key]) // key 가 sid · 계열 · 채널 · 가로축을 다 담는다
  return state
}

// 비교 랩마다 기준 랩과의 시간 차 (F14)
function useDeltas(sid: string, ref: number | 'all', traces: Trace[], on: boolean) {
  const [data, setData] = useState<Map<string, DeltaResponse>>(new Map())
  const key = JSON.stringify([sid, ref, traces.map((t) => [t.key, t.lap]), on])
  useEffect(() => {
    const cmp = traces.filter((t) => t.slot > 0 && t.lap !== null)
    if (!on || ref === 'all' || !cmp.length) return setData(new Map())
    const ctl = new AbortController()
    Promise.all(
      cmp.map((t) =>
        getJson<DeltaResponse>(`/api/sessions/${encodeURIComponent(sid)}/delta?lap=${t.lap}&ref_lap=${ref}&max_points=${MAX_POINTS}`, ctl.signal).then(
          (d) => [t.key, d] as const,
        ),
      ),
    ).then((pairs) => setData(new Map(pairs)), () => !ctl.signal.aborted && setData(new Map()))
    return () => ctl.abort()
  }, [key]) // key 가 sid · 기준 · 비교 랩을 다 담는다
  return data
}

export default function Analysis() {
  const [params, setParams] = useSearchParams()
  const st = readState(params)
  if (!st.sid) {
    const last = lastAnalysis()
    return last ? <Navigate to={`/analysis?${last}`} replace /> : <Pick />
  }
  return <Workspace key={st.sid} st={st} setState={(s) => setParams(writeState(s), { replace: true })} search={params.toString()} />
}

function Workspace({ st, setState, search }: { st: AnalysisState; setState: (s: AnalysisState) => void; search: string }) {
  const sid = st.sid!
  const base = `/api/sessions/${encodeURIComponent(sid)}`
  const page = useApi<SessionPage>(base)
  const chans = useApi<Channel[]>(`${base}/channels`)
  const laps = useApi<LapTable>(`${base}/laps`)
  const quality = useApi<QualityItem[]>(`${base}/quality`)
  const sessions = useApi<SessionRow[]>('/api/sessions')
  const theme = useContext(ThemeContext)
  const group = useRef(newZoomGroup()).current
  const [notice, setNotice] = useState<Notice | null>(null)
  const closeNotice = useCallback(() => setNotice(null), [])
  const [cur, setCur] = useState<{ a: number | null; b: number | null }>({ a: null, b: null })
  const [mapMode, setMapMode] = useState<MapMode | null>(null)
  group.onPick = (v, which) => setCur((c) => ({ ...c, [which]: v }))

  useEffect(() => remember(sid, search), [sid, search])

  const colors = useMemo(() => [0, 1, 2].map((i) => cssVar(`--trace-${i}`)), [theme])
  const channelMap = useMemo(() => new Map([...(chans.data ?? []), DELTA_CHANNEL].map((c) => [c.channel_id, c])), [chans.data])
  const lapNums = useMemo(() => (laps.data?.rows ?? []).map((r) => r.lap), [laps.data])
  const { x, ref, cmp } = resolveState(st, lapNums, page.data?.best_lap ?? null)
  const axes = useMemo(
    () => (st.axes ?? (page.data ? defaultAxes(page.data, x, new Set(channelMap.keys())) : [])).map((a) => a.filter((c) => channelMap.has(c))).filter((a) => a.length),
    [st.axes, page.data, x, channelMap],
  )
  const cmpKey = cmp.join(',')
  const traces = useMemo(() => tracesFor(ref, cmpKey ? cmpKey.split(',').map(Number) : []), [ref, cmpKey])
  const needed = useMemo(() => [...new Set(axes.flat())].filter((c) => c !== DELTA).slice(0, MAX_CHANNELS), [axes])
  const series = useSeriesSet(sid, traces, needed, x)
  const set = (patch: Partial<AnalysisState>) => setState({ ...st, x, ref, cmp, axes, ...patch })

  // 오른쪽 패널 — 트랙맵(코스가 있는 세션) · 구간 · 시간 차 · g-g
  const hasTrack = page.data?.summary.has_track ?? false
  const hasLaps = lapNums.length > 0 && ref !== 'all'
  const mode: MapMode = mapMode ?? (hasLaps ? 'pace' : 'ch:gps.speed_kmh')
  const track = useApi<TrackResponse>(hasTrack && laps.data ? `${base}/track?lap=${ref}` : null)
  const colored = useApi<TrackResponse>(hasTrack && laps.data && mode.startsWith('ch:') ? `${base}/track?lap=${ref}&color=${encodeURIComponent(mode.slice(3))}` : null)
  const sectors = useApi<SectorsResponse>(hasTrack && hasLaps ? `${base}/sectors` : null)
  const deltas = useDeltas(sid, ref, traces, x === 'distance' || mode === 'delta')
  const hasGG = channelMap.has('imu.lat_g') && channelMap.has('imu.long_g')
  const gg = useApi<SeriesResponse>(hasGG && laps.data ? `${base}/series?ch=imu.lat_g&ch=imu.long_g&x=time&max_points=1500&smooth_s=0.5${ref === 'all' ? '' : `&lap=${ref}`}` : null)

  // 커서는 그래프의 x 단위라 가로축 · 기준 랩이 바뀌면 지운다
  useEffect(() => setCur({ a: null, b: null }), [x, ref])
  useEffect(() => {
    group.cursorA = cur.a
    group.cursorB = cur.b
    redrawCursors(group)
  }, [cur, group])

  const retired = page.error?.code === 'session_retired' ? (page.error.detail as Resolved) : null
  if (retired?.current.length === 1) return <Navigate to={`/analysis?${writeState({ ...st, sid: retired.current[0] })}`} replace />
  if (page.error)
    return (
      <div className="page-msg">
        <h1>{retired ? '세션이 바뀌었습니다' : '세션을 열 수 없습니다'}</h1>
        <p>{page.error.message}</p>
        <p>
          <Link to={`/sessions/${encodeURIComponent(sid)}`}>세션 개요에서 보기</Link> · <Link to="/sessions">세션 목록으로</Link>
        </p>
      </div>
    )
  if (!page.data || !chans.data || !laps.data) return <div className="page-msg muted">세션 불러오는 중…</div>

  const s = page.data.summary
  const refRow = ref === 'all' ? null : laps.data.rows.find((r) => r.lap === ref) ?? null
  const lapStart = refRow ? refRow.t_start_s : 0
  const lapEnd = refRow ? refRow.t_end_s : s.duration_s
  const mapChannels = [...new Set(['gps.speed_kmh', ...page.data.blocks.map((b) => b.default_views?.map).filter((c): c is string => !!c)])].filter((c) => channelMap.has(c))
  const shownChannels = axes.flat().map((c) => channelMap.get(c)).filter((c): c is Channel => !!c)
  const listChannels = x === 'distance' && cmp.length ? [...chans.data, DELTA_CHANNEL] : chans.data
  const lapRow = (lap: number | null) => laps.data!.rows.find((r) => r.lap === lap)
  const chip = (t: Trace) => {
    const r = lapRow(t.lap)
    const parts = [s.day_order ? `R${s.day_order}` : '', t.lap === null ? '세션 전체' : `랩 ${t.lap}`, s.driver_label.replace(/^드라이버\s*/, '')]
    return { label: parts.filter(Boolean).join(' · '), time: r ? formatLapTime(r.lap_time_s as number) : '', best: !!r?.best }
  }
  const reduced = series.data.get('ref')?.reduced
  const copyLink = () =>
    navigator.clipboard.writeText(window.location.href).then(
      () => setNotice({ title: '링크를 복사했습니다', body: '받는 사람도 같은 화면(세션 · 랩 · 축)을 보게 됩니다.', kind: 'info' }),
      () => setNotice({ title: '복사하지 못했습니다', body: '주소창의 주소를 직접 복사해 주세요.' }),
    )

  return (
    <div className="analysis">
      <div className="toolbar analysis-tools">
        <label htmlFor="sess">세션</label>
        <select id="sess" className="field sess-select" value={sid} onChange={(e) => setState({ sid: e.target.value, x: null, ref: null, cmp: [], axes: st.axes })}>
          {!(sessions.data ?? []).some((r) => r.sid === sid) && <option value={sid}>{sessionName(s, { withCar: true }).main}</option>}
          {(sessions.data ?? []).map((r) => {
            const n = sessionName(r, { withCar: true })
            return (
              <option key={r.sid} value={r.sid}>
                {n.main}
                {n.purpose ? ` · ${n.purpose}` : ''}
              </option>
            )
          })}
        </select>
        <span className="tool-sep" />
        {traces.map((t, i) => {
          const c = chip(t)
          return (
            <span key={t.key} className="lap-chip" style={{ borderColor: i === 0 ? colors[0] : undefined }} title={i === 0 ? '기준' : `비교 ${i}`}>
              <span className="lap-chip-line" style={{ background: colors[t.slot] }} />
              <span className="lap-chip-text">
                {c.label}
                {c.time && <> · <span className={c.best ? 'best' : undefined}>{c.time}</span></>}
              </span>
              {i > 0 && (
                <button type="button" className="lap-chip-x" aria-label={`비교 ${i} 빼기`} onClick={() => set({ cmp: cmp.filter((l) => l !== t.lap) })}>
                  ×
                </button>
              )}
            </span>
          )
        })}
        <span className="tool-sep" />
        <span className="muted">가로축</span>
        <div className="seg" role="group" aria-label="가로축">
          <button type="button" aria-pressed={x === 'time'} onClick={() => set({ x: 'time' })}>
            시간
          </button>
          <button type="button" aria-pressed={x === 'distance'} disabled={ref === 'all'} title={ref === 'all' ? '랩을 기준으로 골라야 거리축을 쓸 수 있습니다' : undefined}
                  onClick={() => set({ x: 'distance' })}>
            거리
          </button>
        </div>
        <button type="button" className="btn" onClick={() => resetZoom(group)}>
          확대 초기화
        </button>
        <span style={{ flex: 1 }} />
        <Link className="btn btn-link" to={`/sessions/${encodeURIComponent(sid)}`}>
          세션 개요
        </Link>
        <button type="button" className="btn" onClick={copyLink}>
          이 화면 링크 복사
        </button>
      </div>

      <div className="analysis-main">
        <ChannelList channels={listChannels} blockTitles={new Map([...s.data_status.map((d): [string, string] => [d.block, d.title]), ['calc', '화면 계산 (엔진 값)']])}
                     shown={new Set(axes.flat())}
                     onToggle={(c) => set({ axes: toggleChannel(axes, c) })} />
        <div className="analysis-center">
          {series.error && <div className="banner">그래프 값을 불러오지 못했습니다 — {series.error}</div>}
          <Plots axes={axes} channels={channelMap} traces={traces} responses={series.data} deltas={deltas} x={x} quality={quality.data ?? []} colors={colors}
                 theme={theme} group={group} onRemove={(i) => set({ axes: axes.filter((_, k) => k !== i) })} onAdd={(i, c) => set({ axes: addToAxis(axes, i, c) })} />
        </div>
        <aside className="side" aria-label="트랙맵 · 커서 값 · g-g">
          {hasTrack && (
            <section className="side-block">
              <h2 className="side-head">
                <span>트랙맵 · {ref === 'all' ? '세션 전체' : `기준 랩 ${ref}`}</span>
                <label htmlFor="mapc" className="muted">
                  색
                </label>
                <select id="mapc" className="field" value={mode} onChange={(e) => setMapMode(e.target.value as MapMode)}>
                  {hasLaps && <option value="pace">구간 시간</option>}
                  {hasLaps && cmp.length > 0 && <option value="delta">시간 차 (비교 1)</option>}
                  {mapChannels.map((c) => (
                    <option key={c} value={`ch:${c}`}>
                      {channelMap.get(c)!.label}
                    </option>
                  ))}
                </select>
              </h2>
              {track.data ? (
                <TrackMap track={track.data} colored={colored.data} mode={mode} sectors={sectors.data} delta={deltas.get('cmp0') ?? null}
                          refLap={ref === 'all' ? null : ref} x={x} lapStart={lapStart} group={group} cursorA={cur.a} cursorB={cur.b} theme={theme} height={230} />
              ) : (
                <p className="panel-msg muted">{track.error ? `코스를 불러오지 못했습니다 — ${track.error.message}` : '코스 불러오는 중…'}</p>
              )}
              <MapLegend mode={mode} colored={colored.data} channel={mode.startsWith('ch:') ? channelMap.get(mode.slice(3)) : undefined} delta={deltas.get('cmp0')} />
            </section>
          )}
          <CursorTable channels={shownChannels} traces={traces} responses={series.data} deltas={deltas} x={x} colors={colors} a={cur.a} b={cur.b} />
          {hasGG && gg.data && (
            <GG gg={gg.data} track={track.data} x={x} lapStart={lapStart} lapEnd={lapEnd} a={cur.a} onA={(v) => setCur((c) => ({ ...c, a: v }))} group={group} theme={theme} />
          )}
        </aside>
      </div>

      {lapNums.length > 0 ? (
        <LapPicker table={laps.data} refLap={ref} cmp={cmp} colors={colors} onRef={(r) => set({ ref: r, x: r === 'all' ? 'time' : x, cmp: r === 'all' ? [] : cmp.filter((c) => c !== r) })}
                   onCmp={(c) => set({ cmp: c })} />
      ) : (
        <p className="lap-picker-empty muted">{s.has_track ? '랩이 없는 세션입니다 — 세션 전체를 시간축으로 봅니다.' : '코스(위치) 기록이 없는 세션입니다 — 세션 전체를 시간축으로 봅니다.'}</p>
      )}
      <div className="analysis-status muted">
        {series.loading ? '그래프 값 불러오는 중…' : reduced ? `기준 랩 표시 ${reduced.to_points.toLocaleString('en-US')}점 / 원래 ${reduced.from_points.toLocaleString('en-US')}점 · 줄여도 최소 · 최대 보존` : ''}
        {axes.length >= MAX_AXES && ` · 축은 ${MAX_AXES}개까지`}
      </div>
      <Toast notice={notice} onClose={closeNotice} />
    </div>
  )
}

// 세션을 고르지 않고 들어왔을 때 — 최근 연 세션 · 최신 세션
function Pick() {
  const sessions = useApi<SessionRow[]>('/api/sessions')
  const all = sessions.data ?? []
  const recent = recentSessions().map((sid) => all.find((r) => r.sid === sid)).filter((r): r is SessionRow => !!r)
  const item = (r: SessionRow) => {
    const n = sessionName(r, { withCar: true })
    return (
      <li key={r.sid}>
        <Link to={`/analysis?sid=${encodeURIComponent(r.sid)}`}>{n.main}</Link>
        {n.purpose && <span className="muted"> · {n.purpose}</span>}
      </li>
    )
  }
  return (
    <div className="page-msg">
      <h1>분석할 세션을 고르세요</h1>
      {recent.length > 0 && (
        <>
          <p className="muted">최근 연 세션</p>
          <ul>{recent.map(item)}</ul>
        </>
      )}
      <p className="muted">최신 세션</p>
      <ul>{all.slice(0, 12).map(item)}</ul>
      <p>
        <Link to="/sessions">세션 목록에서 고르기</Link>
      </p>
    </div>
  )
}

// 트랙맵 아래 범례 — 색 기준마다 읽는 법 (값은 엔진 것, 색만 화면)
function MapLegend({ mode, colored, channel, delta }: { mode: MapMode; colored: TrackResponse | null; channel?: Channel; delta?: DeltaResponse }) {
  if (mode === 'pace')
    return (
      <p className="map-legend muted">
        <span className="best">보라</span> 세션 최속 · <span className="pace-good">초록</span> 평균보다 빠름 · <span className="pace-bad">빨강</span> 느림 — 미니 구간 25 m, 평균은 완주 랩
      </p>
    )
  if (mode === 'delta')
    return (
      <p className="map-legend muted">
        <span className="pace-good">초록</span> 비교 1 이 빠른 미니 구간 · <span className="pace-bad">빨강</span> 느린 곳
        {delta && ` · 랩 끝 차이 ${delta.total_s > 0 ? '+' : ''}${delta.total_s.toFixed(2)} s`}
      </p>
    )
  const c = colored?.color
  if (!c || !channel) return null
  if (c.levels)
    return <p className="map-legend muted">{c.levels.map((l) => l.label).join(' · ')} — 초록 · 회색 · 빨강</p>
  const vals = c.values.filter((v): v is number => v !== null)
  return (
    <p className="map-legend muted">
      {channel.label} {vals.length ? `${Math.min(...vals).toFixed(channel.digits > 1 ? 1 : channel.digits)} ~ ${Math.max(...vals).toFixed(channel.digits > 1 ? 1 : channel.digits)}` : ''} {c.unit}
      {' '}— 파랑 낮음 · 빨강 높음
    </p>
  )
}
