// 데이터 현황 — 블록마다 한 줄. 보유 블록을 누르면 펼쳐서 사실 · 검증 · 주의와 원본 → 코드 → 결과물을 보여 준다.
// 펼친 블록은 주소(?block=)에 남는다 — 링크로 같은 화면을 열 수 있게

import { Fragment, useState } from 'react'

import { MARK } from '../sessions/blocks'
import type { Block, DataStatus as Status, QualityItem } from '../types'
import { DownloadButton } from './Download'
import { formatBytes, statusGroups } from './model'

interface Props {
  statuses: Status[]
  blocks: Block[]
  quality: QualityItem[]
  open: string | null
  onToggle: (block: string) => void
  onDownload: (fileId: string) => void
}

export default function DataStatus({ statuses, blocks, quality, open, onToggle, onDownload }: Props) {
  const byKey = new Map(blocks.map((b) => [b.block, b]))
  const { shown, none } = statusGroups(statuses)
  const [showNone, setShowNone] = useState(false)

  return (
    <section className="panel">
      <h2>데이터 현황</h2>
      <table className="grid status-table">
        <colgroup>
          <col style={{ width: 230 }} />
          <col style={{ width: 120 }} />
          <col />
          <col style={{ width: 52 }} />
          <col style={{ width: 52 }} />
          <col style={{ width: 170 }} />
        </colgroup>
        <thead>
          <tr className="cols">
            <th>블록</th>
            <th>상태</th>
            <th>장비 · 주기</th>
            <th className="num">채널</th>
            <th className="num">원본</th>
            <th>결과물</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((d) => {
            const b = byKey.get(d.block)
            const isOpen = open === d.block && !!b
            return (
              <Fragment key={d.block}>
                <tr className={isOpen ? 'open' : undefined} onClick={b ? () => onToggle(d.block) : undefined} style={b ? undefined : { cursor: 'default' }}>
                  <td>
                    {b ? (
                      <button type="button" className="row-toggle" aria-expanded={isOpen} onClick={(e) => (e.stopPropagation(), onToggle(d.block))}>
                        <span aria-hidden="true">{isOpen ? '▾' : '▸'}</span> {d.title}
                      </button>
                    ) : (
                      <span className="row-toggle-pad">{d.title}</span>
                    )}
                  </td>
                  <td className={`st-${d.status}`}>
                    {MARK[d.status].mark} <span className="st-text">{d.status === 'seek' ? '미보유' : MARK[d.status].text}</span>
                  </td>
                  <td className="muted" title={d.detail}>
                    {b?.device ?? d.detail}
                  </td>
                  <td className="num">{b ? b.channels.length : ''}</td>
                  <td className="num">{b && b.raw.length ? b.raw.length : ''}</td>
                  <td className="muted" title={b?.exports.map((e) => e.name).join(', ')}>
                    {b?.exports.map((e) => e.name).join(', ')}
                  </td>
                </tr>
                {isOpen && b && (
                  <tr className="detail-row">
                    <td colSpan={6}>
                      <BlockDetail block={b} quality={quality.filter((q) => q.block === b.block)} onDownload={onDownload} />
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
      {none.length > 0 && (
        <div className="none-line">
          <button type="button" className="row-toggle" aria-expanded={showNone} onClick={() => setShowNone(!showNone)}>
            <span aria-hidden="true">{showNone ? '▾' : '▸'}</span> 기록 안 함 {none.length} · {none.map((d) => d.title).join(' · ')}
          </button>
          {showNone && (
            <ul>
              {none.map((d) => (
                <li key={d.block}>
                  {d.title} — <span className="muted">{d.detail}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}

function List({ items, empty }: { items: string[]; empty: string }) {
  return items.length ? (
    <ul className="plain">
      {items.map((x) => (
        <li key={x}>{x}</li>
      ))}
    </ul>
  ) : (
    <p className="muted">{empty}</p>
  )
}

function BlockDetail({ block: b, quality, onDownload }: { block: Block; quality: QualityItem[]; onDownload: (id: string) => void }) {
  const qualityLines = quality.map((q) => `${q.level === 'info' ? '참고' : '주의'} · ${q.message}`)
  return (
    <div className="block-detail">
      <div className="detail-cols">
        <div>
          <h3>사실</h3>
          <List items={b.facts} empty="없음" />
        </div>
        <div>
          <h3>검증</h3>
          <List items={b.checks} empty="없음" />
        </div>
        <div>
          <h3 className={b.cautions.length || quality.length ? 'st-warn' : undefined}>주의</h3>
          <List items={[...b.cautions, ...qualityLines]} empty="없음" />
        </div>
      </div>
      <div className="flow">
        <div className="flow-box">
          <h3>원본</h3>
          {b.raw.length === 0 && <p className="muted">없음 (엔진이 다른 블록에서 계산)</p>}
          {b.raw.map((f, i) => (
            <div className="file-row" key={f.file_id ?? `${f.name}-${i}`}>
              <span className="file-name" title={[f.label, f.note].filter(Boolean).join(' · ')}>
                {f.name}
              </span>
              <span className="muted">{formatBytes(f.size_bytes)}</span>
              {f.file_id ? <DownloadButton fileId={f.file_id} onClick={onDownload} /> : <span className="muted where">{f.where || '내려받을 수 없음'}</span>}
            </div>
          ))}
        </div>
        <div className="flow-arrow" aria-hidden="true">
          →
        </div>
        <div className="flow-box">
          <h3>코드</h3>
          {b.code.map((c) => (
            <p key={c.key}>
              {c.title} <span className="muted">— {c.role}</span>
            </p>
          ))}
        </div>
        <div className="flow-arrow" aria-hidden="true">
          →
        </div>
        <div className="flow-box">
          <h3>결과물</h3>
          {b.exports.map((f) => (
            <div className="file-row" key={f.file_id ?? f.name}>
              <span className="file-name" title={f.label}>
                {f.name}
              </span>
              <span className="muted">{f.rows !== undefined ? `${f.rows.toLocaleString('en-US')} 행` : formatBytes(f.size_bytes)}</span>
              {f.file_id && <DownloadButton fileId={f.file_id} onClick={onDownload} />}
            </div>
          ))}
          {(b.outputs ?? []).map((o) => (
            <p key={o.name} className="muted" title={o.where}>
              {o.label} · {o.name}
            </p>
          ))}
          {b.channels.length > 0 && (
            <div className="chips">
              {b.channels.map((c) => (
                <span key={c} className="chip">
                  {c}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
