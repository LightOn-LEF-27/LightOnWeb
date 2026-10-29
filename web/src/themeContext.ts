// 지금 테마 — 그래프(캔버스)는 CSS 를 따라 바뀌지 않아서, 테마가 바뀌면 다시 그리도록 알려 준다

import { createContext } from 'react'

import type { Theme } from './theme'

export const ThemeContext = createContext<Theme>('dark')
