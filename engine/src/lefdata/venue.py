"""경기장(venue) — 같은 코스를 달린 이벤트들이 함께 쓰는 원점 · 계측선 · 기준선 (팀 계층: 데이터 폴더를 안다).

    <데이터 폴더>/_venues/<이름>.json    {name, frame_id, origin, gate, half_width_m, line{x_m, y_m}, source, made}

    lefdata venue --name <이름> --from-event <이벤트>    그 이벤트의 계측선 · 원점과 최속 랩 주행선을 경기장으로 정한다
                                                         (그 이벤트도 경기장으로 다시 나눈다 — 같은 선 · 같은 원점이라 랩은 그대로)
    lefdata laps --event E --venue <이름>                  다른 이벤트를 그 경기장 계측선으로 나눈다 (gopro --laps 도 같다)

이벤트의 `catalog_notes.toml` 에 `[event] venue = "<이름>"` 을 적어 두면 --venue 를 빼도 그 경기장을 쓴다 (다시 돌려도 선이 안 바뀐다).
같은 경기장을 쓴 이벤트의 세션은 frame_id 가 `venue/<이름>` 으로 같아져, 사이트에서 다른 날 · 다른 차의 랩을 겹칠 수 있다.
코스 배치가 바뀌었으면 `laps.split_event` 가 멈춘다 — 그때는 이름을 달리해 경기장을 새로 만든다.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import tomllib
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from . import laps as lapmod, paths, sectors

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
FRAME_PREFIX = "venue/"
NOTES_NAME = "catalog_notes.toml"


def root() -> Path:
    return paths.DATA_ROOT / "_venues"


def path(name: str, base: Path | None = None) -> Path:
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError(f"경기장 이름은 영문 소문자 · 숫자 · - · _ 만: {name!r}")
    return Path(base or root()) / f"{name}.json"


def load(name: str, base: Path | None = None) -> dict:
    p = path(name, base)
    if not p.exists():
        raise FileNotFoundError(f"경기장 {name} 가 없다 ({p}) — lefdata venue --name {name} --from-event <이벤트> 로 먼저 만든다")
    return json.loads(p.read_text(encoding="utf-8"))


def for_event(event_dir: Path, name: str | None = None, base: Path | None = None) -> dict | None:
    """이벤트가 쓸 경기장 — name 을 주면 그것, 아니면 catalog_notes.toml 의 [event] venue. 둘 다 없으면 None (이벤트마다 계측선)."""
    if name is None:
        notes = Path(event_dir) / NOTES_NAME
        name = tomllib.loads(notes.read_text(encoding="utf-8")).get("event", {}).get("venue") if notes.exists() else None
    return load(name, base) if name else None


def create_from_event(name: str, event_dir: Path, base: Path | None = None) -> dict:
    """이벤트의 랩 분할 결과(gate.json · laps.csv)로 경기장을 정해 쓴다. 기준선 = 그 이벤트의 최속 랩 주행선."""
    event_dir = Path(event_dir)
    derived = event_dir / "derived"
    gate_file = json.loads((derived / "gate.json").read_text(encoding="utf-8"))
    laps = pd.read_csv(derived / "laps.csv", encoding="utf-8-sig")
    if laps.empty:
        raise ValueError(f"{event_dir} 에 완주 랩이 없어 경기장 기준선을 정할 수 없다")
    best = laps.loc[laps["lap_time_s"].idxmin()]
    g = pq.read_table(event_dir / "normalized" / best["recording_id"] / "gps.parquet").to_pandas()
    g = g[g["fix"]]
    origin = gate_file["origin"]
    x, y = lapmod.to_local_m(g["lat"].to_numpy(), g["lon"].to_numpy(), origin["lat0"], origin["lon0"])
    _, lx, ly = sectors.lap_points(g["t_rel_ms"].to_numpy() / 1000.0, x, y, float(best["t_start_s"]), float(best["t_end_s"]))
    doc = {"name": name, "frame_id": FRAME_PREFIX + name, "origin": origin, "gate": gate_file["gate"],
           "half_width_m": gate_file.get("half_width_m", lapmod.GATE_HALF_WIDTH_M),
           "line": {"x_m": [round(float(v), 2) for v in lx], "y_m": [round(float(v), 2) for v in ly]},
           "source": {"event": f"{event_dir.parent.name}/{event_dir.name}", "recording": best["recording_id"], "run": int(best["run"]),
                      "lap": int(best["lap"]), "lap_time_s": round(float(best["lap_time_s"]), 3),
                      "line_m": round(float(sectors.path_length(lx, ly)[-1]), 1)},
           "made": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "note": "x=동, y=북 (m). heading 은 북=0 시계방향. line = 같은 코스인지 볼 때 쓰는 기준선 (그 이벤트의 최속 랩)"}
    p = path(name, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    return doc


def course_share(event_dir: Path, venue: dict) -> dict:
    """이 이벤트의 랩이 경기장 코스와 같은가 (파일을 쓰지 않고 본다) — split_event 의 course_check · 랩 수."""
    runs, laps, _, doc = lapmod.split_event(Path(event_dir), venue)
    return {**doc.get("course_check", {}), "runs": int(len(runs))}


def summary(doc: dict) -> str:
    src = doc["source"]
    return (f"경기장 {doc['name']} (frame {doc['frame_id']}) — 계측선 x={doc['gate']['x']:.1f} m · y={doc['gate']['y']:.1f} m · "
            f"방향 {doc['gate']['heading_deg']:.0f}°, 기준선 {src['event']} {src['recording']} {src['lap']}랩 ({src['line_m']} m, {src['lap_time_s']} s)")

