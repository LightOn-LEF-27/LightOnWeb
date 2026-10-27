"""계층 경계 검사 — 나중에 떼어낼 수 있는 상태를 유지한다.

이 저장소에는 성격이 다른 두 덩어리가 섞여 있다.

    공개 계층   랩 분할·종목 분석·GPMF 읽기·품질 검사.
                FSAE 규격(75 m 가속, 반지름 9.125 m 스키드패드)은 전 세계 공통이라
                다른 팀도 그대로 쓸 수 있다. 언젠가 떼어내 공개할 후보다.

    팀 계층     우리 로거 어댑터(Monolith 바이너리, LEF-26 CSV), 우리 폴더 구조,
                우리 차량 상수(N-100 % = 6000 rpm, 감속비 4.2727 …).

공개 계층이 팀 계층을 한 줄이라도 import 하면 **혼자서는 못 도는 코드**가 된다.
그러면 떼어낼 때 팀 코드가 통째로 딸려오고, 딸려온 코드는 또 우리 폴더 구조를 알아서
결국 아무것도 분리되지 않는다.

지금은 우연히 잘 지켜져 있다. 앞으로 LEF-27 코드를 쓰다 보면
"여기서 잠깐 lef27 값 하나만 가져오면 되는데" 하는 순간이 반드시 온다.
그때 사람이 기억하지 않아도 되게, 이 테스트가 대신 막는다.

실제로 이 경계 덕분에 된 일: **로거 GPS 가 없던 차의 랩을 온보드 영상(GoPro)의 GPS 로 셌다.**
`laps.py` 는 GoPro 가 뭔지 모르지만, 같은 모양의 표를 받았기 때문에 그대로 돌았다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "lefdata"

# 떼어내서 공개할 수 있어야 하는 모듈. 서로는 참조해도 되지만 팀 계층은 몰라야 한다.
PUBLIC = ("laps", "events", "gopro", "quality", "sync", "series", "sectors", "drive")

# 우리 로거·폴더 구조·차량에 묶인 모듈.
TEAM = ("lef26", "monolith", "motor_runs", "video", "paths", "cli", "writer", "energymeter", "evo_stm32", "orion", "clocks",
        "lef26_clock", "lef26_laps", "lef26_report", "report", "report_charts", "em_laps", "catalog", "catalog_charts",
        "catalog_build", "session_store", "api", "venue")


def imported_modules(path: Path) -> set[str]:
    """파일이 실제로 import 하는 저장소 내부 모듈 이름. 주석·문서는 세지 않는다."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:                      # from . import x  /  from .writer import y
                if node.module:
                    found.add(node.module.split(".")[0])
                else:
                    found.update(a.name for a in node.names)
            elif node.module and node.module.startswith("lefdata"):
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("lefdata."):
                    found.add(a.name.split(".")[1])
    return found


def code_strings(path: Path) -> list[str]:
    """코드 안의 문자열 리터럴. **주석과 docstring 은 제외한다** —
    설명에 적은 경로(검증 근거 등)까지 걸리면 오탐이 난다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docs.add(d)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value not in docs]


@pytest.mark.parametrize("mod", PUBLIC)
def test_L1_public_layer_does_not_import_team_code(mod):
    """laps·events·gopro 가 우리 로거·폴더 구조에 묶이면 떼어낼 수 없게 된다."""
    path = SRC / f"{mod}.py"
    assert path.exists(), f"{mod}.py 가 없다 — PUBLIC 목록을 갱신할 것"
    leaked = imported_modules(path) & set(TEAM)
    assert not leaked, (
        f"{mod}.py 가 팀 계층을 import 한다: {sorted(leaked)}\n"
        f"  → 필요한 값은 인자로 받도록 바꿀 것. 파일을 직접 열지 말고 표(배열·DataFrame)를 받는다.\n"
        f"  → 이 규칙의 이유는 이 파일 맨 위 설명을 볼 것."
    )


@pytest.mark.parametrize("mod", PUBLIC)
def test_L2_public_layer_has_no_hardcoded_data_paths(mod):
    """`07_데이터` 같은 우리 폴더 이름이 박히면 남의 데이터에 못 쓴다.

    `normalized` / `derived` 는 예외다 — 지금은 편의 함수(run_event 등)가 그 구조를 안다.
    공개할 때 순수 함수와 편의 함수를 나누면서 같이 정리한다.
    """
    for text in code_strings(SRC / f"{mod}.py"):
        for bad in ("07_데이터", "LEF-25evo", "LEF-26", "yeonggwang"):
            assert bad not in text, f"{mod}.py 코드에 우리 데이터 경로가 박혀 있다: {bad!r}"


def test_L3_team_layer_may_use_public_layer():
    """방향이 한쪽이어야 한다는 확인 — 팀 → 공개는 정상, 그 반대가 문제다."""
    assert "gopro" in imported_modules(SRC / "video.py")


def test_L4_every_module_is_classified():
    """새 모듈을 추가하고 분류를 빠뜨리면 경계 밖에서 자란다."""
    mods = {p.stem for p in SRC.glob("*.py")} - {"__init__", "plots"}
    #   plots 는 중간 계층이라 제외한다. 범용 그림(track_map)과 우리 리포트용 그림이 섞여 있다.
    unclassified = mods - set(PUBLIC) - set(TEAM)
    assert not unclassified, (
        f"분류되지 않은 모듈: {sorted(unclassified)}\n"
        f"  → 이 파일의 PUBLIC 또는 TEAM 에 넣을 것. 어느 쪽인지 애매하면 그게 설계 신호다."
    )
