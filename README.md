# pyhok12

TETR.IO 화면 픽셀 스캔을 제거하고, Chromium DevTools Protocol(CDP)로 브라우저 내부 게임 상태를 읽어 Hydra / Gomen solver에 넘기는 Windows용 앱입니다.

## 실행 전 준비

1. Python 의존성 설치

```powershell
py -3 -m pip install py-fumen-py openpyxl
```

필수 기본 환경:

- Windows
- Python 3
- `tkinter`
- Node.js 20+

2. Node 의존성 설치

```powershell
npm install
```

3. 원격 디버깅 Chromium 실행

이미 remote debugging 브라우저를 직접 띄워도 되고, 앱이 자동 실행하게 둬도 됩니다.

직접 실행 예시:

```powershell
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="%TEMP%\pyhok12-cdp-profile" https://tetr.io/
```

## 실행

```powershell
py -3 main.py
```

앱은 시작 시 Node 기반 `browser-source/tetrio-cdp-source.mjs`를 실행하고, `runtime/tetrio-snapshot.json`을 계속 갱신합니다.

## 설정

설정 파일은 `config.json`입니다.

주요 항목:

- `port`: CDP 포트
- `auto_launch_chromium`: 이미 열린 브라우저가 없을 때 자동 실행할지 여부
- `stale_after_ms`: snapshot stale 판정 시간
- `required_queue_length`: solver에 넘기기 전에 요구하는 최소 queue 길이
- `poll_ms`: 브라우저 상태 polling 간격

## 동작 방식

- Solo: `ejectState()` / `ejectBoardState()` 기반 상태를 읽습니다.
- VS: WebSocket observer로 `roundId`와 local player identity를 잡고, 브라우저 객체/paused scope에서 local board/current/hold/queue/pieceCounter를 읽습니다.
- Python은 `tetrio_state_source.py`에서 snapshot만 검증하고, solver에는 20x10 top-down locked-board만 전달합니다.

## 상태 파일

런타임에 다음 파일이 생성될 수 있습니다.

- `runtime/tetrio-snapshot.json`
- `runtime/tetrio-vs-object-snapshot.json`
- `runtime/vs-ws-bridge.json`

이 파일들은 Git에 포함하지 않습니다.

## 빠른 검증

Python 문법 확인:

```powershell
py -3 -m py_compile main.py hydra_helper.py gomen_helper.py tetrio_state_source.py app_paths.py tools\setup_finder\setup_finder.py tools\setup_finder\setup_converter.py
```

Node 문법 확인:

```powershell
node --check browser-source\chromium-launch.mjs
node --check browser-source\tetrio-cdp-source.mjs
node --check browser-source\ddd-ws-observer.mjs
node --check browser-source\vs-ws-bridge.mjs
```

테스트 실행:

```powershell
py -3 -m unittest tests.test_tetrio_state_source
```
