# 사이트 서버 (`lefsite`)

주행 데이터 뷰어의 FastAPI 서버입니다. 서버는 계산을 하지 않습니다. 로그인과 권한 확인을 통과한 요청만 분석 엔진(`lefdata.api`)에 넘기고, 엔진이 준 JSON 을 그대로 돌려줍니다.
화면 코드는 [`../web/`](../web/) (React + TypeScript)에 있습니다. 빌드한 화면(`web/dist`)이 있으면 서버가 같은 주소에서 함께 내줍니다.

| 파일 | 하는 일 |
|---|---|
| `src/lefsite/config.py` | 설정. 모든 값을 환경 변수로 받습니다 (`server/.env`, 예시는 [`.env.example`](.env.example)). 위험한 조합이면 서버를 띄우기 전에 멈춥니다 |
| `src/lefsite/engines/` | 엔진 연결부. 엔진은 여기서만 부릅니다. `real` 은 `lefdata.api`, `fake` 는 계약 모양을 따르는 합성 세션입니다 (`DEMO-*` 차, 실제 주행 아님) |
| `src/lefsite/auth.py` | 로그인과 권한 (Google 로그인 + 명단, 개발용 자동 로그인) |
| `src/lefsite/db.py` | 앱 DB (SQLite). 명단, 역할, 로그인 기록을 둡니다. 주행 데이터는 넣지 않습니다 |
| `src/lefsite/app.py` | API (`/api/…`), 화면 파일, 보안 헤더 |
| `src/lefsite/__main__.py` | 명령줄. 서버 띄우기와 명단 관리 |

## 띄우기

```bash
cd server
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"          # macOS · Linux 는 .venv/bin/pip
.venv/Scripts/pip install -e ../engine         # 진짜 엔진을 쓸 때 (LEF_SITE_ENGINE=real)
cd ../web && npm install && npm run build && cd ../server
.venv/Scripts/python -m lefsite                # → http://127.0.0.1:8000
```

- 기본 설정은 가짜 엔진, 개발용 자동 로그인, 127.0.0.1 입니다. 데이터나 계정이 없어도 바로 화면이 뜹니다
- 화면을 고치는 중이라면 `cd web && npm run dev` 로 띄웁니다 (http://127.0.0.1:5173). `/api` 와 `/auth` 요청은 8000 번 서버로 넘어갑니다
- 진짜 데이터를 보려면 `LEF_SITE_ENGINE=real` 로 띄웁니다. 세션 저장소 위치는 `LEF_SESSION_ROOT` · `LEF_DATA_ROOT` 이고, 비워 두면 엔진 기본값을 씁니다

## API

엔진 계약의 함수를 그대로 옮겨 놓았습니다. 오류도 계약의 모양 `{"error": {"code", "message", "detail"}}` 을 그대로 쓰고 HTTP 상태만 붙입니다
(`unknown_session` 404 · `session_retired` 410 · `bad_argument` 400 · `not_implemented` 501 · 로그인 필요 401 · 권한 없음 403).

| 주소 | 엔진 함수 | 권한 |
|---|---|---|
| `GET /api/me` | 없음 (로그인한 사람, 역할, 권한) | 로그인 |
| `GET /api/info` | `engine_info` + 연결부 종류 | 보기 |
| `GET /api/sessions?car=&event_id=&discipline=&limit=&offset=` | `list_sessions` | 보기 |
| `GET /api/sessions/{sid}` | `get_session` | 보기 |
| `GET /api/sessions/{sid}/channels` | `list_channels` | 보기 |
| `GET /api/sessions/{sid}/laps` | `get_laps` | 보기 |
| `GET /api/sessions/{sid}/quality` | `get_quality` | 보기 |
| `GET /api/sessions/{sid}/resolve` | `resolve_session` | 보기 |
| `GET · HEAD /api/files/{file_id}` | `open_file` (파일을 첨부로 보냅니다) | 내려받기 |
| `GET /api/admin/users` | 없음 (명단) | 명단 관리 |

내려받기는 팀장과 관리자만 할 수 있고, GET 으로 받을 때마다 누가 언제 무엇을 받았는지 앱 DB 에 남습니다. HEAD 는 받을 수 있는지만 확인하고 기록하지 않습니다. 화면의 내려받기 버튼은 먼저 HEAD 로 물어본 뒤 받습니다.

세션은 세션 ID(`sid`)로만 가리킵니다. 엔진은 한 번 준 `sid` 를 바꾸지 않습니다. 없어진 `sid` 로 부르면 410 과 함께 이어지는 세션(`detail.current`)이 오고, 화면이 그 세션으로 넘겨 줍니다.

## 로그인과 권한

| 역할 | 보기 | 내려받기 · 올리기 | 명단 관리 |
|---|---|---|---|
| `admin` 관리자 | ✅ | ✅ | ✅ |
| `lead` 팀장 | ✅ | ✅ | ❌ |
| `member` 팀원 | ✅ | ❌ | ❌ |

- `LEF_SITE_AUTH=dev` 는 개발용 자동 로그인입니다. 127.0.0.1 에서만 켜지고, 다른 주소로 띄우려 하면 서버가 시작되지 않습니다. 로그인 쿠키에 개발용 메일을 넣어 줄 뿐이고, 명단과 역할 확인은 Google 로그인과 똑같이 거칩니다. 팀원 화면을 보려면 `LEF_SITE_DEV_ROLE=member` 로 띄웁니다
- `LEF_SITE_AUTH=google` 은 Google 로그인입니다. 서버가 ID 토큰의 서명, `email_verified`, 도메인(`hd` 값과 메일 끝 둘 다, `LEF_SITE_ALLOWED_DOMAIN`)을 확인하고 명단에 켜져 있는 계정인지도 봅니다. 비밀번호는 만들지도 보관하지도 않습니다
- 로그인 쿠키는 HttpOnly · SameSite=Lax 이고, https 주소에서는 Secure 가 붙습니다. 요청마다 명단을 다시 확인하므로 명단에서 끈 계정은 바로 막힙니다
- 명단 관리 화면은 아직 없어서 명령으로 관리합니다.

```bash
.venv/Scripts/python -m lefsite users add someone@example.ac.kr --role member --name 이름
.venv/Scripts/python -m lefsite users                 # 명단
.venv/Scripts/python -m lefsite users disable someone@example.ac.kr
.venv/Scripts/python -m lefsite logins                # 최근 로그인 시도와 거절 이유
.venv/Scripts/python -m lefsite downloads             # 최근 내려받기 기록
```

### Google 로그인 설정

1. Google Cloud 콘솔에서 프로젝트를 만들고, Google Auth Platform 에 앱 이름과 지원 메일을 넣습니다. 대상은 "외부"로 하고, 시험하는 동안은 "테스트" 상태로 둔 채 테스트 사용자에 로그인할 메일을 넣습니다. 테스트 상태에서는 이 목록에 있는 계정만 로그인할 수 있습니다
2. 클라이언트를 만들 때 유형은 "웹 애플리케이션"으로 고르고, 승인된 리디렉션 URI 에 `http://127.0.0.1:8000/auth/google/callback` 을 넣습니다. 외부에 공개할 때는 `https://<주소>/auth/google/callback` 입니다. 범위는 기본값(openid · email · profile)만 씁니다
3. `.env.example` 을 복사해 `server/.env` 를 만들고 `LEF_SITE_AUTH=google`, `LEF_SITE_GOOGLE_CLIENT_ID` · `LEF_SITE_GOOGLE_CLIENT_SECRET`, `LEF_SITE_ALLOWED_DOMAIN` 을 채웁니다. 클라이언트 비밀은 `.env` 에만 두고 채팅, 문서, 커밋에는 붙이지 마세요
4. `python -m lefsite users add <메일> --role admin` 으로 자신을 명단에 넣은 뒤 서버를 띄우고 http://127.0.0.1:8000 에서 로그인합니다
5. 로그인이 거절되면 화면과 `python -m lefsite logins` 에 이유가 나옵니다. `not_workspace` 는 토큰에 `hd` 가 없다는 뜻으로, Google Workspace 계정이 아닐 때 나옵니다. 그 밖에 `wrong_domain` · `email_unverified` · `not_listed` · `disabled` · `oauth_failed` 가 있습니다.
   관리자가 외부 앱을 막아 둔 경우처럼 Google 화면에서 막히면 요청이 서버까지 오지 않습니다. 이때는 그 화면의 오류 문구를 그대로 적어 두세요

## 테스트

```bash
.venv/Scripts/python -m pytest -q
```

- 테스트는 가짜 연결부와 임시 DB 로만 돌고 `server/.local` 은 건드리지 않습니다. Google 과도 통신하지 않습니다 (메타데이터와 토큰 교환을 가짜로 바꿔 끼웁니다)
- `tests/test_fake_parity.py` 는 엔진과 세션 저장소가 있는 PC 에서만 돌며, 가짜와 진짜 응답의 키를 비교합니다 (값은 보지 않습니다). 엔진이 계약에 필드를 더하면 이 테스트가 실패하니, 그때 가짜에도 같은 필드를 더합니다
- 화면 테스트는 `cd web && npm test` (이름, 블록 칸, 거르기 규칙)와 `npm run typecheck` 입니다

## 저장소에 넣지 않는 것

`server/.env` (비밀값), `server/.local/` (앱 DB, 쿠키 서명 키), `web/dist`, `node_modules`, 데이터 파일은 루트 `.gitignore` 로 뺐습니다.
테스트와 화면 캡처는 가짜 연결부로만 만들고, 실제 주행 수치는 넣지 않습니다.
