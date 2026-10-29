# LightOnWeb

대학 자작 포뮬러(전기차) 팀 라이트온(LightOn)의 DAQ 파트가 쓰는 데이터 도구입니다.
차량 로거, 모터 컨트롤러, BMS, 에너지미터, 온보드 영상(GoPro)의 기록을 한 형식으로 맞추고 랩 · 종목 · 품질을 계산합니다.
그 결과를 팀원이 브라우저에서 볼 수 있게 하는 것이 목표입니다. MoTeC i2 같은 데이터 뷰어를 생각하면 됩니다.

| 폴더 | 내용 | 상태 |
|---|---|---|
| [`engine/`](engine/) | 분석 엔진 `lefdata` (Python). 장비별 변환기, 시계 맞추기, 랩 · 종목 분석, 품질 검사, 사이트가 부르는 `lefdata.api` | 동작 · 테스트 있음 |
| [`server/`](server/) | 사이트 서버 `lefsite` (FastAPI). 로그인과 권한, 엔진 연결부(진짜 `lefdata.api` · 가짜 합성 세션), 앱 DB | 뼈대 · 테스트 있음 |
| [`web/`](web/) | 사이트 화면 (React + TypeScript + Vite). 세션 목록과 세션 페이지가 있고, 엔진이 준 값을 그리기만 합니다 | 만드는 중 |

계산은 모두 엔진이 맡고 사이트는 받은 값을 그립니다. 둘 사이의 약속(함수 이름, 인자, 돌려주는 모양)은 `engine/src/lefdata/api.py` 맨 위에 적혀 있습니다.

## 데이터는 들어 있지 않습니다

이 저장소에는 코드와 합성 데이터만 있습니다. 실제 주행 기록과 영상, 우리 차에서 나온 기대값은 팀 데이터 폴더에 따로 두고
환경 변수(`LEF_DATA_ROOT` · `LEF_EXPECTED_ROOT` · `LEF_SESSION_ROOT`)로 위치를 알려 줍니다. 데이터가 없는 PC에서는 데이터가 필요한 테스트를 건너뛰고
합성 데이터 테스트와 불변식 테스트만 돌립니다 (`engine/README.md` §테스트).

## 시작하기

```bash
cd engine
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # macOS · Linux 는 .venv/bin/pip
.venv/Scripts/python -m pytest -q
```

사이트는 데이터 없이도 뜹니다. 기본 설정이 가짜 엔진(합성 세션 `DEMO-*`), 개발용 자동 로그인, 127.0.0.1 이기 때문입니다 ([`server/README.md`](server/README.md)).

```bash
cd web && npm install && npm run build && cd ..
cd server
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m lefsite           # → http://127.0.0.1:8000
```

## 라이선스

코드는 [MIT](LICENSE) 라이선스입니다. `engine/assets/fonts/` 의 Pretendard 글꼴과 화면에서 쓰는 npm 패키지 `pretendard` 는 SIL Open Font License 1.1 을 따릅니다 ([`OFL.txt`](engine/assets/fonts/OFL.txt)).
