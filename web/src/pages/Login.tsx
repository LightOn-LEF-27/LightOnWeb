// 로그인 — Google 학교 계정 + 명단. 거절되면 서버가 준 이유 code 로 안내한다 (server/src/lefsite/auth.py REFUSALS)

const REASONS: Record<string, string> = {
  oauth_failed: 'Google 로그인이 끝나지 않았습니다. 다시 시도해 주세요.',
  email_unverified: 'Google 이 메일 주소를 확인하지 않은 계정입니다.',
  not_workspace: '학교 Google 계정이 아닙니다 — 학교 메일 계정으로 로그인해 주세요.',
  wrong_domain: '학교 메일 계정만 들어올 수 있습니다.',
  not_listed: '명단에 없는 계정입니다. 관리자에게 메일 주소를 알려 주세요.',
  disabled: '사용이 꺼진 계정입니다. 관리자에게 문의해 주세요.',
}

export default function Login({ mode, error, next }: { mode: string; error: string | null; next: string }) {
  const href = `/auth/google/login?next=${encodeURIComponent(next)}`
  return (
    <div className="page-msg">
      <h1>LightOn Data</h1>
      <p className="muted">라이트온 팀원이 보는 주행 데이터 뷰어입니다.</p>
      {error && <p className="notice">{REASONS[error] ?? `로그인할 수 없습니다 (${error}).`}</p>}
      {mode === 'google' ? (
        <p>
          <a className="btn" href={href} style={{ display: 'inline-flex', alignItems: 'center' }}>
            학교 Google 계정으로 로그인
          </a>
        </p>
      ) : (
        <p className="muted">
          개발용 자동 로그인 사용자가 명단에서 꺼져 있습니다 — <code>python -m lefsite users enable &lt;메일&gt;</code>
        </p>
      )}
    </div>
  )
}
