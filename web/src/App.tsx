// 틀 — 로그인 확인 → 앱 바 · 본문 · 상태 줄. 주소는 세션 ID(sid)로만 세션을 가리킨다 (계약 §3-1)

import { useCallback, useEffect, useState } from 'react'
import { Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom'

import { ApiError, getJson, post, useApi } from './api'
import Later from './pages/Later'
import Login from './pages/Login'
import NotFound from './pages/NotFound'
import SessionList from './pages/SessionList'
import SessionView from './pages/SessionView'
import { applyTheme, savedTheme, type Theme } from './theme'
import type { Info, Me } from './types'

type Auth = { state: 'loading' } | { state: 'in'; me: Me } | { state: 'out'; mode: string; refused?: string } | { state: 'down' }

export default function App() {
  const [auth, setAuth] = useState<Auth>({ state: 'loading' })
  const location = useLocation()

  const check = useCallback(() => {
    getJson<Me>('/api/me').then(
      (me) => setAuth({ state: 'in', me }),
      (e: unknown) => {
        if (!(e instanceof ApiError) || e.status === 0 || e.status >= 500) return setAuth({ state: 'down' })
        const detail = (e.detail ?? {}) as { login?: string }
        setAuth({ state: 'out', mode: detail.login ?? 'google', refused: e.status === 403 ? e.code : undefined })
      },
    )
  }, [])
  useEffect(check, [check])

  if (auth.state === 'loading') return <div className="page-msg muted">불러오는 중…</div>
  if (auth.state === 'down')
    return (
      <div className="page-msg">
        <h1>서버에 연결할 수 없습니다</h1>
        <p>서버(<code>python -m lefsite</code>)가 켜져 있는지 확인해 주세요.</p>
        <button className="btn" onClick={check}>
          다시 시도
        </button>
      </div>
    )
  if (auth.state === 'out') {
    const error = new URLSearchParams(location.search).get('error') ?? auth.refused
    return <Login mode={auth.mode} error={error ?? null} next={location.pathname === '/login' ? '/' : location.pathname + location.search} />
  }
  return <Shell me={auth.me} onLogout={() => post('/auth/logout').finally(check)} />
}

function Shell({ me, onLogout }: { me: Me; onLogout: () => void }) {
  const [theme, setTheme] = useState<Theme>(savedTheme)
  const info = useApi<Info>('/api/info')
  const pick = (t: Theme) => {
    applyTheme(t)
    setTheme(t)
  }
  const tab = ({ isActive }: { isActive: boolean }) => `tab${isActive ? ' active' : ''}`

  return (
    <div className="app">
      <header className="appbar">
        <div className="brand">LIGHTON DATA</div>
        <nav style={{ display: 'flex' }} aria-label="화면">
          <NavLink to="/sessions" className={tab}>
            세션
          </NavLink>
          {/* 레이싱 라인은 아직 없지만 탭은 둔다 — 누르면 "구현 중" 안내 (사이트_설계.md §7) */}
          <NavLink to="/racing-line" className={({ isActive }) => `tab later${isActive ? ' active' : ''}`}>
            레이싱 라인 <span className="tag">준비 중</span>
          </NavLink>
        </nav>
        <div className="spacer" />
        <div className="seg" role="group" aria-label="테마">
          <button type="button" aria-pressed={theme === 'dark'} onClick={() => pick('dark')}>
            다크
          </button>
          <button type="button" aria-pressed={theme === 'light'} onClick={() => pick('light')}>
            라이트
          </button>
        </div>
        <div className="who" title={me.email}>
          <span>
            {me.name || me.email} · {me.role_label}
            {me.auth === 'dev' && ' (개발용 자동 로그인)'}
          </span>
          {me.auth === 'google' && (
            <button type="button" className="linkish" onClick={onLogout}>
              로그아웃
            </button>
          )}
        </div>
      </header>

      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/sessions" replace />} />
          <Route path="/login" element={<Navigate to="/sessions" replace />} />
          <Route path="/sessions" element={<SessionList total={info.data?.engine.store.sessions ?? null} />} />
          <Route path="/sessions/:sid" element={<SessionView />} />
          <Route path="/racing-line" element={<Later what="레이싱 라인" />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>

      <footer className="statusbar">
        {info.data ? (
          <>
            <span>
              엔진 {info.data.engine.engine.name} {info.data.engine.engine.version} · 계약 {info.data.engine.contract}
            </span>
            {info.data.connector === 'fake' ? (
              <span className="fake">가짜 연결부 — 합성 데이터 (실제 주행 아님)</span>
            ) : (
              <span>데이터 폴더 · 로컬</span>
            )}
            <span>세션 {info.data.engine.store.sessions}개</span>
            <span>사이트 {info.data.site.version}</span>
          </>
        ) : (
          <span>{info.error ? `엔진 정보를 불러오지 못했습니다 — ${info.error.message}` : '엔진 정보 불러오는 중…'}</span>
        )}
      </footer>
    </div>
  )
}
