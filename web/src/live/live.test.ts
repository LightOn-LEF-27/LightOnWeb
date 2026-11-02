import { describe, expect, it } from 'vitest'

import type { LiveState } from '../types'
import { ageText, clock, emptyBuf, lapText, linkView, mergeChart, outageText, rateText, shownStatus, sinceOf, trail } from './model'

const chart = (t: number[], speed: (number | null)[], extra: Record<string, (number | null)[]> = {}): LiveState['chart'] => ({
  t,
  series: { 'gps.speed_kmh': speed, ...extra },
})

const link = (over: Partial<LiveState['link']> = {}): LiveState['link'] => ({
  status: 'ok', age_s: 0.4, period_s: 0.5, rate_10s: 0.95, outages: 0, longest_s: null, since_s: null, ...over,
})

describe('실시간 그래프 값 잇기', () => {
  it('받은 줄을 뒤에 붙이고, since 로 다음에 물을 t 를 준다', () => {
    let b = mergeChart(emptyBuf(), chart([100, 100.5], [10, 11]), 101)
    b = mergeChart(b, chart([101], [12]), 101.5)
    expect(b.t).toEqual([100, 100.5, 101])
    expect(b.series['gps.speed_kmh']).toEqual([10, 11, 12])
    expect(sinceOf(b)).toBe(101)
  })
  it('가진 마지막 t 이하는 다시 붙이지 않는다', () => {
    const b = mergeChart(mergeChart(emptyBuf(), chart([100, 100.5], [10, 11]), 101), chart([100.5, 101], [99, 12]), 101)
    expect(b.t).toEqual([100, 100.5, 101])
    expect(b.series['gps.speed_kmh']).toEqual([10, 11, 12])
  })
  it('창(최근 300 s) 밖은 앞에서 자르고, 늦게 생긴 시리즈는 앞을 비워 길이를 맞춘다', () => {
    let b = mergeChart(emptyBuf(), chart([0, 100, 200], [1, 2, 3]), 200)
    b = mergeChart(b, chart([400], [4], { 'battery.power_kw': [30] }), 400)
    expect(b.t).toEqual([100, 200, 400])
    expect(b.series['gps.speed_kmh']).toEqual([2, 3, 4])
    expect(b.series['battery.power_kw']).toEqual([null, null, 30])
  })
  it('가진 값이 서버의 지금보다 앞서 있으면 처음부터 다시 쌓는다 (서버 시계가 바뀜)', () => {
    const b = mergeChart(mergeChart(emptyBuf(), chart([5000], [1]), 5000), chart([200], [2]), 200)
    expect(b.t).toEqual([200])
  })
  it('최근 궤적 — 위치가 둘 다 있는 줄만, 최근 30 s', () => {
    const b = mergeChart(emptyBuf(), chart([0, 80, 90, 95], [1, 1, 1, 1], { 'gps.x_m': [1, 2, null, 4], 'gps.y_m': [1, 2, 3, 4] }), 100)
    expect(trail(b, 100)).toEqual([[2, 2], [4, 4]])
  })
})

describe('실시간 표시', () => {
  it('서버 응답이 끊기면 색이 있는 상태도 낡음으로 — 무수신은 그대로', () => {
    expect(shownStatus('danger', true)).toBe('stale')
    expect(shownStatus('ok', true)).toBe('stale')
    expect(shownStatus('none', true)).toBe('none')
    expect(shownStatus('warn', false)).toBe('warn')
  })
  it('수신 상태 — 늦음은 주의, 끊김은 끊긴 지 몇 초로', () => {
    expect(linkView(link())).toEqual({ text: '수신 0.4 s', tone: 'ok' })
    expect(linkView(link({ status: 'late', age_s: 4.2 })).tone).toBe('warn')
    expect(linkView(link({ status: 'lost', age_s: 14.2, since_s: 14.2 }))).toEqual({ text: '수신 끊김 14 s', tone: 'danger' })
    expect(linkView(link({ status: 'none', age_s: null })).tone).toBe('none')
    expect(rateText(link())).toBe('10 s 95 %')
    expect(outageText(link({ outages: 1, longest_s: 14.2 }))).toBe('끊김 1번 · 최장 14 s')
    expect(outageText(link())).toBe('끊김 없음')
  })
  it('지난 시간 · 랩 시간 · 한국 시각', () => {
    expect(ageText(3.24)).toBe('3.2 s 전')
    expect(ageText(42)).toBe('42 s 전')
    expect(ageText(300)).toBe('5분 전')
    expect(lapText(66.4)).toBe('1:06.4')
    expect(lapText(31.2)).toBe('31.2')
    expect(lapText(null)).toBe('—')
    expect(clock(Date.UTC(2026, 10, 14, 3, 41, 7) / 1000)).toBe('12:41:07') // UTC 03:41 = 한국 12:41
  })
})
