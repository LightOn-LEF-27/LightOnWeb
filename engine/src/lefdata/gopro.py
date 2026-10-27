"""GoPro 영상에 박혀 있는 텔레메트리(GPMF) 파서.

GoPro 는 MP4 안에 `gpmd` 라는 데이터 트랙을 두고 GPS·가속도·자이로를 같이 기록한다.
저해상도 프록시(.LRV) 에도 같은 트랙이 들어 있어서, MP4 가 없어도 텔레메트리는 뽑을 수 있다.

먼저 ffmpeg 로 트랙만 꺼낸 뒤 이 모듈로 읽는다.

    ffmpeg -i GOPR3331.MP4 -map 0:d:1 -c copy -f data gpmf.bin

구조는 KLV 다 — FourCC(4) + 타입(1) + 샘플 크기(1) + 반복 수(2) + 데이터(4바이트 경계 정렬).
타입이 0 이면 중첩 컨테이너(DEVC, STRM)다.

주요 키
    GPS5  위도·경도·고도·2D 속도·3D 속도   18 Hz   (SCAL 로 나눠야 물리값)
    GPSU  UTC 시각 'YYMMDDHHMMSS.sss'      1 Hz
    GPSF  fix 상태 (3 = 3D fix)            1 Hz
    GPSP  DOP x 100 (수신 품질, 작을수록 좋다) 1 Hz — 변환 시 100 으로 나눈다
    ACCL  가속도 m/s²                      ~200 Hz
    GYRO  각속도 rad/s                     ~400 Hz

⚠ STNM(스트림 이름)은 데이터 키보다 앞에 나오므로 단순 파싱 시 이름이 한 칸 밀린다.
   값 자체는 영향 없지만 이름은 믿지 말 것.

검증: 실제 대회 영상에서 GPS 점 대부분이 fix=3 이었고, 궤적이 다른 로거로 그린 트랙과 1 m 안에서 겹쳤다.
"""

import struct
from pathlib import Path

import numpy as np

FMT = {b'b':'b', b'B':'B', b's':'>h', b'S':'>H', b'l':'>i', b'L':'>I',
       b'f':'>f', b'd':'>d', b'j':'>q', b'J':'>Q'}

def walk(buf, start=0, end=None, depth=0):
    end = len(buf) if end is None else end
    p = start
    while p + 8 <= end:
        fourcc = buf[p:p+4]; typ = buf[p+4:p+5]; ssize = buf[p+5]
        rep = struct.unpack('>H', buf[p+6:p+8])[0]
        dlen = ssize * rep
        data = buf[p+8:p+8+dlen]
        yield depth, fourcc, typ, ssize, rep, data
        if typ == b'\x00':                       # 중첩 컨테이너
            yield from walk(buf, p+8, p+8+dlen, depth+1)
        p += 8 + ((dlen + 3) // 4) * 4

def nums(typ, ssize, rep, data):
    f = FMT.get(typ)
    if not f: return None
    n = struct.calcsize(f.lstrip('>'))
    cnt = ssize // n
    vals = struct.unpack('>' + f.lstrip('>') * (cnt * rep), data[:n*cnt*rep])
    return np.array(vals, dtype=float).reshape(rep, cnt)

def collect(path):
    buf = open(path, 'rb').read()
    out = {}; scal = None; names = {}; cur = None
    for depth, fc, typ, ss, rep, data in walk(buf):
        if fc == b'STRM': scal = None
        elif fc == b'SCAL': scal = nums(typ, ss, rep, data).ravel()
        elif fc == b'STNM': names[cur] = data.rstrip(b'\x00').decode('latin1', 'replace')
        elif fc in (b'GPS5', b'ACCL', b'GYRO', b'GPSF', b'GPSP', b'TMPC', b'GPSU'):
            cur = fc
            if fc == b'GPSU':
                out.setdefault(fc, []).append(data.rstrip(b'\x00').decode('latin1','replace'))
            else:
                v = nums(typ, ss, rep, data)
                if v is not None:
                    if scal is not None and len(scal):
                        v = v / (scal if len(scal) == v.shape[1] else scal[0])
                    out.setdefault(fc, []).append(v)
    return out, names


# ---------------------------------------------------------------- 영상 → 표 (순수 계층)

GPS_HZ_NOMINAL = 18          # HERO5 의 GPS5 기록 속도. 블록 안에서 샘플을 균등 분할할 때만 쓴다
FIX_3D = 3                   # GPSF. 3 = 3D fix


def extract_stream(video: Path, out_bin: Path) -> Path:
    """ffmpeg 로 영상에서 `gpmd`(텔레메트리) 트랙만 뽑아낸다. 영상은 건드리지 않는다."""
    import subprocess
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "d", "-show_entries", "stream=index,codec_tag_string",
         "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    idx = [i for i, (n, tag) in enumerate(
        (l.split(",") for l in probe.stdout.strip().splitlines() if "," in l)) if tag.strip() == "gpmd"]
    if not idx:
        raise RuntimeError(f"gpmd 트랙이 없다: {video.name}")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video),
                    "-map", f"0:d:{idx[0]}", "-c", "copy", "-f", "data", str(out_bin)], check=True)
    return out_bin


def _gpsu_to_utc(s: str):
    """GPSU 'YYMMDDHHMMSS.sss' → UTC datetime. 형식이 깨지면 None."""
    import datetime as dt
    try:
        base = dt.datetime.strptime(s[:12], "%y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    frac = float("0." + s.split(".")[1]) if "." in s else 0.0
    return base + dt.timedelta(seconds=frac)


GPS_COLS = ["t_utc", "fix", "lat", "lon", "alt_m", "speed_kmh", "dop"]
IMU_COLS = ["t_utc", "a0_g", "a1_g", "a2_g", "g0_dps", "g1_dps", "g2_dps", "t_ok"]
BLOCK_MAX_S = 2.0            # 이웃 블록의 GPSU 차이가 이 안이면 이어진 녹화로 본다
IMU_LATE_RANGE_S = (-0.5, 3.0)   # IMU 가 GPS 보다 늦게 찍힌 정도를 찾는 범위. 실측은 0 · 1 · 2 s 뿐이었다
IMU_CAL_MIN_CORR = 0.95


def read_blocks(bin_paths: list[Path]) -> list[dict]:
    """추출한 gpmd 조각들 → 블록(약 1 초) 목록. 파일 이름 순서 = 녹화 순서 (GOPR… < GP01… < GP02…)."""
    blocks = []
    for p in sorted(bin_paths):
        out, _ = collect(p)
        if b"GPS5" not in out:
            continue
        gps_blocks, times = out[b"GPS5"], out.get(b"GPSU", [])
        fixes = np.vstack(out[b"GPSF"]).ravel() if b"GPSF" in out else np.full(len(gps_blocks), FIX_3D)
        # GPSP 는 DOP x 100 으로 저장된다 (GoPro GPMF 규격). 217 -> 2.17
        dops = np.vstack(out[b"GPSP"]).ravel() / 100.0 if b"GPSP" in out else np.full(len(gps_blocks), np.nan)
        acc, gyr = out.get(b"ACCL", []), out.get(b"GYRO", [])
        for i, blk in enumerate(gps_blocks):
            t0 = _gpsu_to_utc(times[i]) if i < len(times) else None
            blocks.append({
                "file": Path(p).stem,
                "t0_s": None if t0 is None else t0.timestamp(),
                "fix": int(fixes[i]) if i < len(fixes) else 0,
                "dop": float(dops[i]) if i < len(dops) else float("nan"),
                "gps": blk,
                "acc": acc[i] if i < len(acc) else None,
                "gyr": gyr[i] if i < len(gyr) else None,
            })
    return blocks


def _resample_gyro(gyr: np.ndarray, n: int) -> np.ndarray:
    """자이로(약 400 Hz) 블록을 가속도(약 200 Hz) 샘플 n 개의 시각에 맞춘다.

    두 센서의 블록당 샘플 수가 다르다 (HERO5: 가속도 197, 자이로 395 안팎). 앞에서부터 짝지으면
    자이로의 앞 절반만 쓰게 되어 1 초 안에서 시간이 2 배로 늘어난다 (2026-10-01 에 고친 버그).
    이웃한 r 개를 평균해 속도를 낮춘 뒤, 블록 안 상대 위치로 보간한다.
    """
    r = max(1, int(round(len(gyr) / n)))
    low = gyr[: (len(gyr) // r) * r].reshape(-1, r, gyr.shape[1]).mean(axis=1)
    src = (np.arange(len(low)) + 0.5) / len(low)
    dst = (np.arange(n) + 0.5) / n
    return np.column_stack([np.interp(dst, src, low[:, k]) for k in range(gyr.shape[1])])


def course_rate(t_s, lat, lon, speed_kmh, half_s: float = 0.25, min_kmh: float = 15.0) -> np.ndarray:
    """GPS 궤적이 휘는 속도 (°/s, 좌회전 양수). 자이로 없이 위치만으로 구한 요레이트다.

    앞뒤 half_s 초 떨어진 두 점을 이은 방향을 진행 방위로 보고 그 변화율을 낸다.
    느릴 때(min_kmh 미만)는 위치 잡음에 방위가 흔들리므로 NaN.
    """
    t_s, lat, lon = (np.asarray(v, float) for v in (t_s, lat, lon))
    x = np.radians(lon) * np.cos(np.radians(np.nanmean(lat))) * 6371000.0
    y = np.radians(lat) * 6371000.0
    k = max(1, int(round(half_s / np.median(np.diff(t_s)))))
    if len(t_s) <= 2 * k + 1:
        return np.full(len(t_s), np.nan)
    course = np.full(len(t_s), np.nan)
    course[k:-k] = np.arctan2(x[2 * k:] - x[:-2 * k], y[2 * k:] - y[:-2 * k])          # 북=0, 시계방향
    rate = -np.degrees(np.gradient(np.unwrap(np.nan_to_num(course)), t_s))
    rate[~np.isfinite(course) | (np.asarray(speed_kmh, float) < min_kmh)] = np.nan
    return rate


def imu_late_by(gps_t_s, lat, lon, speed_kmh, imu_t_s, acc_g, gyro_dps, step: float = 0.05,
                win_s: float = 60.0, min_valid_s: float = 20.0) -> dict | None:
    """IMU 표본이 GPS 표본보다 몇 초 늦게 찍혀 있는지 잰다. 잴 수 없으면 None.

    같은 카메라 안에서도 두 스트림은 어긋나 있다 (아래 read_recording 설명). GPS 궤적이 휘는 속도와
    자이로의 요레이트는 같은 물리량이므로, 둘을 겹쳐 시간차를 구한다. 차가 **돌면서 달린** 구간이 있어야 한다.
    60 s 창마다 재서 상관이 0.95 넘는 창들의 중앙값을 쓴다.
    반환: late_s (IMU 가 늦은 초), windows (쓴 창 수), corr (그 창들의 상관 중앙값)
    """
    from . import sync

    gps_t_s, imu_t_s, speed_kmh = (np.asarray(v, float) for v in (gps_t_s, imu_t_s, speed_kmh))
    if len(gps_t_s) < 50 or len(imu_t_s) < 50:
        return None
    up = vertical_axis(acc_g, imu_t_s, gps_t_s[speed_kmh < 1.0])
    a0, a = sync.grid(gps_t_s, course_rate(gps_t_s, lat, lon, speed_kmh), step)
    b0, b = sync.grid(imu_t_s, yaw_rate(gyro_dps, up), step)
    if not len(a) or not len(b):
        return None
    # 격자는 [칸 번호 × step] 이라 a0 는 '값이 있는 첫 칸' 이다. 저속(NaN) 구간이 앞에 있으면 그만큼 뒤에서 시작한다
    w = int(round(win_s / step))
    lags = np.arange(int(round(IMU_LATE_RANGE_S[0] / step)), int(round(IMU_LATE_RANGE_S[1] / step)) + 1)
    found = []
    for s in range(0, max(len(a) - w, 0) + 1, w // 2):
        ref = a[s: s + w]
        ok = np.isfinite(ref)
        if ok.sum() < min_valid_s / step or np.std(ref[ok]) < 5.0:
            continue
        corr = np.full(len(lags), np.nan)
        for n, lag in enumerate(lags):                    # IMU 가 lag 칸 늦게 찍혔다면, GPS 의 칸 k 는 IMU 의 칸 k + lag
            lo = a0 + s + lag - b0
            if lo < 0 or lo + len(ref) > len(b):
                continue
            y = b[lo: lo + len(ref)]
            m = ok & np.isfinite(y)
            if m.sum() >= min_valid_s / step and np.std(y[m]) > 1e-9:
                corr[n] = np.corrcoef(ref[m], y[m])[0, 1]
        if not np.isfinite(corr).any():
            continue
        i = int(np.nanargmax(corr))
        if corr[i] < IMU_CAL_MIN_CORR:
            continue
        pos = float(lags[i])
        if 0 < i < len(lags) - 1 and np.isfinite(corr[i - 1]) and np.isfinite(corr[i + 1]):
            den = corr[i - 1] - 2 * corr[i] + corr[i + 1]
            if den < 0:
                pos += 0.5 * (corr[i - 1] - corr[i + 1]) / den
        found.append((pos * step, corr[i]))
    if not found:
        return None
    late, c = np.array(found).T
    return {"late_s": float(np.median(late)), "windows": int(len(late)), "corr": float(np.median(c))}


def _table(parts: list[np.ndarray], cols: list[str]):
    import pandas as pd
    if not parts:
        return pd.DataFrame(columns=cols)
    a = np.vstack(parts)
    df = pd.DataFrame(a[:, 1:], columns=cols[1:])
    df.insert(0, "t_utc", pd.to_datetime(a[:, 0], unit="s", utc=True))
    return df.sort_values("t_utc").reset_index(drop=True)


def read_recording(bin_paths: list[Path]):
    """추출한 gpmd 조각들 → (gps, imu, 파일별 IMU 시각 보정 기록). 시각은 전부 UTC.

    GPMF 는 약 1 초짜리 블록 안에 스트림별 샘플을 묶어 둔다. 블록 시각은 GPSU(UTC) 가 준다.
    **fix 가 3D 가 아닌 블록은 버린다** — 위성을 잡기 전이라 위치도 시각도 믿을 수 없다.

    샘플 시각 (GoPro 는 샘플별 시각을 주지 않는다)
        GPS   블록 i 의 j 번째 = GPSU_i + j × (다음 블록까지의 시간 / 샘플 수).
              GPSU 는 블록 **첫 샘플**의 시각이다 — 샘플 9 개짜리 첫 블록 뒤의 GPSU 차이가
              0.495 s (= 9 × 55 ms) 로, 블록 길이와 맞는다. 다음 블록이 없으면 명목 간격을 쓴다.
        IMU   끊기지 않고 이어지는 스트림으로 본다: 파일 안에서 시각 = T0 + k / fs.
              GPS 블록은 샘플이 17~19 개로 들쭉날쭉해서 블록마다 GPSU 에 붙이면 IMU 시각이 ±50 ms 흔들린다.

    ⚠ **같은 블록에 든 IMU 와 GPS 는 같은 순간의 것이 아니다.** IMU 가 0 · 1 · 2 초 늦게 들어 있고,
       그 값이 **파일마다 다르다** (HERO5 에서 잰 것: 첫 조각은 파일마다 0 · 1 · 2 s 로 달랐고, 이어 찍힌 뒤 조각은 0 s 였다).
       출발 순간을 보면 GPS 속도가 먼저 오르고 가속도계가 1 초 뒤에 반응한다 — 차가 가속 없이 빨라질 수는 없다.
       GPS 시각이 맞는 쪽이다: GPS 로 매초 시계를 맞추는 다른 장비와 일치했다.
       그래서 파일마다 `imu_late_by()` 로 늦은 정도를 재고, 가까운 정수 초만큼 IMU 시각을 당긴다.
       잴 수 없는 파일(돌면서 달린 구간이 없음)은 그대로 두고 `t_ok = False` 로 표시한다 — **최대 2 초 늦을 수 있다.**
    """
    import pandas as pd

    blocks = read_blocks(bin_paths)
    if not blocks:
        return pd.DataFrame(columns=GPS_COLS), pd.DataFrame(columns=IMU_COLS), []
    nb = len(blocks)
    t0 = np.array([np.nan if b["t0_s"] is None else b["t0_s"] for b in blocks])
    fix = np.array([b["fix"] == FIX_3D for b in blocks]) & np.isfinite(t0)
    n_gps = np.array([len(b["gps"]) for b in blocks])
    n_acc = np.array([0 if b["acc"] is None else len(b["acc"]) for b in blocks])
    files = np.array([b["file"] for b in blocks])

    span = np.full(nb, np.nan)                       # 블록 길이 = 다음 블록 시작까지
    d = np.diff(t0)
    ok = np.isfinite(d) & (d > 0) & (d < BLOCK_MAX_S)
    span[:-1][ok] = d[ok]
    with np.errstate(invalid="ignore", divide="ignore"):
        per = np.nanmedian(span / n_gps) if np.isfinite(span).any() else 1.0 / GPS_HZ_NOMINAL

    gps_parts, gps_file = [], []
    for i in np.flatnonzero(fix):
        g = blocks[i]["gps"]
        n = n_gps[i]
        step = span[i] / n if np.isfinite(span[i]) else per
        gps_parts.append(np.column_stack([t0[i] + np.arange(n) * step, np.full(n, FIX_3D), g[:, 0], g[:, 1], g[:, 2],
                                          g[:, 3] * 3.6, np.full(n, blocks[i]["dop"])]))
        gps_file.append(np.full(n, files[i]))

    # IMU: 파일 안의 이어진 구간마다 표본 번호 → 시각 직선을 GPSU 에 맞춘다
    seg = np.concatenate([[0], np.cumsum(~ok | (files[1:] != files[:-1]))])
    imu_by_file: dict[str, list[np.ndarray]] = {}
    for s in np.unique(seg):
        idx = np.flatnonzero(seg == s)
        c = np.concatenate([[0], np.cumsum(n_acc[idx])[:-1]])          # 블록 시작까지의 누적 표본 수
        use = np.flatnonzero(fix[idx] & (n_acc[idx] > 0))
        if (use > 0).sum() >= 2:
            use = use[use > 0]                # 구간 첫 블록은 GPS 가 늦게 시작해 GPSU 가 블록 중간일 수 있다
        if len(use) < 2 or c[use[-1]] == c[use[0]]:
            continue
        fs = (c[use[-1]] - c[use[0]]) / (t0[idx[use[-1]]] - t0[idx[use[0]]])
        start = float(np.median(t0[idx[use]] - c[use] / fs))
        for j, i in enumerate(idx):
            b = blocks[i]
            if not fix[i] or b["acc"] is None or b["gyr"] is None or not len(b["acc"]) or not len(b["gyr"]):
                continue
            n = len(b["acc"])
            imu_by_file.setdefault(files[i], []).append(np.column_stack(
                [start + (c[j] + np.arange(n)) / fs, b["acc"] / 9.80665, np.degrees(_resample_gyro(b["gyr"], n))]))

    # 파일마다 IMU 가 GPS 보다 늦은 정도를 재서 당긴다
    g_all = np.vstack(gps_parts) if gps_parts else np.empty((0, 7))
    g_file = np.concatenate(gps_file) if gps_file else np.array([])
    imu_parts, info = [], []
    for name in sorted(imu_by_file):
        a = np.vstack(imu_by_file[name])
        g = g_all[g_file == name]
        cal = imu_late_by(g[:, 0], g[:, 2], g[:, 3], g[:, 5], a[:, 0], a[:, 1:4], a[:, 4:7]) if len(g) else None
        shift = 0.0
        usable = cal is not None and abs(cal["late_s"] - round(cal["late_s"])) <= 0.25
        if usable:
            shift = float(round(cal["late_s"]))       # 블록 단위로 밀리는 것이라 정수 초로 당긴다
            a[:, 0] -= shift
        info.append({"file": name, "imu_late_measured_s": None if cal is None else round(cal["late_s"], 3),
                     "imu_shift_applied_s": shift, "calibrated": bool(usable),
                     "windows": 0 if cal is None else cal["windows"], "corr": None if cal is None else round(cal["corr"], 3)})
        imu_parts.append(np.column_stack([a, np.full(len(a), float(usable))]))

    gps = _table(gps_parts, GPS_COLS)
    if len(gps):
        gps["fix"] = gps["fix"].astype(int)
    imu = _table(imu_parts, IMU_COLS)
    if len(imu):
        imu["t_ok"] = imu["t_ok"].astype(bool)
    return gps, imu, info


def read_streams(bin_paths: list[Path]):
    """추출한 gpmd 조각들 → (gps, imu). `read_recording` 에서 보정 기록만 뺀 것."""
    gps, imu, _ = read_recording(bin_paths)
    return gps, imu


def vertical_axis(acc_g, t_s=None, still_t_s=None, min_still: int = 200) -> np.ndarray:
    """카메라 좌표에서 '위' 방향의 단위벡터. acc_g 는 (n, 3) 가속도 [g].

    정지해 있으면 가속도계에는 중력의 반작용(위쪽 1 g)만 잡힌다. 정지해 있던 시각들(still_t_s)이
    충분하면 그 순간들의 평균을, 아니면 전체 평균을 쓴다 (달리는 동안의 가감속·횡가속은 평균하면 거의 사라진다).
    """
    acc = np.asarray(acc_g, float)
    if t_s is not None and still_t_s is not None and len(still_t_s) >= min_still:
        pos = np.clip(np.searchsorted(np.asarray(t_s, float), np.asarray(still_t_s, float)), 0, len(acc) - 1)
        acc = acc[pos]
    mean = acc.mean(axis=0)
    return mean / np.linalg.norm(mean)


def yaw_rate(gyro_dps, up: np.ndarray) -> np.ndarray:
    """수직 축 둘레의 회전 속도 (°/s). gyro_dps 는 (n, 3). 위에서 볼 때 반시계(좌회전)가 양수.

    자이로 세 축을 '위' 방향에 투영한다. 카메라가 기울어 달려 있어도 장착각과 무관하게 요레이트가 나온다
    (가속도와 자이로의 축 순서가 같아야 한다 — GPMF 는 둘 다 z, x, y 순서다).
    """
    return np.asarray(gyro_dps, float) @ up


def course_from_track(lat, lon, t_s):
    """GoPro 는 진행 방위를 주지 않는다. 위치 변화로 계산한다 (북=0, 시계방향)."""
    lat, lon, t_s = map(np.asarray, (lat, lon, t_s))
    mlat = np.radians(np.nanmean(lat))
    x = np.radians(lon) * np.cos(mlat) * 6371000.0
    y = np.radians(lat) * 6371000.0
    dt = np.gradient(t_s)
    dt[dt == 0] = np.nan
    return np.degrees(np.arctan2(np.gradient(x) / dt, np.gradient(y) / dt)) % 360
