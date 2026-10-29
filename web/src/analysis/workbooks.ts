// 워크북 · 워크시트 = 프리셋 — "어떤 채널을 어떻게 그릴지" 설정이라 어느 세션에나 씌운다 (데이터가 아니다, 사이트_설계.md §7).
// 공통 · 파트마다 하나 · DAQ. 틀은 LEF-27 의 전체 데이터 기준 — 축마다 후보 채널을 적고 그 세션에 있는 것만 앞에서 둘까지 쓴다
// (장비마다 이름이 다른 채널은 후보로 나란히). 채널이 하나도 없는 워크시트는 숨기고 몇 개 숨겼는지 알린다.
// 지금은 이 기본값만 — 파트장이 고쳐 저장하는 기능은 2차 (서버 · 앱 DB, 파트 권한). 파트 구성은 LEF-26 의 2부 7파트 (LEF-27 은 [확인 필요])

import { MAX_PER_AXIS } from './state'
import { DELTA } from './traces'

export interface Sheet {
  name: string
  axes: string[][] // 축마다 후보 채널 (앞에서부터)
}

export interface Workbook {
  key: string
  name: string
  group: '공통' | '섀시부' | '구동부' | 'DAQ' // 고르는 칸의 묶음
  sheets: Sheet[]
}

const SPEED = ['gps.speed_kmh']
const PEDAL = ['pedal.apps_pct', 'pedal.brake_f_bar', 'pedal.torque_cmd_pct']
const STEER = ['steer.angle_deg']
const LAT = ['imu.lat_g']
const LONG = ['imu.long_g']
const CURRENT = ['battery.current_a']
const VOLT = ['battery.voltage_v', 'motor.vdc_v']
const POWER = ['battery.power_kw', 'battery.power_500ms_kw']
const SOC = ['bms.soc_pct']
const CELL_V = ['bms.cell_min_v', 'bms.cell_max_v']
const CELL_T = ['bms.temp_max_c', 'thermistor.highest_c', 'bms.temp_c']
const INV_T = ['motor.inverter_c', 'motor.t_igbt_c']
const MOT_T = ['motor.motor_c', 'motor.t_motor_c']
const RPM = ['motor.rpm']
const COOL = ['cooling.coolant_in_c']
const SUSP = ['suspension.fl_mm', 'suspension.fr_mm', 'suspension.rl_mm', 'suspension.rr_mm']
const WHEEL = ['wheel.fl_kmh', 'wheel.fr_kmh']

const BRAKE_P = ['pedal.brake_f_bar', 'pedal.brake_r_bar']
const YAW = ['imu.yaw_dps']
const LV = ['lv.voltage_v', 'lv.current_a']
const DCL = ['bms.dcl_kw', 'bms.dcl_a']

export const WORKBOOKS: Workbook[] = [
  { key: 'basic', name: '기본', group: '공통', sheets: [
    { name: '개요', axes: [SPEED, PEDAL, STEER, LAT, CURRENT] },
    { name: '랩 비교', axes: [SPEED, [DELTA], PEDAL, STEER, LAT] },
  ] },
  { key: 'coach', name: '드라이버 코칭', group: '공통', sheets: [
    { name: '랩 비교', axes: [SPEED, [DELTA], PEDAL, STEER] },
    { name: '제동', axes: [SPEED, BRAKE_P, LONG, WHEEL] },
    { name: '코너', axes: [SPEED, STEER, LAT, YAW] },
  ] },
  { key: 'suspension', name: '프레임 & 서스펜션', group: '섀시부', sheets: [
    { name: '스트로크', axes: [SPEED, SUSP.slice(0, 2), SUSP.slice(2), LAT] },
    { name: '거동', axes: [SPEED, LONG, LAT, YAW, STEER] },
    { name: '코너', axes: [SPEED, STEER, LAT, SUSP.slice(0, 2), SUSP.slice(2)] },
  ] },
  { key: 'aero', name: '에어로다이나믹스', group: '섀시부', sheets: [
    { name: '고속 · 차고', axes: [SPEED, SUSP.slice(0, 2), SUSP.slice(2), LONG] },
  ] },
  { key: 'brake', name: '브레이크', group: '섀시부', sheets: [
    { name: '제동', axes: [SPEED, BRAKE_P, LONG, WHEEL] },
    { name: '바퀴 잠김', axes: [WHEEL, SPEED, BRAKE_P] },
  ] },
  { key: 'hv', name: 'HV', group: '구동부', sheets: [
    { name: '팩 출력', axes: [CURRENT, VOLT, POWER, DCL] },
    { name: '출력 한도', axes: [['battery.power_500ms_kw', 'battery.power_kw'], DCL, SPEED] },
  ] },
  { key: 'drivetrain', name: '드라이브트레인', group: '구동부', sheets: [
    { name: '모터', axes: [RPM, ['motor.iq_a', 'motor.iq_cmd_a', 'pedal.torque_cmd_pct'], INV_T, MOT_T] },
    { name: '효율', axes: [SPEED, POWER, RPM] },
    { name: '냉각', axes: [COOL, INV_T, MOT_T] },
  ] },
  { key: 'lv', name: 'LV', group: '구동부', sheets: [
    { name: 'LV 전원', axes: [LV, VOLT] },
  ] },
  { key: 'battery', name: '배터리', group: '구동부', sheets: [
    { name: '셀', axes: [SOC, CELL_V, CELL_T, DCL] },
    { name: '전력', axes: [SPEED, POWER, CURRENT, VOLT] },
    { name: '온도', axes: [CELL_T, ['thermistor.lowest_c', 'thermistor.average_c', 'bms.temp_min_c'], COOL] },
  ] },
  { key: 'reliability', name: '신뢰성 점검', group: 'DAQ', sheets: [
    { name: '전압 · 전원', axes: [VOLT, CELL_V, DCL, LV] },
    { name: '온도', axes: [INV_T, MOT_T, CELL_T, COOL] },
    { name: '센서 확인', axes: [['pedal.apps1_pct', 'pedal.apps2_pct'], SUSP.slice(0, 2), SUSP.slice(2), WHEEL] },
  ] },
]

export const WORKBOOK_GROUPS = ['공통', '섀시부', '구동부', 'DAQ'] as const

export function workbook(key: string | null): Workbook {
  return WORKBOOKS.find((w) => w.key === key) ?? WORKBOOKS[0]
}

// 워크시트를 이 세션에 맞춘 축 — 있는 채널만, 축마다 둘까지. 축이 하나도 안 남으면 null (숨김)
export function sheetAxes(sheet: Sheet, available: Set<string>): string[][] | null {
  const axes = sheet.axes.map((a) => a.filter((c) => available.has(c)).slice(0, MAX_PER_AXIS)).filter((a) => a.length)
  return axes.some((a) => !a.includes(DELTA)) ? axes : null
}

export const sameAxes = (a: string[][], b: string[][]): boolean => JSON.stringify(a) === JSON.stringify(b)
