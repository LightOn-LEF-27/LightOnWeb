// 서버 부르기 — 오류는 계약 §8 모양 {"error": {code, message, detail}} 을 ApiError 로 바꾼다.

import { useEffect, useState } from 'react'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: unknown

  constructor(status: number, code: string, message: string, detail: unknown) {
    super(message)
    this.status = status
    this.code = code
    this.detail = detail
  }
}

async function request<T>(method: string, path: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(path, { method, credentials: 'same-origin', headers: { Accept: 'application/json' }, signal })
  if (!r.ok) {
    let err: { code?: string; message?: string; detail?: unknown } | undefined
    try {
      err = (await r.json())?.error
    } catch {
      err = undefined // 서버가 꺼져 있거나 JSON 이 아닌 응답
    }
    throw new ApiError(r.status, err?.code ?? `http_${r.status}`, err?.message ?? `서버 응답 ${r.status}`, err?.detail ?? null)
  }
  return r.status === 204 ? (undefined as T) : ((await r.json()) as T)
}

export const getJson = <T>(path: string, signal?: AbortSignal) => request<T>('GET', path, signal)
export const post = (path: string) => request<void>('POST', path)

export interface Loaded<T> {
  data: T | null
  error: ApiError | null
  loading: boolean
}

// 주소가 바뀌면 다시 부른다. 앞선 요청은 끊는다 (늦게 온 옛 응답이 새 화면을 덮지 않게)
export function useApi<T>(path: string | null): Loaded<T> {
  const [state, setState] = useState<Loaded<T>>({ data: null, error: null, loading: path !== null })
  useEffect(() => {
    if (path === null) return
    const ctl = new AbortController()
    setState({ data: null, error: null, loading: true })
    getJson<T>(path, ctl.signal).then(
      (data) => setState({ data, error: null, loading: false }),
      (e: unknown) => {
        if (ctl.signal.aborted) return
        const error = e instanceof ApiError ? e : new ApiError(0, 'network', '서버에 연결할 수 없습니다. 서버가 켜져 있는지 확인해 주세요', null)
        setState({ data: null, error, loading: false })
      },
    )
    return () => ctl.abort()
  }, [path])
  return state
}
