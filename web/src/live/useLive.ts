// 실시간 상태 묻기 — 0.5 s 마다 GET /api/live/state?since=<가진 그래프의 마지막 t> (계약 §12-1 · §12-4).
// 한 번에 하나만 묻는다 (응답이 오면 다음). 실패하면 늦춰 가며 다시 묻고, 설정 · 권한 문제면 멈춘다.

import { useEffect, useRef, useState } from 'react'

import { ApiError, getJson } from '../api'
import type { LiveState } from '../types'
import { type ChartBuf, emptyBuf, FROZEN_S, mergeChart, POLL_MS, sinceOf } from './model'

const TIMEOUT_MS = 5000 // 응답이 이보다 늦으면 끊고 다시 묻는다
const STOP = new Set(['live_off', 'login_required', 'forbidden'])

export interface Live {
  state: LiveState | null
  buf: ChartBuf
  error: ApiError | null
  frozen: boolean // 서버 응답이 FROZEN_S 넘게 없음 — 화면의 값이 전부 그 전 값
  frozenFor: number | null
}

export function useLive(): Live {
  const [state, setState] = useState<LiveState | null>(null)
  const [buf, setBuf] = useState<ChartBuf>(emptyBuf)
  const [error, setError] = useState<ApiError | null>(null)
  const [frozenFor, setFrozenFor] = useState<number | null>(null)
  const okAt = useRef<number | null>(null) // 마지막으로 받은 때 (performance.now — 이 기기의 경과 시간만 쓴다, 시계는 안 본다)
  const bufRef = useRef(buf)

  useEffect(() => {
    let alive = true
    let timer: number | undefined
    let ctl: AbortController | null = null
    let fails = 0
    let inFlight = false
    const next = (ms: number) => (timer = window.setTimeout(run, ms))
    async function run() {
      if (!alive) return
      if (document.hidden) return void next(1000) // 안 보는 동안은 묻지 않는다 (폰 데이터)
      const started = performance.now()
      inFlight = true
      ctl = new AbortController()
      const kill = window.setTimeout(() => ctl?.abort(), TIMEOUT_MS)
      const since = sinceOf(bufRef.current)
      try {
        const s = await getJson<LiveState>(`/api/live/state${since !== null ? `?since=${since}` : ''}`, ctl.signal)
        if (!alive) return
        bufRef.current = mergeChart(bufRef.current, s.chart, s.now)
        setBuf(bufRef.current)
        setState(s)
        setError(null)
        okAt.current = performance.now()
        setFrozenFor(0)
        fails = 0
        next(Math.max(0, POLL_MS - (performance.now() - started)))
      } catch (e) {
        if (!alive) return
        const err = e instanceof ApiError ? e : new ApiError(0, 'network', '서버 연결 실패', null)
        setError(err)
        if (STOP.has(err.code) || err.status === 401 || err.status === 403) return
        fails += 1
        next(Math.min(5000, POLL_MS * 2 ** Math.min(fails, 4)))
      } finally {
        window.clearTimeout(kill)
        inFlight = false
      }
    }
    run()
    const onShow = () => !document.hidden && !inFlight && (window.clearTimeout(timer), run()) // 다시 보면 바로 묻는다
    document.addEventListener('visibilitychange', onShow)
    // 응답이 끊겨도 '멈춘 지 N s' 가 늘게
    const clockTimer = window.setInterval(() => okAt.current !== null && setFrozenFor((performance.now() - okAt.current) / 1000), 1000)
    return () => {
      alive = false
      window.clearTimeout(timer)
      window.clearInterval(clockTimer)
      document.removeEventListener('visibilitychange', onShow)
      ctl?.abort()
    }
  }, [])

  return { state, buf, error, frozen: frozenFor !== null && frozenFor > FROZEN_S, frozenFor }
}
