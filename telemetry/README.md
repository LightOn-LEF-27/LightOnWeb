# telemetry — 실시간 시험 도구

Monolith 로거가 휴대전화망으로 서버에 보낸 기록을 재서, 실시간 전송이 쓸 만한지 판정합니다.

```
python t8_judge.py fetch --date 2026-11-14 --out <폴더>
python t8_judge.py judge --dir <폴더> --sd <normalized> --runs <runs.csv> --venue <venue.json>
```

- `fetch` 는 서버 DB 를 읽기 전용 트랜잭션으로 읽기만 합니다. 접속 정보는 환경 변수 `T8_SSH_TARGET` · `T8_SSH_KEY` 에 둡니다
- `judge` 는 결과를 화면에 찍고 `<폴더>/t8_result.json` 에 남깁니다. `--sd` · `--runs` · `--venue` 는 없어도 돌지만, 없으면 전달률 · 주행 구간 · 랩당 끊김을 내지 못합니다
- 실행은 엔진 가상환경으로 합니다 (`engine/.venv`, 랩 나누기를 `lefdata.laps` 에서 씁니다)

## 재는 것

| 항목 | 뜻 | 기준 |
|---|---|---|
| 값의 나이 | CAN 프레임이 찍힌 순간 → 서버가 받은 순간 (묶음 대기 포함) | 중앙 ≤ 1 s, 95 % ≤ 3 s |
| 전달 시간 | 장치가 보낸 순간 → 서버가 받은 순간 | 참고 |
| 끊김 | 장치의 주기 토픽(`/d` · `/d/can`)이 10 초 넘게 안 온 구간 | 랩당 1번 이하 |
| 끊긴 자리 | 끊김이 시작된 위치, 50 m 안은 한 자리로 | 기록만 |
| 전달률 | SD 프레임에 펌웨어의 ID별 거르기를 돌린 수 대비 서버가 받은 수 | 기록만 |

## 시계

장치 시각은 부팅 뒤 ms 이고, 그 기준인 `boot_epoch` 는 초 단위 · 동기화 전 값이라 쓰지 않습니다. 세션마다 `/d` 메시지의 "받은 시각 − 보낸 시각" 아래쪽 경계(바닥)를 빼서 시계 차이와 속도 차이를 없앱니다.

- 바닥 자체(가장 빠른 전달 시간)는 잴 수 없어 `--floor` 값(기본 0.10 s)을 더합니다. 가정이므로 결과에 함께 찍습니다
- 시계 속도 차이는 100 ppm 까지만 받습니다. 그 이상은 혼잡으로 봅니다
- SD 와는 같은 부팅의 장치 시각이라 시계를 맞출 필요가 없습니다

## 실시간 수신기 (`receiver.py`)

브로커를 읽기 전용 계정으로 구독해 받은 메시지를 그대로 파일에 남깁니다. 해독은 하지 않고, 장치로는 아무것도 보내지 않습니다.

```
pip install "paho-mqtt>=2,<3"
LEF_RX_HOST=<브로커 주소> LEF_RX_USER=<읽기 전용 계정> LEF_RX_PASSWORD=<비밀번호> LEF_RX_CHANNEL=<장치 이름> LEF_RX_DIR=<저장 폴더> python receiver.py
```

- 하루에 `<날짜>.jsonl` 하나, 한 줄에 메시지 하나 (`t_ns` 받은 시각 · `topic` · `retain` · `b64` 원본). 수신기 쪽 접속 · 끊김은 `event` 줄로 남습니다
- 끊기면 알아서 다시 붙습니다. 값은 환경 변수로만 받습니다
- 장치 설정 토픽(`d/cfg`)은 구독하지 않습니다. 비밀번호가 평문으로 실리기 때문입니다
- 서버에 올릴 때는 `deploy/` 를 씁니다. 수신기는 사이트와 **다른 리눅스 계정**으로 돌고, 값은 사이트의 `.env` 가 아니라 관리자만 읽는 `/etc/lighton/receiver.env` 에 둡니다. 사이트 쪽 버그로 파일이 새어도 이 값은 따라 나가지 않게 하려는 것입니다

```
sudo useradd --system --home /var/lib/lef-receiver --shell /usr/sbin/nologin lefrx
sudo install -d -o lefrx -g lefrx -m 750 /var/lib/lef-receiver
sudo install -d -m 755 /opt/lef-receiver && sudo cp receiver.py /opt/lef-receiver/
sudo python3 -m venv /opt/lef-receiver/.venv && sudo /opt/lef-receiver/.venv/bin/pip install "paho-mqtt>=2,<3"
sudo install -d -m 700 /etc/lighton && sudo install -m 600 deploy/receiver.env.example /etc/lighton/receiver.env   # 값을 채운다
sudo cp deploy/lef-receiver.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now lef-receiver
journalctl -u lef-receiver -f
```

- `retain` 이 참인 메시지는 지금 온 값이 아니라 브로커에 남아 있던 값입니다. 장치가 꺼졌음을 알리는 last will 도 여기에 해당합니다

## 테스트

```
python -m pytest test_t8_judge.py
```

합성 데이터만 씁니다.
