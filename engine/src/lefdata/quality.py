"""데이터 품질 검사 — "이 데이터 믿어도 되나"에 답한다.

우리가 과거 데이터를 뜯으면서 실제로 데인 것들을 검사로 만들었다.
전부 **어떤 팀 데이터에도 적용되는 일반 검사**다. 우리 로거·폴더 구조를 모른다.

왜 필요한가 — 겪은 일들

    체크섬을 통과한 셀 전압이 1 V 도 안 됐다      → 체크섬만 믿으면 안 된다
    지자기 크기가 µT 처럼 보였는데 아니었다       → 그럴듯한 값이 가장 위험하다
    IMU 와 GPS 가 서로 어긋나 있었다              → 독립 측정끼리 대조해야 안다
    시계가 뒤로 감아서 레코딩을 버려야 했다       → 시간축부터 의심한다
    줄 대부분이 직전 줄과 같았다                  → 양이 많다고 정보가 많은 게 아니다

검사는 통과/실패가 아니라 **측정값과 판정**을 같이 돌려준다.
숫자를 봐야 "얼마나 나쁜지"를 알 수 있고, 기준은 팀마다 다르기 때문이다.

기준값은 전부 인자로 받는다. 기본값은 일반적인 것만 두고, 장비 고유 값
(정격 전류, 셀 화학에 따른 전압 범위 등)은 호출하는 쪽에서 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

OK, WARN, ERROR = "ok", "warn", "error"


@dataclass
class Finding:
    """검사 하나의 결과."""

    check: str
    level: str
    message: str
    value: float | None = None
    detail: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.level == ERROR

    def __str__(self) -> str:
        mark = {OK: "OK  ", WARN: "WARN", ERROR: "FAIL"}[self.level]
        return f"[{mark}] {self.check}: {self.message}"


def _f(x) -> float | None:
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else float(x)


# ---------------------------------------------------------------- 값의 범위

def in_range(values, lo: float, hi: float, name: str, unit: str = "",
             max_bad_ratio: float = 0.0) -> Finding:
    """물리적으로 가능한 범위를 벗어난 값이 있는가.

    체크섬을 통과하고도 깨진 값이 섞여 들어온다. 8비트 합 체크섬은
    물리적으로 불가능한 셀 전압도 쉽게 통과시킨다. **범위 검사는 체크섬과 별개로 필요하다.**
    """
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if not len(v):
        return Finding(f"range:{name}", WARN, "값이 없다")
    bad = int(((v < lo) | (v > hi)).sum())
    ratio = bad / len(v)
    level = OK if ratio <= max_bad_ratio else (WARN if ratio < 0.01 else ERROR)
    return Finding(
        f"range:{name}", level,
        f"{bad:,}/{len(v):,}건 ({ratio:.3%})이 {lo}~{hi} {unit} 밖. "
        f"실측 {v.min():.4g}~{v.max():.4g} {unit}",
        ratio, {"bad": bad, "n": len(v), "min": _f(v.min()), "max": _f(v.max())})


def is_constant(values, name: str) -> Finding:
    """전 구간이 같은 값인가 — 센서가 안 붙었거나 배선이 끊긴 신호를 잡는다.

    LEF-26 `PED` 는 114,352행이 전부 0 이었다. 센서를 연결하지 못한 채 수집한 것이다.
    **행이 많다고 데이터가 있는 게 아니다.**
    """
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if not len(v):
        return Finding(f"constant:{name}", WARN, "값이 없다")
    uniq = np.unique(v)
    if len(uniq) == 1:
        return Finding(f"constant:{name}", ERROR,
                       f"{len(v):,}행이 전부 {uniq[0]:g} (센서 미연결 의심)", 1.0)
    return Finding(f"constant:{name}", OK, f"고유값 {len(uniq):,}개", float(len(uniq)))


def bearing_range(course_deg, name: str = "course") -> Finding:
    """진행 방위가 0~360 안인가."""
    return in_range(course_deg, 0.0, 360.0, name, "°")


# ---------------------------------------------------------------- 시간축

def monotonic_time(t_ms, name: str = "t", tol_ms: float = 0.0) -> Finding:
    """시각이 뒤로 감지 않는가.

    LEF-26 로거는 RTC 백업이 없어 시계가 되감겼다. 64개 레코딩 중 31개는
    절대시각을 복원하지 못했다. **시간축이 깨지면 다른 분석이 전부 무의미해진다.**

    `tol_ms` 로 허용치를 준다. 시계 점프를 이어 붙이는 식으로 복원했다면
    이음매에서 작은 역행이 남는 게 정상이다. 그런 한계를 아는 경우에만 넘긴다.
    """
    t = np.asarray(t_ms, dtype=float)
    if len(t) < 2:
        return Finding(f"time:{name}", WARN, "표본이 너무 적다")
    d = np.diff(t)
    back = d[d < 0]
    if not len(back):
        return Finding(f"time:{name}", OK, "단조 증가", 0.0)
    worst = float(back.min())
    level = WARN if abs(worst) <= tol_ms else ERROR
    return Finding(f"time:{name}", level,
                   f"{len(back):,}번 뒤로 감았다 (최대 {worst:.0f} ms)",
                   float(len(back)), {"worst_ms": worst})


def gaps(t_s, max_gap_s: float, name: str = "t") -> Finding:
    """기록이 끊긴 구간이 있는가.

    시판 로거라고 안심할 수 없다 — 기록이 수십 초씩 비는 일이 있다.
    """
    t = np.sort(np.asarray(t_s, dtype=float))
    if len(t) < 2:
        return Finding(f"gap:{name}", WARN, "표본이 너무 적다")
    d = np.diff(t)
    big = d[d > max_gap_s]
    if not len(big):
        return Finding(f"gap:{name}", OK, f"{max_gap_s} s 넘는 공백 없음", 0.0)
    return Finding(f"gap:{name}", WARN,
                   f"{len(big)}곳에 공백 (최대 {big.max():.1f} s, 합계 {big.sum():.1f} s)",
                   _f(big.max()), {"count": len(big), "total_s": _f(big.sum())})


def sample_rate(t_s, expect_hz: float | None = None, tol: float = 0.3,
                name: str = "rate") -> Finding:
    """실제 기록 속도가 기대치와 맞는가.

    CAN 주기를 정해 두어도 신호별 실제 기록 속도는 제각각일 수 있다.
    설정대로 안 들어오면 **수신 손실**을 의심해야 한다.
    """
    t = np.asarray(t_s, dtype=float)
    if len(t) < 2:
        return Finding(name, WARN, "표본이 너무 적다")
    span = t.max() - t.min()
    hz = len(t) / span if span > 0 else float("nan")
    if expect_hz is None:
        return Finding(name, OK, f"{hz:.1f} Hz", _f(hz))
    off = abs(hz - expect_hz) / expect_hz
    level = OK if off <= tol else (WARN if off <= tol * 2 else ERROR)
    return Finding(name, level, f"{hz:.1f} Hz (기대 {expect_hz:g} Hz, 차이 {off:.0%})", _f(hz))


def no_overlap(intervals: list[tuple], name: str = "recordings") -> Finding:
    """레코딩끼리 시간이 겹치지 않는가 — 시각 복원이 맞는지 보는 교차 검증.

    한 대의 로거가 같은 시각에 두 번 기록할 수는 없다. 겹치면 복원이 틀린 것이다.
    """
    iv = sorted((a, b) for a, b in intervals if a is not None and b is not None)
    bad = [(iv[i], iv[i + 1]) for i in range(len(iv) - 1) if iv[i][1] > iv[i + 1][0]]
    return Finding(f"overlap:{name}", OK if not bad else ERROR,
                   f"{len(iv)}개 중 겹침 없음" if not bad else f"{len(bad)}쌍이 겹친다",
                   float(len(bad)))


# ---------------------------------------------------------------- 센서 교차검증

def gravity_magnitude(ax_g, ay_g, az_g, still_mask=None, tol: float = 0.02) -> Finding:
    """정지 상태에서 가속도 크기가 1 g 인가 — IMU 배율·영점을 보는 가장 싼 검사.

    LEF-25evo 는 정지 상태에서 1.07 g 가 나왔다. 축 방향을 알아내고도
    **크기를 믿을 수 없어 절댓값을 쓰지 못했다.**
    """
    a = np.column_stack([np.asarray(x, dtype=float) for x in (ax_g, ay_g, az_g)])
    if still_mask is not None:
        a = a[np.asarray(still_mask, dtype=bool)]
    a = a[np.isfinite(a).all(axis=1)]
    if len(a) < 100:
        return Finding("imu:gravity", OK,
                       "정지 구간 표본이 부족해 건너뜀 (주행 중 값이 섞이면 1 g 를 넘는 게 정상)")
    mag = float(np.median(np.linalg.norm(a, axis=1)))
    off = abs(mag - 1.0)
    level = OK if off <= tol else (WARN if off <= tol * 3 else ERROR)
    return Finding("imu:gravity", level,
                   f"정지 시 가속도 크기 {mag:.4f} g (1.0 에서 {off:+.1%})", mag)


def cross_check(a, b, name_a: str, name_b: str, tol: float = 0.1) -> Finding:
    """독립적인 두 측정이 같은 값을 말하는가.

    같은 물리량을 서로 다른 장치가 재고 있다면 맞아야 한다. 어긋나면 한쪽이 틀린 것이다.
    이 검사로 IMU 의 크기가 GPS 와 눈에 띄게 어긋난 것을 찾은 일이 있다.
    """
    x, y = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 10:
        return Finding(f"cross:{name_a}~{name_b}", WARN, "대조할 표본이 부족하다")
    mx, my = float(np.median(np.abs(x[m]))), float(np.median(np.abs(y[m])))
    if my == 0:
        return Finding(f"cross:{name_a}~{name_b}", WARN, f"{name_b} 가 0 이라 비교 불가")
    ratio = mx / my
    off = abs(ratio - 1.0)
    level = OK if off <= tol else (WARN if off <= tol * 3 else ERROR)
    return Finding(f"cross:{name_a}~{name_b}", level,
                   f"{name_a}/{name_b} = {ratio:.3f} ({off:+.1%})", ratio,
                   {"corr": _f(np.corrcoef(x[m], y[m])[0, 1])})


# ---------------------------------------------------------------- 중복·보존

def duplicate_rate(df, subset=None, max_ratio: float = 0.2) -> Finding:
    """직전 행과 완전히 같은 행의 비율.

    줄의 절반 넘게가 직전 줄과 시각까지 똑같았던 일이 있다.
    저장 용량과 쓰기 부하를 그만큼 버린 것이다.
    """
    d = df if subset is None else df[list(subset)]
    if len(d) < 2:
        return Finding("duplicate", WARN, "표본이 너무 적다")
    same = int((d.iloc[1:].to_numpy() == d.iloc[:-1].to_numpy()).all(axis=1).sum())
    ratio = same / (len(d) - 1)
    return Finding("duplicate", OK if ratio <= max_ratio else WARN,
                   f"직전 행과 동일 {same:,}/{len(d) - 1:,} ({ratio:.1%})", ratio)


def row_count_preserved(before: int, after: int, name: str = "rows") -> Finding:
    """가공 전후로 행이 사라지지 않았는가.

    남이 나눠 준 파일에서 한 종류의 줄이 통째로 빠져 있던 일이 있다.
    **도와주려고 정리한 것이 정보를 깎는다.** 가공할 때마다 세야 한다.
    """
    lost = before - after
    return Finding(f"preserved:{name}", OK if lost == 0 else ERROR,
                   f"{before:,} → {after:,}" + ("" if lost == 0 else f" / {lost:,}건 사라짐"),
                   float(lost))


# ---------------------------------------------------------------- 묶음

def check_gps(df, expect_hz: float | None = None, max_gap_s: float = 5.0) -> list[Finding]:
    """GPS 테이블 한 장에 대한 기본 검사 묶음."""
    out = [monotonic_time(df["t_rel_ms"], "gps")]
    t_s = np.asarray(df["t_rel_ms"], dtype=float) / 1000.0
    out.append(sample_rate(t_s, expect_hz, name="rate:gps"))
    out.append(gaps(t_s, max_gap_s, "gps"))
    if "course_deg" in df:
        out.append(bearing_range(df["course_deg"]))
    if "lat" in df:
        out.append(in_range(df["lat"], -90, 90, "lat", "°"))
        out.append(in_range(df["lon"], -180, 180, "lon", "°"))
    if "fix" in df and len(df):
        rate = float(np.asarray(df["fix"], dtype=bool).mean())
        out.append(Finding("gps:fix", OK if rate > 0.95 else WARN,
                           f"fix 비율 {rate:.1%}", rate))
    return out


def check_imu(df, cols=("ax_g", "ay_g", "az_g"), g_limit: float = 16.0,
              still_speed=None) -> list[Finding]:
    """IMU 테이블 기본 검사. `still_speed` 를 주면 정지 구간에서 중력 크기를 본다."""
    out = [monotonic_time(df["t_rel_ms"], "imu")]
    for c in cols:
        if c in df:
            out.append(in_range(df[c], -g_limit, g_limit, c, "g"))
    if all(c in df for c in cols):
        mask = None
        if still_speed is not None:
            mask = np.asarray(still_speed, dtype=float) < 2.0
        out.append(gravity_magnitude(df[cols[0]], df[cols[1]], df[cols[2]], mask))
    return out


def summarize(findings: list[Finding]) -> str:
    """사람이 읽는 요약. 나쁜 것부터 보여준다."""
    order = {ERROR: 0, WARN: 1, OK: 2}
    lines = [str(f) for f in sorted(findings, key=lambda f: (order[f.level], f.check))]
    n = {k: sum(1 for f in findings if f.level == k) for k in (ERROR, WARN, OK)}
    lines.append(f"\n  실패 {n[ERROR]} · 경고 {n[WARN]} · 통과 {n[OK]}")
    return "\n".join(lines)


def worst(findings: list[Finding]) -> str:
    """묶음 전체의 판정."""
    if any(f.level == ERROR for f in findings):
        return ERROR
    return WARN if any(f.level == WARN for f in findings) else OK


def check_event(event_dir, limits: dict | None = None, rates: dict | None = None,
                time_tol_ms: float = 0.0) -> dict:
    """이벤트의 normalized/ 전체를 훑어 레코딩별 검사 결과를 낸다.

    ⚠ 이 함수만 우리 폴더 구조(`normalized/<레코딩>/<테이블>.parquet`)를 안다.
       위의 검사 함수들은 전부 배열·표만 받으므로 어디서든 쓸 수 있다.
    """
    import pandas as pd
    import pyarrow.parquet as pq
    from pathlib import Path

    event_dir = Path(event_dir)
    limits = limits or {}
    rates = rates or {}
    out: dict[str, list[Finding]] = {}
    spans: dict[str, list] = {}              # 장비별로 모은다 — 서로 다른 장비(로거와 카메라)는 같은 시각에 기록하는 게 정상이다

    for rec in sorted(p for p in (event_dir / "normalized").iterdir() if p.is_dir()):
        found: list[Finding] = []
        # IMU 중력 검사에는 정지 구간이 필요하다. GPS 속도를 먼저 읽어 둔다.
        still_t = still_v = None
        gps_path = rec / "gps.parquet"
        if gps_path.exists() and pq.ParquetFile(gps_path).metadata.num_rows:
            g = pq.read_table(gps_path).to_pandas()
            if "fix" in g:
                g = g[g["fix"]]
            if "speed_kmh" in g and len(g) > 10:
                still_t = g["t_rel_ms"].to_numpy(dtype=float)
                still_v = g["speed_kmh"].to_numpy(dtype=float)

        for tbl in sorted(rec.glob("*.parquet")):
            if tbl.stem in ("frames",):
                continue
            meta = pq.ParquetFile(tbl).metadata
            if meta.num_rows == 0:
                continue
            df = pq.read_table(tbl).to_pandas()
            if tbl.stem == "gps":
                found += check_gps(df, rates.get("gps"))
                if "t_abs" in df and df["t_abs"].notna().any():
                    spans.setdefault(_device(rec), []).append((df["t_abs"].min(), df["t_abs"].max()))
            elif tbl.stem == "imu":
                cols = [c for c in df.columns if c.endswith("_g")][:3]
                if len(cols) == 3:
                    sp = None
                    if still_t is not None:
                        sp = np.interp(df["t_rel_ms"].to_numpy(dtype=float), still_t, still_v)
                    found += check_imu(df, tuple(cols), still_speed=sp)
                else:
                    found.append(monotonic_time(df["t_rel_ms"], "imu", time_tol_ms))
            elif "t_rel_ms" in df:
                found.append(monotonic_time(df["t_rel_ms"], tbl.stem, time_tol_ms))
            # 값 범위: 호출자가 준 것만 본다 (장비마다 다르므로 기본값을 강요하지 않는다)
            for col, (lo, hi, unit) in limits.items():
                if col in df.columns:
                    found.append(in_range(df[col], lo, hi, f"{tbl.stem}.{col}", unit))
            # 전 구간 같은 값인 신호 (센서 미연결)
            for col in df.columns:
                if col not in ("t_rel_ms", "t_abs", "t_quality", "fix", "valid") \
                        and pd.api.types.is_numeric_dtype(df[col]) and len(df) > 100:
                    f = is_constant(df[col], f"{tbl.stem}.{col}")
                    if f.failed:
                        found.append(f)
        out[rec.name] = found

    checks = [no_overlap(iv, "recordings" if len(spans) == 1 else f"recordings[{dev}]")
              for dev, iv in sorted(spans.items()) if len(iv) > 1]
    if checks:
        out["_event"] = checks
    return out


def _device(rec_dir) -> str:
    """레코딩을 만든 장비 이름 (meta.json 의 source.logger). 없으면 빈 문자열 — 전부 한 장비로 본다."""
    import json
    try:
        return str(json.loads((rec_dir / "meta.json").read_text(encoding="utf-8")).get("source", {}).get("logger", ""))
    except (OSError, ValueError):
        return ""
