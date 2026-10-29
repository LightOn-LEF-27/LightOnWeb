// 테마 — 다크 기본, 라이트 선택. 고른 것은 이 브라우저에만 기억한다 (못 읽으면 다크)

export type Theme = 'dark' | 'light'

const KEY = 'lefsite.theme'

export function savedTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

export function applyTheme(t: Theme): void {
  document.documentElement.dataset.theme = t
  try {
    localStorage.setItem(KEY, t)
  } catch {
    // 저장소를 막은 브라우저 — 이번 창에서만 바뀐다
  }
}
