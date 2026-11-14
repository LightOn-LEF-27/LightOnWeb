// 실시간 뷰들이 같이 쓰는 조각 — 타일 · 게이지 · 추세선 · 맨 윗줄 · 경고 띠 · 사건 · 랩 표 · 인버터.
// 그리기만 한다. 분기는 id · status · level · kind 로만 (문구는 엔진 것)

import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { formatValue } from '../overview/model'
import type { LiveLap, LiveScale, LiveSource, LiveState, LiveStatus, LiveTile } from '../types'
import { alertView, type LiveCar, VIEW_NAMES, type ViewKey } from './cars'
import {
  ageText, clock, deviceView, eventText, type EventRow, gaugePos, lapText, linkView, onOff, outageText, pctText, rateText,
  receiverView, scaleOf, shownStatus, signedText, STATUS_WORD, type TileKind, tileValue,
} from './model'

// ---------------------------------------------------------------- 타일

// 타일 다섯 상태 — 위험 · 주의 · 정상은 색, 낡음은 회색 + 지난 초, 무수신은 빗금 + "—" (낡음 · 무수신이 먼저).
// 범위 · 한도가 있는 값은 게이지(small 은 빼고), 한계를 모르는 값은 최근 1분 추세선
export function Tile({ t, kind, frozen, spark, sign, children }: {
  t: LiveTile
  kind: TileKind
  frozen: boolean
  spark?: ([number, number] | null)[] | null
  sign?: boolean // 부호 뜻을 칸 안에 (RPM 처럼 헷갈리는 것)
  children?: ReactNode // 칸 아래 덧붙임 (APPS 센서 등)
}) {
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
      {sign && t.sign && <div className="live-tile-sub muted">{t.sign}</div>}
      {children}
      <TileAge st={st} age={t.age_s} frozen={frozen} />
    </div>
  )
}

export function TileAge({ st, age, frozen }: { st: LiveStatus; age: number | null; frozen: boolean }) {
  return <div className="live-tile-age">{st === 'none' ? '수신 없음' : st === 'stale' ? (frozen ? '갱신 멈춤' : ageText(age)) : ''}</div>
}

// 가로 막대 게이지 — 채움은 0(범위에 0 이 있으면) 또는 최소에서 지금 값까지, 색은 타일 상태. 선은 주의(노랑) · 한도(빨강)
export function Gauge({ v, s, st }: { v: number | null; s: LiveScale; st: LiveStatus }) {
  const base = s.min < 0 && s.max > 0 ? gaugePos(0, s) : 0
  const p = v === null ? null : gaugePos(v, s)
  const at = (x: number) => `${gaugePos(x, s) * 100}%`
  const fmt = (x: number) => formatValue(x, Number.isInteger(x) ? 0 : 1)
  const crowded = [s.warn, s.danger].some((m) => m !== null && gaugePos(m, s) > 0.88) // 끝 숫자와 겹치면 끝 숫자를 뺀다
  // 주의 · 한도 숫자가 가까우면(폰에서 "5560" 처럼 붙는다) 주의는 선 왼쪽, 한도는 선 오른쪽으로 비켜 쓴다
  const close = s.warn !== null && s.danger !== null && Math.abs(gaugePos(s.warn, s) - gaugePos(s.danger, s)) < 0.12
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
        {s.warn !== null && <span className={`mark warn${close ? ' before' : ''}`} style={{ left: at(s.warn) }}>{fmt(s.warn)}</span>}
        {s.danger !== null && <span className={`mark danger${close ? ' after' : ''}`} style={{ left: at(s.danger) }}>{fmt(s.danger)}</span>}
        <span>{crowded ? '' : fmt(s.max)}</span>
      </div>
    </div>
  )
}

// 최근 1분 추세선 — 세로는 그 1분의 최소 ~ 최대 (모양만 본다, 눈금 없음)
export function Spark({ pts }: { pts: ([number, number] | null)[] }) {
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

// 작은 값 묶음 — 이름 · 값 · (낡으면) 지난 초. BMS 기타처럼 드물게 오는 값
export function Facts({ items, frozen }: { items: LiveTile[]; frozen: boolean }) {
  return (
    <div className="live-factgrid">
      {items.map((t) => {
        const st = shownStatus(t.status, frozen)
        return (
          <div key={t.id} className={`live-fact ${st}`} title={t.id}>
            <span className="live-fact-label">{t.label}</span>
            <span className="live-fact-value">
              {tileValue(t)}
              {t.value !== null && t.unit && <span className="live-tile-unit">{t.unit}</span>}
            </span>
            <TileAge st={st} age={t.age_s} frozen={frozen} />
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------- 맨 윗줄 · 경고 띠

// 맨 윗줄 — 수신 · 수신률 · 끊김 · 송신 장치 · 수신기 · 원천별 표시등 (설계안 §4-2 "맨 윗줄은 언제나 수신 상태")
export function StatusStrip({ s, car, frozen }: { s: LiveState; car: LiveCar; frozen: boolean }) {
  const link = linkView(s.link)
  const dev = deviceView(s.device, car.device)
  const rx = receiverView(s.receiver)
  const tone = (t: string) => (frozen && t !== 'none' ? 'stale' : t)
  return (
    <div className="live-top" role="status">
      <span className={`live-flag ${tone(link.tone)}`}>
        <span className="live-dot" aria-hidden="true" />
        {frozen ? '수신 — 화면 갱신 멈춤' : link.text}
      </span>
      <span title={`${car.device ?? '장치'} 메시지(/d) 수신률`}>{rateText(s.link)}</span>
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
    </div>
  )
}

// 켜진 경고 — 뷰와 상관없이 늘 맨 위에. 그 경고의 칸이 지금 뷰에 없으면 그 뷰로 가는 링크
export function AlertBand({ s, car, view, frozen }: { s: LiveState; car: LiveCar; view: ViewKey; frozen: boolean }) {
  if (!s.alerts.length) return null
  const top = frozen ? 'stale' : s.alerts[0].level
  return (
    <div className={`live-alertband ${top}`} role="alert">
      {s.alerts.map((a) => {
        const tile = s.tiles.find((t) => t.alert === a.id)?.id ?? null
        const go = alertView(a.id, tile, view, car.views ?? [])
        return (
          <span key={a.id} className={`live-alert ${frozen ? 'stale' : a.level}`}>
            <span className="live-dot" aria-hidden="true" />
            <span>{a.text}</span>
            {a.since !== null && <span className="muted">{clock(a.since)}부터</span>}
            {go && (
              <Link className="live-alert-go" to={`/live/${car.id}/${go}`}>
                {VIEW_NAMES[go]} 뷰
              </Link>
            )}
          </span>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------- 사건

export function EventList({ rows, limit, more }: { rows: EventRow[]; limit?: number; more?: ReactNode }) {
  const shown = limit ? rows.slice(0, limit) : rows
  return (
    <section className="panel live-events">
      <h2 className="live-h2">
        <span>{limit ? '최근 사건' : '사건 (오늘, 최근이 위)'}</span>
        {more}
      </h2>
      {shown.length ? (
        <ul className={`live-list${limit ? ' fixed' : ''}`}>
          {shown.map((e, i) => (
            <li key={`${e.t}-${i}`} className={`live-event ${e.kind}`}>
              <span className="mono">{clock(e.t)}</span>
              <span>{eventText(e)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="panel-msg muted">오늘 사건 없음</p>
      )}
    </section>
  )
}

// ---------------------------------------------------------------- 랩

// 랩 수 · Current · Last · Best — 이름은 레이싱에서 쓰는 영어 그대로 (사용자 결정)
export function LapStrip({ laps }: { laps: LiveState['laps'] }) {
  if (!laps) return <p className="panel-msg muted">랩 — 계측선 통과 전</p>
  return (
    <div className="live-lapstrip">
      <div>
        <div className="k">랩</div>
        <div className="v">{laps.count}</div>
      </div>
      <div>
        <div className="k">Current</div>
        <div className="v">{lapText(laps.current_s)}</div>
      </div>
      <div>
        <div className="k">Last</div>
        <div className="v">{lapText(laps.last_s)}</div>
      </div>
      <div>
        <div className="k">Best</div>
        <div className="v best">{lapText(laps.best_s)}</div>
      </div>
      <div className="live-lapstrip-basis muted">{laps.basis}</div>
    </div>
  )
}

const pctDelta = (v: number | null | undefined) => (v == null ? '—' : `${signedText(v, 1)} %`)

const isBest = (l: LiveLap, laps: LiveState['laps']) => laps?.best_s != null && Math.abs(l.lap_time_s - laps.best_s) < 0.05

// 쓴 에너지 — 전력 값에 빈 곳이 있으면(cover < 1) 작게 나오므로 "일부" 를 붙인다 (계약 §12-4)
export function WhCell({ l }: { l: LiveLap | null | undefined }) {
  if (!l || l.net_wh == null) return <span>—</span>
  const part = l.cover != null && l.cover < 0.999
  return (
    <span title={part ? `전력 값이 이어진 시간 ${Math.round((l.cover ?? 0) * 100)} % — 빈 곳은 적분하지 않아 작게 나옴` : undefined}>
      {formatValue(l.net_wh, 0)} Wh{part && <span className="live-part"> 일부</span>}
    </span>
  )
}

// 메인 — 최근 랩 (최근이 위, 3줄)
export function RecentLaps({ laps }: { laps: LiveState['laps'] }) {
  const rows = (laps?.rows ?? []).slice(-3).reverse()
  return (
    <section className="panel">
      <h2>최근 랩</h2>
      {rows.length ? (
        <table className="live-table">
          <thead>
            <tr>
              <th>랩</th>
              <th>시간</th>
              <th>Last 대비</th>
              <th>최고 속도</th>
              <th>셀 최고 온도</th>
              <th>SOC 끝</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((l) => (
              <tr key={l.lap}>
                <td>{l.lap}</td>
                <td className={isBest(l, laps) ? 'best' : ''}>{lapText(l.lap_time_s)}</td>
                <td>{l.lap_delta_s == null ? '—' : `${signedText(l.lap_delta_s, 1)} s`}</td>
                <td>{l.speed_max_kmh == null ? '—' : `${formatValue(l.speed_max_kmh, 0)} km/h`}</td>
                <td>{l.cell_t_max_c == null ? '—' : `${formatValue(l.cell_t_max_c, 0)} °C`}</td>
                <td>{l.soc_end_pct == null ? '—' : `${formatValue(l.soc_end_pct, 1)} %`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="panel-msg muted">{laps ? '끝난 랩 없음' : '랩 — 계측선 통과 전'}</p>
      )}
    </section>
  )
}

// 메인 — 이번 랩 · 지난 랩 에너지
export function LapEnergy({ laps }: { laps: LiveState['laps'] }) {
  const cur = laps?.current ?? null
  const last = laps?.rows?.length ? laps.rows[laps.rows.length - 1] : null
  const row = (name: string, l: LiveLap | null, strong: boolean) => (
    <tr className={strong ? 'strong' : ''}>
      <td className="muted">
        {name}
        {l ? ` (${l.lap})` : ''}
      </td>
      <td>
        <WhCell l={l} />
      </td>
      <td>{pctDelta(l?.soc_delta_pct)}</td>
    </tr>
  )
  return (
    <section className="panel">
      <h2 title={laps?.energy_basis}>이번 랩 · 지난 랩 에너지</h2>
      {laps ? (
        <table className="live-table">
          <thead>
            <tr>
              <th />
              <th>쓴 에너지</th>
              <th>SOC 변화</th>
            </tr>
          </thead>
          <tbody>
            {row('이번', cur, true)}
            {row('지난', last, false)}
          </tbody>
        </table>
      ) : (
        <p className="panel-msg muted">랩 — 계측선 통과 전</p>
      )}
    </section>
  )
}

// 배터리 — 랩별 배터리 (이번 랩 + 끝난 랩, 최근이 위)
export function BatteryLaps({ laps }: { laps: LiveState['laps'] }) {
  const done = (laps?.rows ?? []).slice().reverse()
  const cur = laps?.current ?? null
  const line = (l: LiveLap, now: boolean) => (
    <tr key={`${now ? 'c' : 'd'}${l.lap}`} className={now ? 'strong' : ''}>
      <td>{now ? `이번 (${l.lap})` : l.lap}</td>
      <td>
        <WhCell l={l} />
      </td>
      <td>{pctDelta(l.soc_delta_pct)}</td>
      <td>{l.cell_v_min == null ? '—' : `${formatValue(l.cell_v_min, 3)} V`}</td>
      <td>{l.cell_t_max_c == null ? '—' : `${formatValue(l.cell_t_max_c, 0)} °C`}</td>
    </tr>
  )
  return (
    <section className="panel live-batlaps">
      <h2 title={laps?.energy_basis}>랩별 배터리</h2>
      {laps && (cur || done.length) ? (
        <div className="live-table-scroll">
          <table className="live-table">
            <thead>
              <tr>
                <th>랩</th>
                <th>쓴 에너지</th>
                <th>SOC 변화</th>
                <th>셀 최저 전압</th>
                <th>셀 최고 온도</th>
              </tr>
            </thead>
            <tbody>
              {cur && line(cur, true)}
              {done.map((l) => line(l, false))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="panel-msg muted">랩 — 계측선 통과 전</p>
      )}
      {laps?.energy_basis && <p className="live-note muted">{laps.energy_basis}</p>}
    </section>
  )
}

// ---------------------------------------------------------------- 인버터 · APPS

function staleNote(st: LiveStatus, age: number | null, frozen: boolean) {
  if (st !== 'stale' && st !== 'none') return null
  return <span className="muted"> · {st === 'none' ? '수신 없음' : frozen ? '갱신 멈춤' : ageText(age)}</span>
}

// 메인 — 한 줄: ENA · RDY · 오류
export function InverterLine({ inv, frozen }: { inv: LiveState['inverter']; frozen: boolean }) {
  const st = shownStatus(inv.status, frozen)
  const lamp = (v: boolean | null) => (v === null || st === 'stale' || st === 'none' ? 'none' : v ? 'ok' : 'off')
  return (
    <section className={`panel live-inverter ${st}`}>
      <h2>
        인버터
        {staleNote(st, inv.age_s, frozen)}
      </h2>
      <div className="live-invline">
        <span className={`live-flag ${lamp(inv.enabled)}`}>
          <span className="live-dot" aria-hidden="true" />
          ENA
        </span>
        <span className={`live-flag ${lamp(inv.ready)}`}>
          <span className="live-dot" aria-hidden="true" />
          RDY
        </span>
        <span className={inv.errors.length ? 'live-err' : 'muted'}>{st === 'none' ? '—' : inv.errors.length ? inv.errors.join(' · ') : '오류 없음'}</span>
      </div>
    </section>
  )
}

// 인버터 · 모터 뷰 — ENA · RDY 등 + 오류 · 경고 이름
export function InverterPanel({ inv, frozen }: { inv: LiveState['inverter']; frozen: boolean }) {
  const st = shownStatus(inv.status, frozen)
  const lamp = (name: string, v: boolean | null) => (
    <div className={`live-lamp ${v === null || st === 'stale' || st === 'none' ? 'none' : v ? 'ok' : 'off'}`}>
      <span className="live-dot" aria-hidden="true" />
      {name}
      <strong>{onOff(v)}</strong>
    </div>
  )
  return (
    <section className={`panel live-inverter ${st}`}>
      <h2>
        인버터 상태
        {staleNote(st, inv.age_s, frozen)}
      </h2>
      <div className="live-lamps">
        {lamp('ENA', inv.enabled)}
        {lamp('RDY', inv.ready)}
      </div>
      <dl className="live-facts">
        <dt>오류</dt>
        <dd className={inv.errors.length ? 'live-err' : ''}>{st === 'none' ? '—' : inv.errors.length ? inv.errors.join(' · ') : '없음'}</dd>
        <dt>경고</dt>
        <dd className={inv.warnings.length ? 'live-warn-text' : ''}>{st === 'none' ? '—' : inv.warnings.length ? inv.warnings.join(' · ') : '없음'}</dd>
      </dl>
    </section>
  )
}

// APPS 칸 아래 — 센서 1 · 2 와 고장 이름 (엔진 state.pedal)
export function AppsDetail({ s1, s2, v1, v2, pedal, frozen }: {
  s1: LiveTile
  s2: LiveTile
  v1: LiveTile
  v2: LiveTile
  pedal: LiveState['pedal']
  frozen: boolean
}) {
  const faults = pedal?.faults ?? []
  const volt = (v: LiveTile) => (v.value === null ? '' : ` (${tileValue(v)} ${v.unit})`) // 보드 ADC 전압 — 받았을 때만
  const st = pedal ? shownStatus(pedal.status, frozen) : 'none'
  return (
    <div className="live-tile-sub">
      <div>
        센서 1 · {tileValue(s1)}
        {s1.value !== null && ` ${s1.unit}`}
        {volt(v1)}
        {'   '}센서 2 · {tileValue(s2)}
        {s2.value !== null && ` ${s2.unit}`}
        {volt(v2)}
      </div>
      {pedal && (
        <div className={faults.length && st !== 'stale' ? 'live-warn-text' : 'muted'}>
          {st === 'none' ? 'APPS 보드 수신 없음' : faults.length ? `APPS 고장 · ${faults.join(' · ')}` : 'APPS 고장 없음'}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 수신 · DAQ

// 수신률 막대 — 색은 엔진의 rate_status (80 / 50 %, 사용자 확인). 상태를 안 주는 송신 장치 줄은 plain (색 없음). 서버 응답이 끊기면 회색
export function RateBar({ r, status, frozen }: { r: number | null | undefined; status: LiveSource['rate_status'] | 'plain'; frozen: boolean }) {
  const tone = frozen ? 'stale' : r == null ? 'none' : status ?? 'none'
  return (
    <span className="live-rate">
      <span className="live-rate-track">{r != null && <span className={`live-rate-fill ${tone}`} style={{ width: `${Math.min(1, r) * 100}%` }} />}</span>
      <span className={`live-rate-num ${tone}`}>{pctText(r)}</span>
    </span>
  )
}

