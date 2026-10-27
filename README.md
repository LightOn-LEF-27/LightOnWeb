# LightOnWeb

대학 자작 포뮬러(전기차) 팀 **라이트온(LightOn)** DAQ 파트의 데이터 도구 모음.
차량 로거 · 모터 컨트롤러 · BMS · 에너지미터 · 온보드 영상(GoPro)의 기록을 한 형식으로 맞추고, 랩 · 종목 · 품질을 계산하고,
그 결과를 팀원이 브라우저로 보게 하는 것이 목표다 (MoTeC i2 같은 데이터 뷰어).

| 폴더 | 무엇 | 상태 |
|---|---|---|
| [`engine/`](engine/) | **분석 엔진** `lefdata` (Python) — 장비별 변환기, 시계 맞추기, 랩 · 종목 분석, 품질 검사, 사이트가 부르는 `lefdata.api` | 동작 · 테스트 있음 |
| 사이트 | 데이터 뷰어 (서버 FastAPI + 화면 React) — 엔진을 import 해서 보여 주기만 한다 | 만드는 중 |

계산은 전부 엔진이 하고, 사이트는 받은 값을 그린다. 둘 사이의 약속(함수 이름 · 인자 · 돌려주는 모양)은 `engine/src/lefdata/api.py` 맨 위에 있다.

## 데이터는 들어 있지 않다

이 저장소에는 **코드와 합성 데이터만** 있다. 실제 주행 기록 · 영상 · 우리 차에서 나온 기대값은 팀 데이터 폴더에 따로 두고
환경 변수(`LEF_DATA_ROOT` · `LEF_EXPECTED_ROOT` · `LEF_SESSION_ROOT`)로 가리킨다. 데이터가 없으면 데이터에 기대는 테스트는 건너뛰고,
합성 데이터 · 불변식 테스트만 돈다 (`engine/README.md` §테스트).

## 시작

```bash
cd engine
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # macOS · Linux 는 .venv/bin/pip
.venv/Scripts/python -m pytest -q
```

## 라이선스

코드는 [MIT](LICENSE). `engine/assets/fonts/` 의 Pretendard 글꼴은 SIL Open Font License 1.1 이다 ([`OFL.txt`](engine/assets/fonts/OFL.txt)).
