"""데이터 폴더 경로.

기본값은 이 패키지 폴더(engine)에서 두 단계 위의 07_데이터 (팀 작업 폴더에 둔 배치).
다른 위치를 쓰려면 환경변수 LEF_DATA_ROOT 로 지정한다.
"""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT = _REPO_ROOT.parents[1] / "07_데이터"

DATA_ROOT = Path(os.environ.get("LEF_DATA_ROOT", _DEFAULT))

LEF25EVO_EVENT = DATA_ROOT / "LEF-25evo" / "2026-08-13_yeonggwang"
LEF25EVO_FSK = DATA_ROOT / "LEF-25evo" / "2026-08-28_yeonggwang"      # FSK 2026 대회
LEF25EVO_FSK_DAYS = ("2026-08-28", "2026-08-30")
LEF25EVO_ORION_CSV = DATA_ROOT / "LEF-25evo" / "raw_orion_connect" / "csv_orion_utility"
LEF26_EVENT = DATA_ROOT / "LEF-26" / "2026-08-13_yeonggwang"         # 테스트위크 (로거 + 온보드 영상)
LEF26_FSK = DATA_ROOT / "LEF-26" / "2026-08-30_yeonggwang"           # FSK 2026 대회 (온보드 영상 + 내구 에너지미터)
