"""정규화 결과 쓰기 — Parquet 테이블, meta.json, 레코딩 인덱스."""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

TZ = "Asia/Seoul"
T_ABS_TYPE = pa.timestamp("ms", tz=TZ)


def t_abs_array(epoch_ms: np.ndarray, mask: np.ndarray | None = None) -> pa.Array:
    """UTC epoch ms → timestamp[ms, Asia/Seoul]. mask=True 인 자리는 null."""
    return pa.array(epoch_ms.astype(np.int64), type=pa.int64(), mask=mask).cast(T_ABS_TYPE)


def write_parquet(table: pa.Table, path: Path) -> int:
    pq.write_table(table, path, compression="zstd")
    return path.stat().st_size


def write_json(obj: dict, path: Path) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def fresh_dir(path: Path) -> Path:
    """변환 결과 폴더를 비우고 새로 만든다. normalized/ 밖은 절대 지우지 않는다."""
    if "normalized" not in path.parts:
        raise ValueError(f"normalized/ 밖의 경로는 지울 수 없음: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def write_index(rows: list[dict], path: Path) -> None:
    if not rows:
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
