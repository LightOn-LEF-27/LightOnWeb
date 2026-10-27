"""명령행 진입점.

    lefdata monolith            LEF-25evo Monolith .log 정규화
    lefdata lef26 [--session S] LEF-26 통합본 CSV 정규화
    lefdata laps                GPS 로 주행 구간·랩 분할 → derived/   (LEF-25evo)
    lefdata motor-runs          모터 속도로 주행 구간 분할 → derived/motor_runs.csv  (LEF-26)
    lefdata events              가속(75m)·스키드패드 분석 → derived/   (LEF-25evo)
    lefdata gopro               온보드 영상 → normalized/ (GPS·IMU)
    lefdata check               데이터 품질 검사 (시간축·물리범위·공백·중복)
    lefdata energymeter         FSK 에너지미터(FSK-EEM) .log → normalized/
    lefdata evo-stm32           25Evo STM32 백업 로거 CSV → normalized/ + 시계 속도·시작 시각 맞추기 (--clock em|gps)
    lefdata orion               Orion BMS Connect CSV(Utility 변환본) → normalized/
    lefdata clocks              이벤트 안 장비 시계 차이 측정 → derived/clock_offsets.csv · power_cycles.csv
    lefdata lef26-clock         LEF-26 로거 시계를 로그 자체와 영상 GPS 로 검증 → derived/logger_clock.csv
    lefdata lef26-laps          영상 GPS 로 나눈 랩·가속·스키드패드에 로거 값을 붙인다 → derived/lap_logger.csv 외
    lefdata em-laps             GPS 랩에 에너지미터를 붙인다 (전력 ↔ GPS 로 시계를 맞춘다) → derived/lap_energy.csv 외
    lefdata report              하루치 시험 주행 보고서 (HTML + 그림, --pdf) → derived/report_<날짜>/
    lefdata catalog             주행 데이터 카탈로그: 세션마다 한 페이지 (원본 → 코드 → 결과물 · 그림) → derived/catalog/ · 07_데이터/_catalog/
    lefdata sessions            분석 엔진(lefdata.api)이 읽는 세션 저장소를 만든다 → 07_데이터/_sessions/
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import (clocks, em_laps, energymeter, events, evo_stm32, laps, lef26, lef26_clock, lef26_laps, monolith, motor_runs, orion,
               paths, quality, video)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="lefdata")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("monolith", help="Monolith .log → normalized/")
    p.add_argument("--event", type=Path, default=paths.LEF25EVO_EVENT,
                   help="이벤트 폴더 (raw_logger/ 를 포함)")

    p = sub.add_parser("lef26", help="LEF-26 통합본 CSV → normalized/")
    p.add_argument("--event", type=Path, default=paths.LEF26_EVENT)
    p.add_argument("--session", action="append", help="예: 2026-08-13_am (여러 번 지정 가능)")

    p = sub.add_parser("laps", help="GPS 주행 구간·랩 분할 → derived/runs.csv, laps.csv")
    p.add_argument("--event", type=Path, default=paths.LEF25EVO_EVENT)
    p.add_argument("--venue", help="경기장 이름 — 그 계측선 · 원점으로 나눈다 (없으면 catalog_notes.toml 의 [event] venue, 그것도 없으면 이벤트마다 새로)")

    p = sub.add_parser("venue", help="경기장 정하기: 이벤트의 계측선 · 원점 · 최속 랩 → 07_데이터/_venues/<이름>.json")
    p.add_argument("--name", required=True, help="경기장 이름 (영문 소문자, 예: yeonggwang)")
    p.add_argument("--from-event", type=Path, required=True, help="랩 분할이 끝난 이벤트 — 그 계측선을 경기장 것으로 삼는다")

    p = sub.add_parser("motor-runs", help="모터 속도로 주행 구간 분할 → derived/motor_runs.csv")
    p.add_argument("--event", type=Path, default=paths.LEF26_EVENT)

    p = sub.add_parser("events", help="가속·스키드패드 종목 분석 → derived/")
    p.add_argument("--event", type=Path, default=paths.LEF25EVO_EVENT)

    p = sub.add_parser("check", help="데이터 품질 검사")
    p.add_argument("--event", type=Path, required=True)
    p.add_argument("--all", action="store_true", help="통과한 검사도 전부 보여준다")

    p = sub.add_parser("energymeter", help="FSK 에너지미터(FSK-EEM) .log → normalized/")
    p.add_argument("--event", type=Path, default=paths.LEF25EVO_FSK)

    p = sub.add_parser("evo-stm32", help="25Evo STM32 백업 로거 CSV → normalized/ + 에너지미터 시간 정렬")
    p.add_argument("--event", type=Path, default=paths.LEF25EVO_FSK)
    p.add_argument("--no-align", action="store_true", help="변환만 하고 시간 정렬은 건너뛴다")
    p.add_argument("--clock", choices=("em", "gps"), default="em",
                   help="절대시각을 어느 장비 시계로 채울지: em = 에너지미터(전압), gps = Monolith(GPS 속도). 기본 em")

    p = sub.add_parser("orion", help="Orion BMS Connect CSV → normalized/")
    p.add_argument("--event", type=Path, default=paths.LEF25EVO_FSK)
    p.add_argument("--src", type=Path, default=paths.LEF25EVO_ORION_CSV, help="Utility 로 변환한 CSV 폴더")
    p.add_argument("--from", dest="first", default=paths.LEF25EVO_FSK_DAYS[0], help="시작일 (KST)")
    p.add_argument("--to", dest="last", default=paths.LEF25EVO_FSK_DAYS[1], help="마지막 날 (KST)")

    p = sub.add_parser("clocks", help="장비 시계 차이 측정 → derived/clock_offsets.csv")
    p.add_argument("--event", type=Path, default=paths.LEF25EVO_FSK)

    p = sub.add_parser("lef26-clock", help="LEF-26 로거 시계 검증 → derived/logger_clock.csv")
    p.add_argument("--event", type=Path, default=paths.LEF26_EVENT, help="로거와 영상이 둘 다 변환돼 있어야 한다")

    p = sub.add_parser("lef26-laps", help="영상 GPS 구간(랩·가속·스키드패드)에 로거 값을 붙인다 → derived/*_logger.csv")
    p.add_argument("--event", type=Path, default=paths.LEF26_EVENT, help="lef26-clock 까지 끝나 있어야 한다")

    p = sub.add_parser("em-laps", help="GPS 랩 · 주행 구간에 에너지미터 값을 붙인다 → derived/lap_energy.csv · run_energy.csv")
    p.add_argument("--event", type=Path, default=paths.LEF26_FSK, help="energymeter 변환과 랩 분할이 끝나 있어야 한다")

    p = sub.add_parser("report", help="하루치 시험 주행 보고서 → derived/report_<날짜>/index.html")
    p.add_argument("--event", type=Path, default=paths.LEF26_EVENT)
    p.add_argument("--day", required=True, help="시험일 (예: 2026-08-13)")
    p.add_argument("--car", default="lef26", choices=sorted(REPORT_BUILDERS), help="어느 차량의 데이터 읽는 법을 쓸지")
    p.add_argument("--pdf", action="store_true", help="PDF 도 만든다 (Chrome 또는 Edge 필요)")

    p = sub.add_parser("gopro", help="온보드 영상(GPMF) → normalized/")
    p.add_argument("--event", type=Path, required=True, help="이벤트 폴더 (raw_video/ 를 읽는다)")
    p.add_argument("--laps", action="store_true", help="변환 후 랩 분할까지 이어서 실행")
    p.add_argument("--venue", help="랩 분할에 쓸 경기장 이름 (laps 의 --venue 와 같다)")

    p = sub.add_parser("catalog", help="주행 데이터 카탈로그: 세션 페이지 → derived/catalog/, 첫 페이지 · 코드 페이지 → 07_데이터/_catalog/")
    p.add_argument("--event", type=Path, action="append", help="이벤트 폴더 (여러 번 지정 가능). 없으면 catalog_notes.toml 이 있는 이벤트 전부")
    p.add_argument("--notion", type=Path, help="노션용 마크다운도 쓴다 — 업로드 ID · 페이지 주소를 적은 JSON (→ _catalog/notion/)")

    p = sub.add_parser("sessions", help="분석 엔진(사이트용)이 읽는 세션 저장소 → 07_데이터/_sessions/<세션 ID>/")
    p.add_argument("--event", type=Path, action="append", help="이벤트 폴더 (여러 번 지정 가능). 없으면 catalog_notes.toml 이 있는 이벤트 전부")
    p.add_argument("--root", type=Path, help="저장소 위치 (기본: LEF_SESSION_ROOT 또는 07_데이터/_sessions)")
    p.add_argument("--reframe", action="store_true", help="그 이벤트들의 frame(기준선 · 구간 경계)을 다시 정한다 — 구간 시간 · 시간 차가 바뀐다")

    args = ap.parse_args(argv)
    t0 = time.perf_counter()

    if args.cmd == "monolith":
        print(f"[monolith] {args.event}")
        rows = monolith.convert_event(args.event)
        total_in = sum(r["input_bytes"] for r in rows)
        total_out = sum(r["output_bytes"] for r in rows)
        print(f"[monolith] 레코딩 {len(rows)}개, frames {sum(r['frames'] for r in rows):,}, "
              f"invalid {sum(r['invalid'] for r in rows)}, "
              f"{total_in/1e6:.1f}MB → {total_out/1e6:.1f}MB ({total_out/total_in:.0%}), "
              f"{time.perf_counter()-t0:.1f}s")

    elif args.cmd == "lef26":
        print(f"[lef26] {args.event}")
        rows = lef26.convert_event(args.event, args.session)
        print(f"[lef26] 레코딩 {len(rows)}개, frames {sum(r['frames'] for r in rows):,}, "
              f"dup {sum(r['dup'] for r in rows):,}, "
              f"시각 확보 {sum(r['time_trusted'] for r in rows)}/{len(rows)}, "
              f"{time.perf_counter()-t0:.0f}s")

    elif args.cmd == "motor-runs":
        print(f"[motor-runs] {args.event}")
        runs, _ = motor_runs.run_event(args.event)
        for rid, r in runs.groupby("recording_id"):
            print(f"  {rid:<20} run {len(r):>2}개  합계 {r['duration_s'].sum():>6.0f} s  "
                  f"SOC {r['soc_start_pct'].iloc[0]} → {r['soc_end_pct'].iloc[-1]} %  최대 전류 {r['iq_peak_a'].max():.0f} A")
        print(f"[motor-runs] run {len(runs)}개, 주행 시간 합계 {runs['duration_s'].sum()/60:.1f} 분")
        from . import plots
        figs = args.event / "derived" / "figures"
        figs.mkdir(exist_ok=True)
        timed = runs[runs["t_abs_start"].notna() & runs["soc_start_pct"].notna()]
        for rid in timed.groupby("recording_id")["duration_s"].sum().nlargest(2).index:
            print("  그림:", plots.lef26_discharge(args.event, rid, figs / f"discharge_{rid}.png").name)

    elif args.cmd == "laps":
        from . import venue
        print(f"[laps] {args.event}")
        ven = venue.for_event(args.event, args.venue)
        runs, lap_df, gate = laps.run_event(args.event, ven)
        print(f"[laps] 계측선 x={gate.x:.1f} m, y={gate.y:.1f} m, 방향 {gate.heading_deg:.0f}°" + (f" — 경기장 {ven['name']}" if ven else ""))
        for rid, r in runs.groupby("recording_id"):
            n = lap_df[lap_df["recording_id"] == rid]
            best = f"  최고 랩 {n['lap_time_s'].min():.2f}s" if len(n) else ""
            print(f"  {rid}  {r['label'].iloc[0] or '-':<15} run {len(r):>2}개  완주 랩 {len(n):>2}개{best}")
        from . import plots                       # matplotlib 은 그림 그릴 때만 불러온다
        figs = args.event / "derived" / "figures"
        figs.mkdir(exist_ok=True)
        circuit = lap_df[lap_df["label"].isin(["endurance", "endurance-race"])]
        if circuit.empty:
            print("  완주 랩 없음 - 트랙 지도·랩 추이 그림 생략")
        else:
            best_rec = circuit.loc[circuit["lap_time_s"].idxmin(), "recording_id"]
            print("  그림:", plots.track_map(args.event, best_rec, figs / "track_map_best_lap.png").name)
        race = sorted(lap_df.loc[lap_df["label"] == "endurance-race", "recording_id"].unique())
        if race:
            names = [f"Run {i} ({r[11:13]}:{r[13:15]})" for i, r in enumerate(race, 1)]
            print("  그림:", plots.endurance_laps(args.event, race, names, figs / "endurance_race_laps.png").name)

    elif args.cmd == "events":
        print(f"[events] {args.event}")
        accel, skid, arcs = events.run_event(args.event)
        fw = accel[accel["direction"] == "forward"] if not accel.empty else accel
        if not fw.empty:
            print(f"  가속 {len(accel)}회 (정방향 {len(fw)}) · 75 m 최속 {fw['t75_s'].min():.3f} s "
                  f"(범위 {fw['t75_s'].min():.3f}~{fw['t75_s'].max():.3f})")
        if not skid.empty:
            print(f"  스키드패드 {len(skid)}랩 · 최속 {skid['lap_time_s'].min():.3f} s · "
                  f"횡가속 최대 {skid['lat_acc_g'].max():.2f} g · 평균 반지름 {skid['circle_r_m'].mean():.2f} m")
        from . import plots
        figs = args.event / "derived" / "figures"
        figs.mkdir(exist_ok=True)
        if not fw.empty:
            print("  그림:", plots.accel_runs(args.event, figs / "accel_75m.png").name)
        if not skid.empty:
            print("  그림:", plots.skidpad(args.event, figs / "skidpad.png").name)

    elif args.cmd == "check":
        print(f"[check] {args.event}")
        # 리튬 셀·일반 차량 범위. 장비가 다르면 이 표만 바꾸면 된다
        limits = {"cell_max_v": (2.0, 4.3, "V"), "cell_min_v": (2.0, 4.3, "V"),
                  "temp_c": (-20, 80, "℃"), "soc_pct": (0, 100, "%"),
                  "speed_kmh": (0, 200, "km/h"), "course_deg": (0, 360, "°")}
        res = quality.check_event(args.event, limits, {"gps": None})
        bad = 0
        for rec, found in res.items():
            show = found if args.all else [f for f in found if f.level != quality.OK]
            if show:
                print("")
                print(f"  {rec}")
                for f in sorted(show, key=lambda f: f.level):
                    print(f"    {f}")
            bad += sum(1 for f in found if f.level != quality.OK)
        flat = [f for v in res.values() for f in v]
        n_err = sum(1 for f in flat if f.level == quality.ERROR)
        print("")
        print(f"[check] 레코딩 {len(res)}개 · 검사 {len(flat)}건 · "
              f"실패 {n_err} · 경고 {bad - n_err}")

    elif args.cmd == "energymeter":
        print(f"[energymeter] {args.event}")
        rows = energymeter.convert_event(args.event)
        hv = [r for r in rows if r["hv_on"]]
        print(f"[energymeter] 레코딩 {len(rows)}개 (HV 켜짐 {len(hv)}), "
              f"방전 합계 {sum(r['discharge_wh'] for r in hv)/1000:.2f} kWh, {time.perf_counter()-t0:.1f}s")

    elif args.cmd == "evo-stm32":
        print(f"[evo-stm32] {args.event}")
        rows = evo_stm32.convert_event(args.event)
        print(f"[evo-stm32] 레코딩 {len(rows)}개, 행 {sum(r['rows'] for r in rows):,}")
        if not args.no_align:
            al = evo_stm32.align_event(args.event, clock=args.clock)
            for _, r in al[al["em_windows"].notna() | al["gps_windows"].notna() | al["em_recording"].notna()].iterrows():
                mark = "적용" if r["applied"] else "보류"
                em = (f"전압 ↔ {r['em_recording']} 오차 {r['err_v']:.2f} V" + (f" f={r['em_f']:.4f} a={r['em_a_s']:+.1f}s 창 {int(r['em_windows'])}"
                      if r["em_windows"] == r["em_windows"] else "")) if isinstance(r["em_recording"], str) else ""
                gp = (f"속도 ↔ {r['gps_recording']} f={r['gps_f']:.4f} a={r['gps_a_s']:+.1f}s 창 {int(r['gps_windows'])}"
                      f"(2위 {int(r['gps_runner_up_windows'])})") if isinstance(r["gps_recording"], str) else ""
                print(f"  {r['recording_id']:<20} {int(r['duration_s']):>5} s  {em:<64} {gp:<58} "
                      f"→ {r['t_abs_start']}  f={r['clock_f']:.4f}({r['clock_basis']})  [{mark}] {r['note']}")
            if al["em_recording"].notna().any():
                print(f"  두 장비 전압 차이(STM32 - 미터) {al['offset_v'].iloc[0]:+.2f} V")
            meas = al[al["clock_basis"] != "prior"]["clock_f"]
            if len(meas):
                print(f"  잰 시계 속도 f (참 시간 = f × 로거 TIME): {meas.min():.4f} ~ {meas.max():.4f}, 중앙 {meas.median():.4f} ({len(meas)}개)")
            print(f"[evo-stm32] 시각 적용 {int(al['applied'].sum())} / 시계 속도를 잰 것 {len(meas)} / 전체 {len(al)}  "
                  f"(기준 시계: {'에너지미터' if args.clock == 'em' else 'Monolith'}, derived/stm32_alignment.csv), {time.perf_counter()-t0:.1f}s")

    elif args.cmd == "orion":
        print(f"[orion] {args.event}  ({args.first} ~ {args.last})")
        rows = orion.convert_event(args.event, args.src, args.first, args.last)
        for r in rows:
            if r["current_max_a"] > 30:
                print(f"  {r['recording_id']}  {r['duration_s']:>6.0f} s  I≤{r['current_max_a']:5.1f} A  "
                      f"SOC {r['soc_start_pct']:.1f} → {r['soc_end_pct']:.1f}  최고 {r['temp_max_c']:.0f} ℃  "
                      f"최저 셀 {r['cell_min_v']:.3f} V")
        print(f"[orion] 레코딩 {len(rows)}개, {time.perf_counter()-t0:.1f}s")

    elif args.cmd == "clocks":
        print(f"[clocks] {args.event}  (기준: Orion)")
        off = clocks.measure_event(args.event)
        if off.empty:
            print("  겹치는 장비 쌍을 찾지 못함")
        for dev, g in (off.groupby("device") if not off.empty else []):
            print(f"  {dev:<12} Orion 시계 = 이 장비 시각 {g['offset_to_orion_s'].median():+.0f} s  "
                  f"(세션 {len(g)}개, 범위 {g['offset_to_orion_s'].min():+d} ~ {g['offset_to_orion_s'].max():+d} s, "
                  f"상관 {g['corr'].min():.2f} ~ {g['corr'].max():.2f})")
        pc = clocks.power_cycles(args.event)
        if len(pc):
            for day, g in pc.groupby("day"):
                print(f"  전원 주기 {day}: Monolith ↔ 에너지미터 {len(g)}쌍, 에너지미터 시계가 Monolith 보다 "
                      f"{g['em_minus_monolith_s'].median():+.1f} s 앞섬 (범위 {g['em_minus_monolith_s'].min():+.1f} ~ "
                      f"{g['em_minus_monolith_s'].max():+.1f}), 꺼진 순간 차 {g['off_diff_s'].min():+.2f} ~ {g['off_diff_s'].max():+.2f} s")
        print(f"[clocks] derived/clock_offsets.csv · power_cycles.csv, {time.perf_counter()-t0:.1f}s")

    elif args.cmd == "lef26-clock":
        import pandas as pd
        print(f"[lef26-clock] {args.event}")
        clock, pairs, windows, speed = lef26_clock.run_event(args.event)
        names = {"gps_pps": "GPS 가 매초 맞춤", "free": "혼자 돎", "mixed": "도중에 바뀜", "unknown": "판정 불가"}
        modes = clock["mode"].value_counts()
        print("  시계 상태 (로그의 밀리초 분포):  " + " · ".join(f"{names[m]} {n}" for m, n in modes.items()))
        seen = clock[clock["f_basis"] == "video"]
        print(f"  영상과 겹친 레코딩 {len(seen)}개 (영상 {windows['video'].nunique()}개, 맞은 창 {len(windows)}개)")
        for mode in ("gps_pps", "free"):
            g = seen[seen["mode"] == mode]
            if g.empty:
                continue
            err = pd.concat([g["normalized_minus_true_first_obs_s"], g["normalized_minus_true_last_obs_s"]]).dropna()
            raw = g["logger_minus_true_at_end_s"].dropna()
            print(f"    {names[mode]:<12} 로거 시계 속도 {g['f'].min():.5f} ~ {g['f'].max():.5f} · "
                  f"보정 뒤 남은 속도 오차 {(g['f_residual'] - 1).abs().max():.2%} 이하 · "
                  f"normalized − 참 {err.min():+.2f} ~ {err.max():+.2f} s (보정 안 했다면 끝에서 {raw.min():+.0f} s 까지)")
        ticks = seen["tick_rate"].dropna()
        if len(ticks):
            print(f"  MCU 틱: 참 시간의 {ticks.median():.4f} 배 ({ticks.min():.4f} ~ {ticks.max():.4f}, 레코딩 {len(ticks)}개)")
        rest = clock[clock["f_basis"] != "video"]
        for basis, label in (("sd_sync", "SD 동기화 주기로 잼"), ("assumed", "근거 없어 가정")):
            g = rest[rest["f_basis"] == basis]
            if len(g):
                print(f"  영상 없는 레코딩 — {label}: {len(g)}개, 시계 속도 {g['f'].min():.5f} ~ {g['f'].max():.5f}")
        k = lef26_clock.speed_ratio(speed)
        if k is not None:
            print(f"  모터 속도 1 % = {k:.3f} km/h (문서값 {lef26_clock.KMH_PER_PCT_DOC:.3f} 의 {k / lef26_clock.KMH_PER_PCT_DOC:.3f} 배, "
                  f"영상 {int((speed['used'] >= lef26_clock.SPEED_SUMMARY_MIN).sum())}개)")
        print(f"[lef26-clock] derived/logger_clock.csv · logger_clock_pairs.csv · logger_clock_windows.csv · "
              f"speed_calibration.csv, {time.perf_counter()-t0:.1f}s")

    elif args.cmd == "lef26-laps":
        print(f"[lef26-laps] {args.event}")
        names = {"lap": "트랙 랩", "accel": "가속 75 m", "skidpad": "스키드패드"}
        for kind, df in lef26_laps.run_event(args.event).items():
            ok = df[df["cover_share"] >= lef26_laps.MIN_COVER]
            print(f"  {names[kind]:<8} {len(df):>3}구간 중 {len(ok)}구간에 로거 값 · "
                  f"로거 가속도 ↔ GPS 상관 {ok['align_corr'].min():.3f} ~ {ok['align_corr'].max():.3f} · "
                  f"GPS 지연 {ok['gps_lag_s'].min():+.2f} ~ {ok['gps_lag_s'].max():+.2f} s (중앙 {ok['gps_lag_s'].median():+.2f})")
            miss = df[df["cover_share"] < lef26_laps.MIN_COVER]
            for _, r in miss.iterrows():
                print(f"    로거 기록 없음: {r['recording_id']} {r['t_abs_start']} (덮은 비율 {r['cover_share']:.0%})")
        lap = pd_read(args.event / "derived" / "lap_logger.csv")
        if lap is not None and len(lap):
            best = lap.loc[lap["lap_time_s"].idxmin()]
            print(f"  최속 랩 {best['lap_time_s']:.2f} s ({best['recording_id']} run {best['run']} lap {best['lap']}): "
                  f"SOC {best['soc_start_pct']:.1f} → {best['soc_end_pct']:.1f} % · 전류 최대 {best['iq_peak_a']:.0f} A · "
                  f"팩 전압 최저 {best['vdc_min_v']} V · 최저 셀 {best['cell_min_v']:.3f} V · 배터리 최고 {best['batt_high_c']:.0f} ℃")
        from . import plots
        figs = args.event / "derived" / "figures"
        figs.mkdir(exist_ok=True)
        if lap is not None and len(lap):
            print("  그림:", plots.lap_logger(args.event, figs / "lap_logger.png").name)
        print(f"[lef26-laps] derived/lap_logger.csv · accel_logger.csv · skidpad_logger.csv, {time.perf_counter()-t0:.1f}s")

    elif args.cmd == "catalog":
        import json

        from . import catalog_build
        events = args.event or sorted(p.parent for p in paths.DATA_ROOT.glob(f"*/*/{catalog_build.NOTES_NAME}"))
        for ev in events:
            print(f"[catalog] {ev}")
            for row in catalog_build.write_event(ev):
                print(f"  {row['sid']}  {row['title']} · {row['duration_min']:.1f} 분 · {row['laps']}랩 · "
                      f"있는 데이터 {len(row['have']) + len(row['warn'])}종 · 찾는 데이터 {len(row['seek'])}종 · 그림 · 파일 {len(row['uploads'])}개")
        done = catalog_build.write_root(paths.DATA_ROOT)
        print(f"  세션 {len(done['sessions'])}개 · 변환된 레코딩 {done['recordings']}개 · 코드 페이지 {len(catalog_build.CODE_PAGES)}개 → {done['root']}")
        if args.notion:
            files = catalog_build.write_notion(paths.DATA_ROOT, json.loads(args.notion.read_text(encoding="utf-8")))
            print(f"  노션용 마크다운 {len(files)}개 → {files[0].parent}")
        print(f"[catalog] {time.perf_counter()-t0:.1f}s")

    elif args.cmd == "sessions":
        from . import catalog_build, session_store
        import json
        events = args.event or sorted(p.parent for p in paths.DATA_ROOT.glob(f"*/*/{catalog_build.NOTES_NAME}"))
        root = args.root or session_store.DEFAULT_ROOT
        for ev in events:
            print(f"[sessions] {ev}")
            for row in session_store.build_event(ev, root, reframe=args.reframe):
                have = [d["block"] for d in row["data_status"] if d["status"] in ("have", "warn")]
                laps = f"{row['laps']}랩" if row["laps"] is not None else "랩 없음 (시각 구간)"
                print(f"  {row['sid']}  {row['title']} · {row['duration_s'] / 60:.1f} 분 · {laps} · 데이터 {' · '.join(have)}")
        for f in sorted((root / session_store.FRAMES).glob("*.json")):
            fr = json.loads(f.read_text(encoding="utf-8"))
            ref = fr["reference"]
            print(f"  frame {fr['frame_key']}: 기준 랩 {ref['sid']} {ref['lap']}랩 · 기준 길이 {fr['ref_len_m']} m · 정한 때 {fr['fixed_at']}")
        print(f"[sessions] → {root}  {time.perf_counter()-t0:.1f}s")

    elif args.cmd == "em-laps":
        print(f"[em-laps] {args.event}")
        table, lap_df, run_df = em_laps.run_event(args.event)
        if table.empty:
            print("  전력 파형이 닮은 에너지미터 ↔ GPS 레코딩 쌍을 찾지 못함")
        for _, r in table.iterrows():
            print(f"  {r['em_recording']} ↔ {r['gps_recording']}  미터 시계가 GPS 보다 {r['em_minus_gps_s']:+.2f} s · "
                  f"시계 속도 f={r['f']:.5f} · 맞은 창 {r['inliers']}/{r['windows']} (잔차 {r['rms_s']:.3f} s) · "
                  f"상관 {r['corr_median']:.3f}  [{'적용' if r['accepted'] else '보류'}]")
        done = lap_df[lap_df["discharge_wh"].notna()] if len(lap_df) else lap_df
        for (rid, run), g in (done.groupby(["recording_id", "run"]) if len(done) else []):
            print(f"  {rid} run {run}: {len(g)}랩 · 랩당 {g['net_wh'].min():.0f} ~ {g['net_wh'].max():.0f} Wh (중앙 {g['net_wh'].median():.0f}) · "
                  f"{g['wh_per_km'].median():.0f} Wh/km · 전류 최대 {g['i_max_a'].max():.0f} A · 500 ms 평균 전력 최대 {g['p500_max_kw'].max():.1f} kW · "
                  f"랩별 어긋남 {g['em_lag_s'].min():+.2f} ~ {g['em_lag_s'].max():+.2f} s")
        if len(lap_df) > len(done):
            print(f"  에너지미터 기록이 없는 랩 {len(lap_df) - len(done)}개 (값을 비워 둠)")
        print(f"[em-laps] derived/em_alignment.csv · lap_energy.csv · run_energy.csv, {time.perf_counter()-t0:.1f}s")

    elif args.cmd == "report":
        from . import report
        print(f"[report] {args.event}  {args.day}")
        day = REPORT_BUILDERS[args.car](args.event, args.day)
        if not day.notes:
            print(f"  report_notes/{args.day}.toml 이 없다 — 시험 목적 · 판단 · 조치가 '기록 없음' 으로 나온다")
        out = args.event / "derived" / f"report_{args.day}"
        path = report.write_report(day, out)
        print(f"  주행 {len(day.sessions)}구간 (랩 기록 {len(day.gps_sessions())}) · 그림 {len(list((out / 'figures').glob('*.png')))}장")
        for e in day.events:
            print(f"  특이 사항: {e['time']:%H:%M:%S} {e['session']} {e['what']}")
        print(f"  {path}")
        if args.pdf:
            pdf = report.write_pdf(path)
            print(f"  {pdf}" if pdf else "  PDF 를 만들지 못했다 — Chrome 이나 Edge 를 찾지 못함")
        print(f"[report] {time.perf_counter()-t0:.1f}s")

    elif args.cmd == "venue":
        from . import venue
        doc = venue.create_from_event(args.name, args.from_event)
        print(f"[venue] {venue.summary(doc)} → {venue.path(args.name)}")
        runs, lap_df, _ = laps.run_event(args.from_event, doc)     # 그 이벤트도 경기장 frame 으로 (같은 선 · 같은 원점이라 랩은 그대로)
        print(f"[venue] {args.from_event.name}: 경기장으로 다시 나눔 — 주행 {len(runs)}구간 · 완주 {len(lap_df)}랩. "
              f"catalog_notes.toml 의 [event] 에 venue = \"{args.name}\" 를 적어 둘 것")

    elif args.cmd == "gopro":
        print(f"[gopro] {args.event}")
        rows = video.convert_event(args.event)
        if not rows:
            print("  변환된 녹화 없음 - raw_video/ 와 GPS fix 를 확인할 것")
        elif args.laps:
            from . import venue
            ven = venue.for_event(args.event, args.venue)
            print("[laps] 이어서 랩 분할" + (f" — 경기장 {ven['name']}" if ven else ""))
            runs, laps_df, _ = laps.run_event(args.event, ven)
            print(f"  주행 {len(runs)}구간 · 완주 {len(laps_df)}랩 · 최속 {laps_df['lap_time_s'].min():.2f} s")
            from . import plots
            figs = args.event / "derived" / "figures"
            figs.mkdir(exist_ok=True)
            for rid in laps_df["recording_id"].unique():
                out = plots.stint_laps(args.event, figs / f"stint_laps_{rid}.png", rid)
                print("  그림:", out.name)


def _lef26_day(event_dir: Path, day: str):
    from . import lef26_report
    return lef26_report.build_day(event_dir, day)


# 차량 → 그 차량의 데이터를 읽어 report.DayReport 를 채우는 함수. LEF-27 은 여기에 한 줄 더한다
REPORT_BUILDERS = {"lef26": _lef26_day}


def pd_read(path: Path):
    """있으면 CSV 를 읽는다 (pandas 는 필요할 때만 불러온다)."""
    if not path.exists():
        return None
    import pandas as pd
    return pd.read_csv(path, encoding="utf-8-sig")


if __name__ == "__main__":
    main()
