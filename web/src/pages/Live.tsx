// 실시간 화면 (계약 §12, 실시간화면_v1.md) — 0.5 s 마다 받은 상태를 그리기만 한다. 다시 계산하지 않는다.
// 분기는 id · status · level · kind 로만 (문구는 엔진 것). 서버 응답이 끊기면 화면 전체를 낡음으로 돌린다

import { useContext, useMemo } from 'react'

import { useApi } from '../api'
import LiveChart from '../live/LiveChart'
import LiveMap from '../live/LiveMap'
import {
  ageText, clock, deviceView, gaugePos, kindOf, lapText, linkView, offState, onOff, outageText, PANELS, rateText, receiverView, scaleOf, shownStatus, SPARK, TILE_KIND,
  sparkPoints, STATUS_WORD, type TileKind, tileValue, trail,
} from '../live/model'
import { useLive } from '../live/useLive'
import { ThemeContext } from '../themeContext'
import { formatValue } from '../overview/model'
import type { LiveScale, LiveState, LiveStatus, LiveTile, LiveTrack } from '../types'

export default function Live() {
  const theme = useContext(ThemeContext)
  const live = useLive()
  const { buf, error, frozenFor } = live
  // 수신이 꺼져 있으면 아무것도 안 받은 틀을 회색으로 보인다 (켜지면 다음 묻기에서 넘어간다)
  const off = !live.state && error?.code === 'live_off'
  const state = useMemo(() => live.state ?? (off ? offState() : null), [live.state, off])
  const frozen = live.frozen && !off
  const track = useApi<LiveTrack>(live.state ? '/api/live/track' : null)
  const tiles = useMemo(() => new Map((state?.tiles ?? []).map((t) => [t.id, t])), [state])
  const path = useMemo(() => (state ? trail(buf, state.now) : []), [buf, state])

  if (!state) {
    if (error)
      return (
        <div className="page-msg">
          <h1>실시간 값을 불러올 수 없음</h1>
          <p>{error.message}</p>
        </div>
      )
    return <div className="page-msg muted">실시간 값 불러오는 중…</div>
  }

  const pos = state.position
  return (
    <div className="live">
      {off && <div className="live-band off">{error?.message} · 화면 틀만 표시</div>}
      {state.source.replay && <div className="live-band replay">재생 중 — 실제 주행 아님</div>}
      {frozen && (
        <div className="live-band frozen">
          화면 갱신 멈춤 — 서버 응답 {Math.round(frozenFor ?? 0)} s 없음{error ? ` (${error.message})` : ''} · 아래 값은 그 전 값
        </div>
      )}
      <TopBar s={state} frozen={frozen} />

      <div className="live-grid">
        <section className="live-tiles" aria-label="지금 값">
          {(['big', 'gauge', 'small'] as const).map((k) => (
            <div key={k} className={`live-row ${k}`}>
              {/* 칸 배치를 정한 것만 — 엔진이 칸을 더 보내도 뷰 화면이 생기기 전까지는 이 11개 */}
              {state.tiles.filter((t) => t.id in TILE_KIND && kindOf(t.id) === k).map((t) => (
                <Tile key={t.id} t={t} kind={k} frozen={frozen} spark={SPARK.has(t.id) ? sparkPoints(buf, t.id, state.now) : null} />
              ))}
            </div>
          ))}
        </section>

        <section className="panel live-charts">
          <h2>최근 5분</h2>
          {PANELS.map((p) => (
            <LiveChart key={p.key} title={p.title} series={p.series} buf={buf} now={state.now} tiles={tiles} theme={theme} height={110} />
          ))}
        </section>

        <div className="live-side">
          <section className="panel live-map">
            <h2>트랙맵</h2>
            {track.data ? (
              <LiveMap track={track.data} trail={path} position={pos} fresh={!frozen && !!pos && pos.age_s <= 2} theme={theme} height={260} />
            ) : (
              <p className="panel-msg muted">{off ? '수신이 켜지면 경기장 · 지금 위치 표시' : track.error ? track.error.message : '경기장 불러오는 중…'}</p>
            )}
            <Laps laps={state.laps} />
          </section>
          <section className="panel">
            <h2>경고</h2>
            {state.alerts.length ? (
              <ul className="live-list">
                {state.alerts.map((a) => (
                  <li key={a.id} className={`live-alert ${frozen ? 'stale' : a.level}`}>
                    <span className="live-dot" aria-hidden="true" />
                    <span>{a.text}</span>
                    {a.since !== null && <span className="muted">{clock(a.since)}부터</span>}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="panel-msg muted">켜진 경고 없음</p>
            )}
          </section>
          <Inverter inv={state.inverter} frozen={frozen} />
        </div>

        <section className="panel live-events">
          <h2>사건</h2>
          {state.events.length ? (
            <ul className="live-list">
              {state.events.map((e, i) => (
                <li key={`${e.t}-${i}`} className={`live-event ${e.kind}`}>
                  <span className="mono">{clock(e.t)}</span>
                  <span>{e.text}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="panel-msg muted">오늘 사건 없음</p>
          )}
        </section>
      </div>
    </div>
  )
}

// 맨 윗줄 — 수신 · 수신률 · 끊김 · 장치 · 수신기 · 원천별 표시등 (설계안 §4-2 "맨 윗줄은 언제나 수신 상태")
function TopBar({ s, frozen }: { s: LiveState; frozen: boolean }) {
  const link = linkView(s.link)
  const dev = deviceView(s.device)
  const rx = receiverView(s.receiver)
  const tone = (t: string) => (frozen && t !== 'none' ? 'stale' : t)
  return (
    <div className="live-top" role="status">
      <span className={`live-flag ${tone(link.tone)}`}>
        <span className="live-dot" aria-hidden="true" />
        {frozen ? '수신 — 화면 갱신 멈춤' : link.text}
      </span>
      <span>{rateText(s.link)}</span>
      <span>{outageText(s.link)}</span>
      <span className={`live-flag ${tone(dev.tone)}`}>
        <span className="live-dot" aria-hidden="true" />
        {dev.text}
      </span>
      <span className={`live-flag ${tone(rx.tone)}`}>
        <span className="live-dot" aria-hidden="true" />
        {rx.text}
      </span>
      <span className="live-sources">
        {s.sources.map((src) => (
          <span key={src.id} className={`live-flag ${tone(src.status)}`} title={src.age_s !== null ? ageText(src.age_s) : '받은 적 없음'}>
            <span className="live-dot" aria-hidden="true" />
            {src.label}
          </span>
        ))}
      </span>
      <span className="spacer" />
      <span className="muted">
        {s.car}
        {s.device.firmware && ` · ${s.device.firmware}`}
        {s.device.boots > 0 && ` · 부팅 ${s.device.boots}번`}
        {!!s.source.bad_rows && ` · 깨진 줄 ${s.source.bad_rows}`}
      </span>
    </div>
  )
}

// 타일 다섯 상태 — 위험 · 주의 · 정상은 색, 낡음은 회색 + 지난 초, 무수신은 빗금 + "—" (낡음 · 무수신이 먼저).
// 범위 · 한도가 있는 값은 게이지, 한계를 모르는 값은 최근 1분 추세선을 붙인다
function Tile({ t, kind, frozen, spark }: { t: LiveTile; kind: TileKind; frozen: boolean; spark: ([number, number] | null)[] | null }) {
  const st = shownStatus(t.status, frozen)
  const word = STATUS_WORD[st]
  const scale = kind === 'small' ? null : scaleOf(t)
  return (
    <div className={`live-tile ${kind} ${st}`} title={[t.id, t.sign && `부호: ${t.sign}`].filter(Boolean).join(' · ')}>
      <div className="live-tile-label">
        <span>{t.label}</span>
        {word && <span className="live-tile-word">{word}</span>}
      </div>
      <div className="live-tile-value">
        {tileValue(t)}
        {t.value !== null && <span className="live-tile-unit">{t.unit}</span>}
      </div>
      {scale && <Gauge v={t.value} s={scale} st={st} />}
      {spark && <Spark pts={spark} />}
      <div className="live-tile-age">{st === 'none' ? '수신 없음' : st === 'stale' ? (frozen ? '갱신 멈춤' : ageText(t.age_s)) : ''}</div>
    </div>
  )
}

// 가로 막대 게이지 — 채움은 0(범위에 0 이 있으면) 또는 최소에서 지금 값까지, 색은 타일 상태. 선은 주의(노랑) · 한도(빨강)
function Gauge({ v, s, st }: { v: number | null; s: LiveScale; st: LiveStatus }) {
  const base = s.min < 0 && s.max > 0 ? gaugePos(0, s) : 0
  const p = v === null ? null : gaugePos(v, s)
  const at = (x: number) => `${gaugePos(x, s) * 100}%`
  const fmt = (x: number) => formatValue(x, Number.isInteger(x) ? 0 : 1)
  const crowded = [s.warn, s.danger].some((m) => m !== null && gaugePos(m, s) > 0.88) // 끝 숫자와 겹치면 끝 숫자를 뺀다
  return (
    <div className="live-gauge">
      <div className="live-gauge-track">
        {p !== null && <div className={`live-gauge-fill ${st}`} style={{ left: `${Math.min(base, p) * 100}%`, width: `${Math.abs(p - base) * 100}%` }} />}
        {base > 0 && <i className="live-gauge-zero" style={{ left: `${base * 100}%` }} />}
        {s.warn !== null && <i className="live-gauge-mark warn" style={{ left: at(s.warn) }} />}
        {s.danger !== null && <i className="live-gauge-mark danger" style={{ left: at(s.danger) }} />}
      </div>
      <div className="live-gauge-scale">
        <span>{fmt(s.min)}</span>
        {s.warn !== null && <span className="mark warn" style={{ left: at(s.warn) }}>{fmt(s.warn)}</span>}
        {s.danger !== null && <span className="mark danger" style={{ left: at(s.danger) }}>{fmt(s.danger)}</span>}
        <span>{crowded ? '' : fmt(s.max)}</span>
      </div>
    </div>
  )
}

// 최근 1분 추세선 — 세로는 그 1분의 최소 ~ 최대 (모양만 본다, 눈금 없음)
function Spark({ pts }: { pts: ([number, number] | null)[] }) {
  if (!pts.length) return <div className="live-spark" />
  const runs: string[] = []
  let cur: string[] = []
  for (const q of pts) {
    if (q === null) {
      if (cur.length > 1) runs.push(cur.join(' '))
      cur = []
    } else cur.push(`${(q[0] * 100).toFixed(1)},${(22 - q[1] * 20).toFixed(1)}`)
  }
  if (cur.length > 1) runs.push(cur.join(' '))
  return (
    <svg className="live-spark" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true">
      {runs.map((r, i) => (
        <polyline key={i} points={r} />
      ))}
    </svg>
  )
}

function Laps({ laps }: { laps: LiveState['laps'] }) {
  if (!laps) return <p className="panel-msg muted">랩 — 계측선 통과 전</p>
  return (
    <div className="live-laps">
      <span>
        랩 <strong>{laps.count}</strong>
      </span>
      <span>이번 {lapText(laps.current_s)}</span>
      <span>직전 {lapText(laps.last_s)}</span>
      <span>
        최고 <span className="best">{lapText(laps.best_s)}</span>
      </span>
      <span className="live-laps-basis muted">{laps.basis}</span>
    </div>
  )
}

function Inverter({ inv, frozen }: { inv: LiveState['inverter']; frozen: boolean }) {
  const st = shownStatus(inv.status, frozen)
  return (
    <section className={`panel live-inverter ${st}`}>
      <h2>
        인버터
        {(st === 'stale' || st === 'none') && <span className="muted"> · {st === 'none' ? '수신 없음' : frozen ? '갱신 멈춤' : ageText(inv.age_s)}</span>}
      </h2>
      <dl className="live-facts">
        <dt>구동 허용</dt>
        <dd>{onOff(inv.enabled)}</dd>
        <dt>운전 준비</dt>
        <dd>{onOff(inv.ready)}</dd>
        <dt>오류</dt>
        <dd className={inv.errors.length ? 'live-err' : ''}>{inv.errors.length ? inv.errors.join(' · ') : '없음'}</dd>
        <dt>경고</dt>
        <dd className={inv.warnings.length ? 'live-warn-text' : ''}>{inv.warnings.length ? inv.warnings.join(' · ') : '없음'}</dd>
      </dl>
    </section>
  )
}
