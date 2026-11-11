// 실시간 트랙 받기 — 경기장 기준선 · 계측선 · 그날 달린 궤적 (계약 §12-5).
// 처음 한 번 받고, 상태의 track_rev 가 받은 궤적의 rev 와 달라지면 다시 받는다 (TRACK_MIN_MS 에 한 번까지).
// 다시 받는 동안 · 실패해도 가진 트랙은 그대로 그린다 (지도가 깜빡이거나 사라지지 않게)

import { useEffect, useRef, useState } from 'react'

import { ApiError, getJson, type Loaded } from '../api'
import type { LiveTrack } from '../types'
import { trackWait } from './model'

export function useLiveTrack(enabled: boolean, rev: number | null | undefined): Loaded<LiveTrack> {
  const [state, setState] = useState<Loaded<LiveTrack>>({ data: null, error: null, loading: enabled })
  const lastAt = useRef<number | null>(null) // 마지막으로 물은 때 (performance.now)
  const busy = useRef(false)
  const [done, setDone] = useState(0) // 받고 나면 다시 따져 보게

  useEffect(() => {
    if (!enabled || busy.current) return
    const wait = trackWait(rev, state.data, lastAt.current, performance.now())
    if (wait === null) return
    const ctl = new AbortController()
    const timer = window.setTimeout(() => {
      busy.current = true
      lastAt.current = performance.now()
      getJson<LiveTrack>('/api/live/track', ctl.signal)
        .then(
          (data) => setState({ data, error: null, loading: false }),
          (e: unknown) => {
            if (ctl.signal.aborted) return
            const error = e instanceof ApiError ? e : new ApiError(0, 'network', '서버 연결 실패', null)
            setState((s) => ({ data: s.data, error, loading: false }))
          },
        )
        .finally(() => {
          busy.current = false
          setDone((n) => n + 1)
        })
    }, wait)
    return () => {
      window.clearTimeout(timer)
      if (!busy.current) ctl.abort()
    }
  }, [enabled, rev, state.data, done])

  return state
}
