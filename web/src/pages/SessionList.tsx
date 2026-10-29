// 보드 1 · 세션 목록 — 왼쪽 거르기(차량 · 이벤트 · 종목 · 데이터), 세션 표 + 블록마다 한 칸.
// 입구는 세션 단위 — 드라이버 거르기 · 드라이버 열 · 최속 랩 열은 두지 않는다 (사이트_설계.md §7)

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { useApi } from '../api'
import { blockCell, blockColumns, type Column } from '../sessions/blocks'
import { applyFilters, facet, readFilters, SORTS, toggle, writeFilters, hasSeek, hasWarn, type FacetKey, type Filters, type SortKey } from '../sessions/filters'
import { formatDuration, formatNumber, formatStart, lapsCell, sessionName } from '../sessions/format'
import type { SessionRow } from '../types'

const UNCLASSIFIED = '미분류' // 엔진이 랩 · 종목을 못 알아본 주행 (계약 v0.11)

const FACETS: { key: FacetKey; title: string }[] = [
  { key: 'car', title: '차량' },
  { key: 'event', title: '이벤트' },
  { key: 'disc', title: '종목' },
]

export default function SessionList({ total }: { total: number | null }) {
  const { data, error, loading } = useApi<SessionRow[]>('/api/sessions')
  const [params, setParams] = useSearchParams()
  const f = readFilters(params)

  // 찾기 칸은 자체 상태로 바로 받는다 — 글자마다 주소를 고치고 다시 읽으면 한글 조합(ㄱ → 가 → 각)이 깨진다.
  // 주소에는 입력이 멈춘 뒤에 쓴다 (새로고침 · 링크 복사용)
  const [q, setQ] = useState(f.q)
  const written = useRef(f.q)
  useEffect(() => {
    if (f.q !== written.current) {
      written.current = f.q // 바깥에서 주소가 바뀌었다 (조건 초기화 · 뒤로 가기) — 칸도 따라간다
      setQ(f.q)
    }
  }, [f.q])
  useEffect(() => {
    if (q === written.current) return
    const timer = setTimeout(() => {
      written.current = q
      setParams((p) => writeFilters({ ...readFilters(p), q }), { replace: true })
    }, 300)
    return () => clearTimeout(timer)
  }, [q, setParams])

  const set = (g: Partial<Filters>) => {
    written.current = q
    setParams(writeFilters({ ...f, q, ...g }), { replace: true })
  }

  const all = useMemo(() => data ?? [], [data])
  const shown = useMemo(() => applyFilters(all, { ...readFilters(params), q }), [all, params, q])
  const cols = useMemo(() => blockColumns(shown), [shown])

  return (
    <>
      <div className="toolbar">
        <label htmlFor="q">찾기</label>
        <input id="q" className="field" style={{ width: 260 }} placeholder="세션 · 이벤트 · 종목 · ID" value={q}
               onChange={(e) => setQ(e.target.value)} />
        <label htmlFor="sort" style={{ marginLeft: 8 }}>정렬</label>
        <select id="sort" className="field" value={f.sort} onChange={(e) => set({ sort: e.target.value as SortKey })}>
          {SORTS.map((s) => (
            <option key={s.key} value={s.key}>{s.label}</option>
          ))}
        </select>
        <div style={{ flex: 1 }} />
        <span className="muted" style={{ marginRight: 14 }}>
          이름 = 날짜 · R순번 · 출발 · 종목 · 드라이버 · <span style={{ color: 'var(--text-label)' }}>목적</span>
        </span>
        <span className="muted">
          세션 {shown.length}개 표시{total !== null && ` · 전체 ${total}개`}
        </span>
      </div>

      <div className="split">
        <aside className="filters" aria-label="거르기">
          {FACETS.map(({ key, title }) => {
            const items = facet(all, key)
            const values = items.map((i) => i.value)
            return (
              <fieldset key={key}>
                <legend>{title}</legend>
                <div className="items">
                  {items.map((it) => (
                    <label key={it.value} title={it.value}>
                      <input type="checkbox" checked={f[key] === null || f[key]!.includes(it.value)}
                             onChange={() => set({ [key]: toggle(f[key], values, it.value) })} />
                      <span>{it.value}</span>
                      <span className="count">{it.n}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
            )
          })}
          <fieldset>
            <legend>데이터</legend>
            <div className="items">
              <label>
                <input type="checkbox" checked={f.warn} onChange={() => set({ warn: !f.warn })} />
                <span>주의가 있는 세션만</span>
                <span className="count">{all.filter(hasWarn).length}</span>
              </label>
              <label>
                <input type="checkbox" checked={f.seek} onChange={() => set({ seek: !f.seek })} />
                <span>미보유 데이터가 있는 세션만</span>
                <span className="count">{all.filter(hasSeek).length}</span>
              </label>
            </div>
          </fieldset>
        </aside>

        <section className="table-wrap" aria-label="세션">
          <div className="table-scroll">
            {loading && <div className="empty">세션 목록 불러오는 중…</div>}
            {error && <div className="empty">세션 목록을 불러오지 못했습니다 — {error.message}</div>}
            {data && shown.length === 0 && (
              <div className="empty">
                조건에 맞는 세션이 없습니다.{' '}
                <button type="button" className="linkish" onClick={() => setParams({}, { replace: true })}>조건 초기화</button>
              </div>
            )}
            {shown.length > 0 && <SessionTable rows={shown} columns={cols.columns} computed={cols.computed} hidden={cols.hidden} />}
          </div>
          <div className="legend">
            <span><span className="st-have">✓</span> 보유</span>
            <span><span className="st-warn">▲</span> 보유 (주의)</span>
            <span><span className="st-seek">✕</span> 미보유 · 기록했는데 우리한테 없음</span>
            <span>빈칸 · 기록 안 함</span>
            <span>계산 · 엔진이 다른 데이터로 계산한 값 (추정이라 항상 ▲)</span>
            <span>랩 — · 랩이 없는 세션 · 코스 기록 없음</span>
          </div>
        </section>
      </div>
    </>
  )
}

// 칸 폭 (px). 세션 이름 칸만 남는 폭을 쓴다 — 1440 px 노트북 화면에 블록 칸까지 한 번에 들어오게
const FIXED = { start: 126, car: 84, event: 128, disc: 68, drive: 52, dist: 52, laps: 34, mark: 32, computed: 44 }
const NAME_MIN = 240

function SessionTable({ rows, columns, computed, hidden }: { rows: SessionRow[]; columns: Column[]; computed: Column[]; hidden: Column[] }) {
  const navigate = useNavigate()
  const marks = [...columns, ...computed]
  const fixed = FIXED.start + FIXED.car + FIXED.event + FIXED.disc + FIXED.drive + FIXED.dist + FIXED.laps + columns.length * FIXED.mark + computed.length * FIXED.computed
  const hiddenNote = hidden.length > 0 ? ` · 없는 블록 ${hidden.length}개 숨김` : ''
  return (
    <table className="grid" style={{ minWidth: fixed + NAME_MIN }}>
      <colgroup>
        <col style={{ width: FIXED.start }} />
        <col />
        <col style={{ width: FIXED.car }} />
        <col style={{ width: FIXED.event }} />
        <col style={{ width: FIXED.disc }} />
        <col style={{ width: FIXED.drive }} />
        <col style={{ width: FIXED.dist }} />
        <col style={{ width: FIXED.laps }} />
        {marks.map((c) => (
          <col key={c.key} style={{ width: c.computed ? FIXED.computed : FIXED.mark }} />
        ))}
      </colgroup>
      <thead>
        <tr className="group">
          <th colSpan={8} />
          {columns.length > 0 && (
            <th colSpan={columns.length} className="sep" title={hidden.length ? `숨긴 블록: ${hidden.map((c) => c.label).join(' · ')}` : undefined}>
              블록 (데이터 종류){hiddenNote}
            </th>
          )}
          {computed.length > 0 && <th colSpan={computed.length} className="sep" style={{ textAlign: 'center' }}>계산</th>}
        </tr>
        <tr className="cols">
          <th className="start">시작</th>
          <th>세션</th>
          <th>차량</th>
          <th>이벤트</th>
          <th>종목</th>
          <th className="num">주행</th>
          <th className="num" title="거리 (km)">km</th>
          <th className="num">랩</th>
          {marks.map((c, i) => (
            <th key={c.key} className={`mark${i === 0 || i === columns.length ? ' sep' : ''}`} title={c.label}>
              {c.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const n = sessionName(r)
          const laps = lapsCell(r)
          const href = `/sessions/${encodeURIComponent(r.sid)}`
          return (
            <tr key={r.sid} onClick={(e) => !(e.target as HTMLElement).closest('a') && navigate(href)}>
              <td className="start muted">{formatStart(r.start)}</td>
              <td className="name" title={`${n.main}${n.purpose ? ` · ${n.purpose}` : ''}\n${r.sid}`}>
                <Link to={href}>{n.main}</Link>
                {n.purpose && <span className="purpose"> · {n.purpose}</span>}
              </td>
              <td>{r.car}</td>
              <td title={`${r.event_title} · ${r.event_kind} · ${r.place}`}>{r.event_title}</td>
              {r.discipline === UNCLASSIFIED ? (
                <td className="st-warn" title="종목을 알아보지 못한 주행 — 확인 필요 (세션 정리에서 종목을 정하거나 버린다)">
                  ▲ {r.discipline}
                </td>
              ) : (
                <td>{r.discipline}</td>
              )}
              <td className="num">{formatDuration(r.duration_s)}</td>
              <td className="num">{formatNumber(r.distance_km, 2)}</td>
              <td className={`num${laps.muted ? ' muted' : ''}`} title={laps.tip}>{laps.text}</td>
              {[...columns, ...computed].map((c, i) => {
                const cell = blockCell(r, c.key)
                return (
                  <td key={c.key} className={`mark st-${cell.status}${i === 0 || i === columns.length ? ' sep' : ''}`} title={cell.tip}>
                    {cell.mark}
                  </td>
                )
              })}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
