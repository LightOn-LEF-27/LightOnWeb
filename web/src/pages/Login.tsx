// 로그인 — Google 학교 계정 + 명단. 거절되면 서버가 준 이유 code 로 안내한다 (server/src/lefsite/auth.py REFUSALS)

const REASONS: Record<string, string> = {
  oauth_failed: 'Google 로그인이 끝나지 않음. 다시 시도 필요',
  email_unverified: 'Google 이 메일 주소를 확인하지 않은 계정',
  not_workspace: '학교 Google 계정 아님 — 학교 메일 계정으로 로그인 필요',
  wrong_domain: '학교 메일 계정만 사용 가능',
  not_listed: '명단에 없는 계정. 관리자에게 메일 주소 전달 부탁드립니다.',
  disabled: '사용이 꺼진 계정. 관리자에게 문의 부탁드립니다.',
}

export default function Login({ mode, error, next }: { mode: string; error: string | null; next: string }) {
  const href = `/auth/google/login?next=${encodeURIComponent(next)}`
  return (
    <div className="page-msg">
      <h1>LightOn Data</h1>
      <p className="muted">라이트온 팀원용 주행 데이터 뷰어</p>
      {error && <p className="notice">{REASONS[error] ?? `로그인할 수 없음 (${error})`}</p>}
      {mode === 'google' ? (
        <p>
          <a className="btn" href={href} style={{ display: 'inline-flex', alignItems: 'center' }}>
            학교 Google 계정으로 로그인
          </a>
        </p>
      ) : (
        <p className="muted">
          개발용 자동 로그인 사용자가 명단에서 꺼져 있음 — <code>python -m lefsite users enable &lt;메일&gt;</code>
        </p>
      )}
    </div>
  )
}
