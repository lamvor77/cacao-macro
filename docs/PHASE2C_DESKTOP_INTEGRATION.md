# Phase 2C — PC 프로그램 ↔ 클라우드 실연동

Phase 2A(로컬 서비스 계층)와 Phase 2B(Supabase 스키마/RLS/역할)를 실제
`MainWindow`/`DataManager`/`AutoScheduler`에 연결한 단계. 새 코드는 전부
`services/` 아래에 있고, 기존 `core/scheduler.py`, `core/message_sender.py`,
`core/kakao_controller.py`, `storage/data_manager.py`의 공개 인터페이스는
바꾸지 않았다.

---

## 1. 전체 데이터 흐름

```
프로그램 시작 (MainWindow.__init__)
    │
    ▼
CloudSyncCoordinator.load_local_and_apply()   ← 동기, 로컬 파일 I/O만, 매우 빠름
    │  storage/cloud_sync/messages.json 읽기
    ▼
ControlPanel.load_messages(...)               ← 즉시 UI에 반영 (네트워크 대기 없음)
    │
    ▼
self.after(100, coordinator.start)            ← mainloop 진입 이후로 예약
    │  (mainloop 시작 전에 백그라운드 스레드가 Tk를 건드리면
    │   "main thread is not in main loop" 오류가 나기 때문에 한 박자 늦춤)
    ▼
[백그라운드 스레드] CloudSyncCoordinator._run_loop()
    │
    ├─ CloudSyncService.is_enabled() 확인 (네트워크 없음)
    ├─ AuthService.is_logged_in() 확인 (로컬 파일만 확인, 네트워크 없음)
    │
    ├─ 둘 다 통과하면 → 초기 동기화(pull → 8규칙 비교 → 필요 시 UI/로컬 적용 또는 업로드)
    └─ 이후 SUPABASE_SYNC_INTERVAL_SECONDS(기본 30초)마다 반복 폴링
```

```
사용자가 "메시지 저장" 또는 "목록 저장" 버튼 클릭 (기존 버튼, 그대로 유지)
    │
    ▼
DataManager.save_messages() / DataManager.save()   ← 기존 로직 그대로, 변경 없음
    │  (성공 시에만 아래로 진행)
    ▼
CloudSyncCoordinator.notify_local_save(messages)
    │
    ├─ storage/cloud_sync/messages.json에도 동일 내용 저장 (동기화 기준선)
    ├─ 로컬 dirty 상태 표시
    └─ 로그인+활성화 상태면 백그라운드 스레드로 즉시 업로드 시도
         (실패해도 이미 끝난 로컬 저장에는 영향 없음 — 상태만 "동기화 실패"로 표시)
```

---

## 2. 초기 동기화 정책 (버전/타임스탬프/dirty 비교)

`CloudSyncCoordinator._reconcile()`이 메시지 번호(1~12)마다 아래 표대로 판단한다.
`_local_state`(메시지별 `version`/`dirty`/`last_text`)는 coordinator가
`storage/cloud_sync/local_sync_state.json`에 별도로 보관한다(DataManager의 JSON
포맷은 건드리지 않는다 — 시그니처/파일 형식 변경 없음).

| 상황 | 판단 | 처리 |
|---|---|---|
| 클라우드에 없음, 로컬에 값 있음 | 로컬만 존재 | 업로드 |
| 클라우드에 값 있음, 로컬 dirty 아니고 로컬이 마지막 확인한 버전보다 클라우드가 최신 | 클라우드 최신 | UI+로컬에 적용 |
| 로컬 dirty, 클라우드 버전이 로컬이 마지막으로 확인한 버전과 같거나 그 이하 | 로컬 최신 | 업로드 |
| 로컬 dirty, 그 사이 클라우드 버전이 더 전진 | 양쪽 모두 변경 | **충돌** — 자동 덮어쓰기 금지, 상태만 표시 |
| 그 외(버전 동일, dirty 아님) | 변화 없음 | 아무 것도 안 함 |

충돌이 감지된 메시지는 로컬 값도 클라우드 값도 건드리지 않는다(둘 중 어느
쪽도 조용히 사라지지 않는다). 실제 병합/해결 UI는 이번 단계에 없다 — 상태
라벨이 "충돌 확인 필요"로 바뀌고 로그에 남는 것까지가 범위다.

업로드는 `CloudSyncService.push_messages()`가 이미 갖고 있는 버전 기반 낙관적
잠금(Phase 2A)을 그대로 통과해야 최종 반영된다 — coordinator의 사전 판단과
`CloudSyncService`의 사후 검증이 이중으로 걸린다.

---

## 3. 오프라인 / 실패 동작

- `SUPABASE_ENABLED=false` → `CloudSyncCoordinator`는 어떤 네트워크 호출도 시도하지
  않는다. 상태: **클라우드 미설정**.
- 로그인 세션 없음(`AuthService.is_logged_in() == False`) → 네트워크 호출 시도하지
  않는다. 상태: **로그인 필요**. (Phase 2C 시점에는 로그인 자체가 미구현이라
  항상 이 상태다 — 아래 6절 참고.)
- `pull_messages()`/`push_messages()`가 `error_code == "connection_error"`로 실패
  → 상태: **오프라인**. 다음 폴링에서 자동으로 재시도한다.
- 그 외 이유로 실패 → 상태: **동기화 실패**. 로컬 데이터는 항상 그대로 유지된다
  (실패해도 되돌리지 않는다 — `notify_local_save()`가 로컬 저장을 클라우드
  업로드보다 먼저, 그리고 독립적으로 수행하기 때문).
- 반복 팝업은 없다 — 실패는 상단의 작은 상태 라벨 문구와 로그로만 표시된다.

---

## 4. 폴링 구조

- 이 프로젝트는 PyQt가 아니라 **CustomTkinter/tkinter**를 사용한다. `QTimer`
  대신, `core/scheduler.py`의 `AutoScheduler._loop()`와 동일한 패턴(전용
  `threading.Thread` 데몬 + `threading.Event.wait(interval)`)을 그대로 재사용했다
  — tkinter의 `self.after()`는 메인(UI) 스레드에서만 안전하고, 그 안에서 블로킹
  네트워크 호출을 하면 UI가 멈추므로 실제 폴링 타이밍과 네트워크 호출은 전부
  별도 스레드에서 수행하고, UI 갱신만 `self.after(0, ...)`로 메인 스레드에 넘긴다.
- 기본 주기: `SUPABASE_SYNC_INTERVAL_SECONDS`(기존 변수명 재사용, 기본값 30초).
- 이전 동기화가 끝나지 않았으면(`threading.Lock.acquire(blocking=False)` 실패)
  해당 폴링 주기를 건너뛴다 — 로그에 "polling 건너뜀" 기록.
- 저장 직후 업로드와 폴링이 같은 락을 공유하므로 서로 겹치지 않는다.
- `CloudSyncCoordinator.stop()`이 `threading.Event`를 세팅해 루프를 멈추고, 최대
  2초만 스레드 종료를 기다린 뒤 진행한다(데몬 스레드라 프로세스 종료 시 정리됨) —
  종료가 과도하게 지연되지 않는다.

---

## 5. 클라우드 → UI 반영 시 무한 루프 방지

`CloudSyncCoordinator._applying_remote` 플래그를 클라우드 값을 반영하는 동안
`True`로 설정한다. `notify_local_save()`는 이 플래그가 `True`이면 아무 것도 하지
않고 즉시 반환한다 — 클라우드 적용이 (미래에 추가될 수 있는) 자동저장 훅을 통해
다시 클라우드 업로드를 유발하는 루프를 막기 위한 방어 코드다.

현재 `ControlPanel`에는 "입력할 때마다 자동 저장" 같은 훅이 없고, 저장은
명시적으로 버튼을 눌러야만 일어난다 — 그래서 현재 코드에서는 이 루프가 실제로
발생할 조건 자체가 없다. 하지만 요구사항에 따라 방어 코드는 미리 넣어두었다
(`tests/test_cloud_sync_coordinator.py`의 `test_9_...`가 이 방어가 실제로
동작하는지 검증한다).

클라우드 값 반영 시 항상 다음을 함께 수행한다:

1. `ControlPanel.load_messages()`(기존 메서드) 호출 — UI 반영
2. `DataManager.save_messages()`(기존 메서드) 호출 — 로컬 JSON도 함께 갱신
3. `AutoScheduler.notify_cloud_update()` 호출 — 발송 중이면 "현재 그룹 종료 후
   다음 발송부터 적용" 로그, 아니면 "다음 발송부터 적용" 로그. 실제 반영 시점은
   Phase 2A에서 이미 구현된 그룹 스냅샷 메커니즘이 보장한다(이 문서 3절이 아니라
   `core/scheduler.py`가 담당 — 이번 단계에서 그 로직을 바꾸지 않았다).

---

## 6. 인증(로그인)

> **갱신(Phase 2D): `login_with_google()`을 실제로 구현했다.** 아래 내용은 Phase 2C
> 시점의 기록이다 — 최신 내용은 [PHASE2D_GOOGLE_OAUTH.md](PHASE2D_GOOGLE_OAUTH.md) 참고.

`services/auth_service.py`의 `AuthService`가 다음까지 구현되어 있었다(Phase 2C):

- 저장된 세션 로드 (`get_session()`)
- 만료 확인 (`AuthSession.is_expired()`)
- refresh_token으로 갱신 시도 (`refresh_session()`)
- 세션을 **Windows DPAPI**(`win32crypt.CryptProtectData`/`CryptUnprotectData` —
  `pywin32`는 기존 의존성이라 새로 추가하지 않았다)로 암호화해 현재 Windows
  로그인 계정에서만 복호화 가능한 형태로 저장/로드/삭제

Phase 2C 시점에는 `login_with_google()`이 `NotImplementedError`를 던지는
스텁이었다 — Phase 2D에서 Supabase 공식 PKCE 흐름 + 로컬 loopback 콜백 서버로
실제 구현했다(자세한 내용은 PHASE2D_GOOGLE_OAUTH.md).

---

## 7. 실행 방법

```powershell
pip install -r requirements.txt
python main.py
```

클라우드 동기화를 시험해보려면 `.env`에 `SUPABASE_ENABLED=true`와
`SUPABASE_URL`/`SUPABASE_ANON_KEY`를 채운 뒤 실행한다 — 로그인이 아직 없으므로
상태 라벨은 "로그인 필요"로 표시되고, 로컬 저장/자동발송은 평소와 동일하게
동작한다.

## 8. 환경변수

`.env.example` 참고. Phase 2A에서 이미 정의된 변수명을 그대로 재사용했다(새
변수를 만들지 않음) — `SUPABASE_SYNC_INTERVAL_SECONDS`가 이번 단계부터
`CloudSyncCoordinator`의 폴링 주기로 실제 사용되며, 기본값을 60→30초로 조정했다.

| 변수 | 기본값 | 비고 |
|---|---|---|
| `SUPABASE_ENABLED` | `false` | Phase 2A와 동일 |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | (없음) | Phase 2A와 동일, anon key만 |
| `SUPABASE_SYNC_INTERVAL_SECONDS` | **30**(변경됨, 기존 60) | 폴링 주기(초) |
| `SUPABASE_DEVICE_ID` | 자동 생성 | Phase 2A와 동일 |

## 9. 테스트 방법

```powershell
python -m unittest discover -s tests -v
```

모두 fake/mock으로 동작하며 실제 Supabase 프로젝트에 연결하지 않는다.

- `tests/test_cloud_sync_coordinator.py` — 초기 동기화 8규칙, 오프라인/실패 시
  로컬 유지, polling 중복 방지, 무한 루프 방지, scheduler 연동, 시작 비차단,
  종료 정리 (12개 필수 시나리오 전부 + 게이팅 3건)
- `tests/test_auth_service.py` — DPAPI 저장/로드 왕복, 만료 처리, 로그아웃,
  `login_with_google()`이 명시적 스텁인지
- `tests/test_phase2a_regression.py` — Phase 2A 스케줄러 스냅샷/설정 기본값/
  DataManager 왕복 회귀 (이전에는 세션 스크래치패드에만 있던 것을 이번에
  저장소로 옮김)

## 10. 아직 미구현된 항목

- ~~Google OAuth 브라우저 로그인~~ → **Phase 2D에서 구현 완료**
  ([PHASE2D_GOOGLE_OAUTH.md](PHASE2D_GOOGLE_OAUTH.md) 참고).
- 클라우드 동기화 on/off를 GUI에서 켜고 끄는 토글 (현재는 `.env`로만 제어)
- 충돌(`CONFLICT` 상태)을 사용자가 직접 눌러서 해결하는 UI(둘 중 하나 선택,
  병합 등) — 지금은 상태 표시와 로그까지만
- `push_messages()`가 새 버전 숫자를 반환하지 않으므로(기존 인터페이스를 바꾸지
  않기 위해 그대로 둠), 업로드 직후 정확한 버전 숫자는 다음 폴링 때 확정된다 —
  그 사이 한 번의 무해한 "재확인" 왕복이 있을 수 있다(데이터 손실 없음, 약간의
  비효율만 있음)
- PC 전용 계정으로 로그인시킬지, 실행자별 계정으로 로그인시킬지는
  PHASE2B_ROLES_AUTH_SPEC.md에서 PC 전용 계정으로 정했으나, 로그인 자체는 이제
  누구나(승인된 계정이면) 가능하다 — "PC에는 반드시 이 계정으로만" 같은 강제는
  이번에도 구현하지 않았다(운영 절차로 지켜야 함).
- Redirect URL 와일드카드(`http://127.0.0.1:*/oauth/**`)의 실동작은 Supabase
  공식 예시로 100% 확인되지 않아, 실제 프로젝트에서 1회 검증이 필요하다
  (PHASE2D_GOOGLE_OAUTH.md 8절 참고).
