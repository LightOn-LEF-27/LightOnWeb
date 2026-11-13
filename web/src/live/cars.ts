// 실시간 화면의 차량 · 뷰 (실시간화면_v1.md §5 · §6, 2026-11-12 개편 — 계기판 · 그래프를 나눔).
// 차마다 받을 수 있는 값이 달라 뷰도 차마다 다르다 (있는 것 위주). 어느 칸을 어느 뷰에 둘지만 화면이 정한다 — 값 · 상태 · 문구는 엔진 것

export type ViewKey = 'main' | 'graph' | 'battery' | 'powertrain' | 'link'

export const VIEW_NAMES: Record<ViewKey, string> = {
  main: '계기판',
  graph: '그래프',
  battery: '배터리',
  powertrain: '인버터 · 모터',
  link: '수신 · DAQ',
}

export interface LiveCar {
  id: string
  live: 'on' | 'none' | 'building' // on = 실시간으로 받는 차 · none = 송출 불가 · building = 구현 중
  device?: string // 송신 장치 이름 — 화면 문구의 '장치' 자리 (25Evo = Monolith)
  views?: ViewKey[]
  plan?: string // 구현 중인 차의 예정 뷰
}

export const CARS: LiveCar[] = [
  { id: 'LEF-25evo', live: 'on', device: 'Monolith', views: ['main', 'graph', 'battery', 'powertrain', 'link'] },
  { id: 'LEF-26', live: 'none' }, // 로거가 송출하지 않는다
  { id: 'LEF-27', live: 'building', plan: '메인 · 배터리 · 인버터 · 모터 · 섀시 · 타이어 · 수신 · DAQ' },
]

export const DEFAULT_CAR = CARS[0].id

export const carOf = (id: string | undefined): LiveCar | null => CARS.find((c) => c.id === id) ?? null

// 뷰마다 보이는 칸 — 경고 띠에서 그 경고의 칸이 지금 뷰에 없으면 "어느 뷰에서 자세히" 를 고를 때 쓴다 (views.tsx 의 배치와 같게)
export const VIEW_TILES: Record<ViewKey, string[]> = {
  main: [
    'gps.speed_kmh', 'battery.power_kw', 'bms.cell_min_v', 'bms.temp_max_c', 'motor.t_motor_c', 'motor.t_igbt_c',
    'pedal.apps_pct', 'brake.ain1_v', 'bms.soc_pct', 'bms.dcl_kw', 'bms.temp_spread_c', 'motor.rpm', 'lv.voltage_v',
  ],
  graph: [], // 그래프만 — 경고 띠는 칸이 있는 뷰로 보낸다
  battery: [
    'bms.soc_pct', 'battery.power_kw', 'battery.voltage_v', 'battery.current_a', 'bms.dcl_kw', 'bms.cell_min_v', 'bms.temp_max_id',
    'bms.temp_min_id', 'bms.temp_avg_c', 'bms.temp_max_c', 'bms.temp_min_c', 'bms.temp_spread_c', 'bms.bms_heatsink_c', 'bms.pack_resistance_ohm',
    'bms.pack_ah', 'bms.dod_pct',
  ],
  powertrain: [
    'motor.t_motor_c', 'motor.t_igbt_c', 'motor.vdc_v', 'motor.rpm', 'pedal.apps_pct', 'pedal.apps1_pct', 'pedal.apps2_pct',
    'pedal.torque_cmd_pct', 'motor.error_raw', 'pedal.fault_raw', // 오류 · 고장 비트는 인버터 상태 · APPS 칸에 이름으로
  ],
  link: ['lv.voltage_v'],
}

// 칸이 없는 경고 (id 로) — 수신 늦음 · 끊김, 원천 낡음, 장치 오류는 수신 · DAQ 뷰
const ALERT_VIEW: [string, ViewKey][] = [['W-LINK', 'link'], ['W-SRC', 'link'], ['W-DEV', 'link']]

// 경고를 자세히 볼 뷰 — 지금 뷰면 null. tileId = 그 경고가 물들인 칸 (tiles[].alert 로 찾는다)
export function alertView(alertId: string, tileId: string | null, current: ViewKey, views: ViewKey[]): ViewKey | null {
  const byId = ALERT_VIEW.find(([pre]) => alertId.startsWith(pre))?.[1]
  const home = byId ?? (tileId ? views.find((v) => VIEW_TILES[v].includes(tileId)) : undefined)
  if (!home || !views.includes(home)) return null
  if (home === current || (tileId && VIEW_TILES[current].includes(tileId))) return null
  return home
}
