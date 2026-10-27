"""기대값 파일 — 우리 차 데이터에서 나온 값은 테스트 코드가 아니라 데이터 폴더의 파일에 둔다.

테스트는 두 가지다 (공개 전에 나눔, 2026-10-27).

1. **보편 테스트** — 어떤 데이터에도 맞는 것. 만들어 낸 데이터의 단위 테스트와, 우리 데이터에 돌리더라도 형식 · 물리상
   늘 참이어야 하는 불변식(시간은 늘기만 한다, 체크섬, 방위는 0 ~ 360, 에너지 = 전력의 시간 적분, 빈 값을 0 으로 채우지 않는다 …).
   코드에 남는 숫자는 단위 · 물리 상수 · 형식의 정의 · 방법의 허용 오차뿐이다.
2. **골든 테스트** — 다른 팀이 자기 차 데이터로 같은 테스트를 돌리면 바뀌는 값: 개수 · 시각 · 길이 · 측정값 · 레코딩 이름 · 랩 번호,
   그리고 우리 차 · 코스 · 센서에 묶인 범위(코스 한 바퀴 길이의 범위, 배터리 직렬 셀 수 같은 것). 테스트 코드는 "기대값 파일의 값과 맞는가" 만 본다.
   **값과 그 값의 이유(우리 차 사정)는 기대값 파일에 있다.**

기대값 파일: `<데이터 폴더>/_expected/<이름>.toml` (환경 변수 `LEF_EXPECTED_ROOT` 로 바꾼다). 없으면 그 골든 테스트는 건너뛴다.
공개 저장소에는 합성 데이터용 예시(`tests/expected_example/`)만 있다.

    E = Expected("laps_golden")                 tests/test_laps_golden.py 의 값
    @E.need                                   파일이 없으면 건너뛴다 (데이터 폴더가 있어도)
    assert n == E["race.laps"]                값 그대로
    assert t == E.approx("race.duration_s")   허용 오차가 적힌 값 → pytest.approx
    assert s.between(*E.range("lap_m")).all() 범위

값을 적는 법 (TOML)

    laps = 12                                 정확히 같아야 하는 값 (정수 · 글자 · 목록 · 참/거짓)
    duration_s = { value = 120.0, abs = 0.1 } 허용 오차 (abs · rel)
    lap_m = { min = 800, max = 900 }          범위
    (위에 맨 위 값들, 그 아래 [표] — TOML 은 [표] 뒤에 적은 값을 그 표의 것으로 읽는다)
    [[cases]]                                 매개변수 목록 (E.cases("cases"))

스스로 확인 — `LEF_EXPECTED_PERTURB=up` (또는 `down`) 으로 돌리면 읽은 기대값을 전부 일부러 틀리게 바꾼다 (숫자는 키우거나 줄이고,
글자 · ID 는 없는 것으로, 범위는 아무것도 들지 않게). 골든 테스트가 정말 값을 보고 있다면 up · down 중 적어도 한 번은 실패해야 한다
(값을 파일로 옮기다 검사가 빈 껍데기가 된 것을 잡는다). 상한 · 하한 검사는 한 방향으로만 실패하므로 두 번 돌린다.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from lefdata import paths

ROOT = Path(os.environ.get("LEF_EXPECTED_ROOT", paths.DATA_ROOT / "_expected"))
PERTURB = os.environ.get("LEF_EXPECTED_PERTURB", "")         # "", "up", "down"
EXAMPLE_ROOT = Path(__file__).parent / "expected_example"
_OPENED: list["Expected"] = []


class Expected:
    """기대값 파일 하나. 읽은 열쇠를 기억해 두었다가, 한 번도 안 쓴 값을 끝에 알려 준다 (검사가 빠졌다는 신호)."""

    def __init__(self, name: str, root: Path | str | None = None):
        self.name = name
        self.path = Path(root or ROOT) / f"{name}.toml"
        self.data = tomllib.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None
        if PERTURB and self.data is not None and self.path.parent.resolve() != EXAMPLE_ROOT.resolve():
            self.data = _perturb(self.data)
        self.used: set[str] = set()
        _OPENED.append(self)

    @property
    def exists(self) -> bool:
        return self.data is not None

    @property
    def need(self):
        """골든 테스트에 붙이는 표시 — 기대값 파일이 없으면 건너뛴다."""
        return pytest.mark.skipif(not self.exists, reason=f"기대값 파일 없음: {self.path}")

    def _spec(self, key: str):
        if self.data is None:
            pytest.skip(f"기대값 파일 없음: {self.path}")
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"{self.path.name} 에 '{key}' 가 없다 — 기대값 파일을 채울 것")
            node = node[part]
        self.used.add(key)
        return node

    @staticmethod
    def _is_tol(spec) -> bool:
        return isinstance(spec, dict) and "value" in spec and set(spec) <= {"value", "abs", "rel", "why"}

    def __getitem__(self, key: str):
        spec = self._spec(key)
        return spec["value"] if self._is_tol(spec) else spec

    def get(self, key: str, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def approx(self, key: str):
        """허용 오차가 적힌 값은 pytest.approx 로. 오차 없이 적힌 숫자는 아주 작은 상대 오차(1e-9)로."""
        spec = self._spec(key)
        if self._is_tol(spec):
            return pytest.approx(spec["value"], abs=spec.get("abs"), rel=spec.get("rel"))
        return pytest.approx(spec, rel=1e-9)

    def range(self, key: str) -> tuple[float, float]:
        spec = self._spec(key)
        if not (isinstance(spec, dict) and {"min", "max"} <= set(spec)):
            raise TypeError(f"{self.path.name} 의 '{key}' 는 {{ min = …, max = … }} 이어야 한다")
        return spec["min"], spec["max"]

    def cases(self, key: str) -> list[dict]:
        """매개변수 목록. 파일이 없으면 빈 목록 — pytest 가 그 테스트를 건너뛴다."""
        if self.data is None:
            return []
        return list(self._spec(key))

    def unused(self) -> list[str]:
        """파일에 있는데 한 번도 읽지 않은 값 (맨 아래 잎 기준)."""
        if self.data is None:
            return []
        leaves: list[str] = []

        def walk(node, prefix):
            if isinstance(node, dict) and not self._is_tol(node) and not ({"min", "max"} <= set(node)):
                for k, v in node.items():
                    walk(v, f"{prefix}.{k}" if prefix else k)
            else:
                leaves.append(prefix)
        walk(self.data, "")
        return [k for k in leaves if not any(k == u or k.startswith(u + ".") for u in self.used)]


def _perturb(node):
    """스스로 확인용: 값을 전부 틀리게 (허용 오차 abs · rel 은 그대로, 범위는 아무것도 들지 않게)."""
    if isinstance(node, bool):
        return not node
    up = PERTURB != "down"
    if isinstance(node, int):
        return node + 7 if up else node - 7
    if isinstance(node, float):
        return node * 1.37 + 0.731 if up else node * 0.63 - 0.731
    if isinstance(node, str):
        return node + "~"
    if isinstance(node, list):
        return [_perturb(x) for x in node] if node else ["~"]
    if isinstance(node, dict):
        if Expected._is_tol(node):
            return {**node, "value": _perturb(node["value"])}
        if {"min", "max"} <= set(node):
            return {"min": 1e12, "max": -1e12}
        return {k: _perturb(v) for k, v in node.items()}
    return node


def opened() -> list[Expected]:
    return list(_OPENED)
