// 왼쪽 채널 목록 — 블록마다 묶음. 누르면 축으로 그리고, 다시 누르면 뺀다. 주의가 있는 채널은 ▲, 엔진이 계산한 채널은 "계산"
// 묶음 순서는 세션 목록의 블록 칸과 같다 (LEF-27 틀). 이 세션에 없는 블록은 숨기고 몇 개 숨겼는지 한 줄로 알린다
// 묶음마다 전체 선택 · 전체 해제 (세션 목록 거르기와 같은 모양) — 찾기 글자가 있으면 보이는 채널만. 축이 꽉 차면 들어가는 만큼만

import { useMemo, useState } from 'react'

import { COMPUTED, FRAME } from '../sessions/blocks'
import type { Channel } from '../types'

interface Props {
  channels: Channel[]
  blockTitles: Map<string, string>
  shown: Set<string> // 그래프에 그려진 채널
  onToggle: (channelId: string) => void
  onSet: (channelIds: string[], on: boolean) => void // 묶음 전체 선택 · 해제
  room: number // 더 그릴 수 있는 축 수
  maxAxes: number
}

export default function ChannelList({ channels, blockTitles, shown, onToggle, onSet, room, maxAxes }: Props) {
  const [q, setQ] = useState('')
  const [closed, setClosed] = useState<Set<string>>(new Set())
  const groups = useMemo(() => {
    const order = [...FRAME, ...COMPUTED].map((c) => c.key)
    const by = new Map<string, Channel[]>()
    for (const c of channels) by.set(c.block, [...(by.get(c.block) ?? []), c])
    return [...by.entries()].sort(([a], [b]) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99))
  }, [channels])
  const hidden = [...FRAME, ...COMPUTED].filter((c) => !groups.some(([b]) => b === c.key))
  const words = q.toLowerCase().split(/\s+/).filter(Boolean)
  const match = (c: Channel) => words.every((w) => `${c.label} ${c.channel_id}`.toLowerCase().includes(w))

  return (
    <aside className="channels" aria-label="채널">
      <div className="channels-search">
        <label htmlFor="chq" className="sr-only">
          채널 찾기
        </label>
        <input id="chq" className="field" placeholder="채널 찾기" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <div className="channels-list">
        {groups.map(([block, list]) => {
          const items = list.filter(match)
          if (!items.length) return null
          const open = words.length > 0 || !closed.has(block)
          const title = blockTitles.get(block) ?? block
          const ids = items.map((c) => c.channel_id)
          const off = ids.filter((c) => !shown.has(c)).length
          const full = off > 0 && room === 0
          return (
            <div key={block}>
              <div className="ch-group">
                <button type="button" className="ch-group-btn" aria-expanded={open}
                        onClick={() => setClosed((s) => (s.has(block) ? new Set([...s].filter((b) => b !== block)) : new Set(s).add(block)))}>
                  <span aria-hidden="true">{open ? '▾' : '▸'}</span>
                  <span className="ch-group-name">{title}</span>
                  <span className="muted">{list.length}</span>
                </button>
                <span className="legend-actions">
                  <button type="button" disabled={off === 0 || full} aria-label={`${title} 전체 선택`}
                          title={full || off > room ? `축은 ${maxAxes}개까지 — 들어가는 만큼만 그림` : undefined} onClick={() => onSet(ids, true)}>
                    전체 선택
                  </button>
                  <button type="button" disabled={off === ids.length} aria-label={`${title} 전체 해제`} onClick={() => onSet(ids, false)}>
                    전체 해제
                  </button>
                </span>
              </div>
              {open &&
                items.map((c) => (
                  <button key={c.channel_id} type="button" className={`ch-item${shown.has(c.channel_id) ? ' on' : ''}`} aria-pressed={shown.has(c.channel_id)}
                          title={[c.channel_id, c.sign && `부호: ${c.sign}`, c.derivation, ...c.cautions].filter(Boolean).join('\n')}
                          onClick={() => onToggle(c.channel_id)}>
                    <span className="ch-dot" aria-hidden="true" />
                    <span className="ch-name">{c.label}</span>
                    {c.cautions.length > 0 && <span className="st-warn">▲</span>}
                    {c.derived && <span className="muted ch-tag">계산</span>}
                    <span className="muted ch-unit">{c.unit}</span>
                  </button>
                ))}
            </div>
          )
        })}
      </div>
      {hidden.length > 0 && (
        <p className="channels-foot muted" title={hidden.map((c) => c.label).join(' · ')}>
          이 세션에 없는 블록 {hidden.length}개 숨김
        </p>
      )}
    </aside>
  )
}
