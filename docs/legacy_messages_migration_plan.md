# 레거시 messages ↔ shared_messages 이원 체계 정리 계획

Production Stabilization Sprint 요구사항 11절 — 지금 이 프로젝트에는 메시지
1~12를 다루는 두 개의 독립된 클라우드 동기화 시스템이 공존한다. 이 문서는
그 현황을 정리하고, 실제로 하나로 합치기 전까지의 안전한 운영 방법과
전환 절차를 설명한다. **이번 스프린트에서 레거시 시스템을 삭제하지
않는다** — 아래는 분석과 준비 단계일 뿐이다.

## 1. 왜 두 시스템이 존재하는가

- **레거시 `messages` 테이블(Phase 2A~2E)**: PC 프로그램 전용, `version` 컬럼
  기반 낙관적 잠금. 로컬 자동저장(2초 디바운스) → 15분 조건부 업로드라는
  자체 흐름을 가진다. (Test Environment Deployment & E2E Validation Sprint —
  상시 30초 전체 조회 polling은 제거됐다. 지금은 로그인 직후 1회 조회 +
  저장/발송 시작·종료/15분 조건부 시 push + 수동 새로고침 시 조회 + 발송
  직전 단건 확인만 서버를 호출한다 — 아래 2절 참고.)
- **`shared_messages`(Mobile 실시간 동기화 스프린트 이후)**: PC+모바일 공용,
  Supabase Realtime 구독(폴링 없음) + `revision` 컬럼 기반 낙관적 잠금,
  `update_shared_message`/`force_update_shared_message` RPC를 통해서만 쓸 수
  있다.

새 기능(모바일 편집, 실시간 반영)이 필요해지면서 레거시 시스템을 개조하는
대신 **완전히 별도의 새 테이블/서비스 계층**으로 구현했다 — "기존 기능을
깨뜨리지 않는다"는 원칙을 지키기 위한 의도적 선택이었다(Mobile 실시간
동기화 스프린트 완료 보고서 참고).

## 2. 레거시 `messages` 테이블을 읽는 코드

- `services/cloud_sync_service.py`의 `pull_messages()`(전체 12건) — 로그인
  직후 1회(`_initial_sync`)와 수동 새로고침(`request_manual_refresh`)에서만
  호출된다. `pull_message(number)`(단건)는 발송 직전 검증
  (`core/legacy_send_verification.py`) 전용이다.
- `services/cloud_sync_coordinator.py`의 `_sync_once()` — 위 두 트리거에서만
  실행되며, 상시 타이머로 자동 호출되지 않는다(15분 dirty tick은 push만
  담당하고 pull은 하지 않는다).

## 3. 레거시 `messages` 테이블을 쓰는 코드

- `services/cloud_sync_service.py`의 `push_messages()`(`_insert_new`/
  `_update_existing`) — `version` 비교 후 `INSERT`/`UPDATE`.
- 호출 경로: `gui/main_window.py`의 `_run_local_autosave()`/
  `_flush_local_autosave_now()`(2초 디바운스 자동저장) →
  `CloudSyncCoordinator.notify_local_autosave()` → 15분 조건부 업로드,
  그리고 저장 버튼/발송 시작/종료 시점의 즉시 업로드 요청.

## 4. `storage/cloud_sync/*.json` 사용 위치

- `messages.json` — `CloudSyncCoordinator`가 관리하는 로컬 상태 캐시(레거시
  전용, revision/dirty 플래그 등).
- `message_cache.json` — `CloudSyncService`가 관리하는 pull 결과 캐시(레거시
  전용, `version` 기반 충돌 감지에 사용).
- `local_sync_state.json` — `LOCAL_PENDING`/`CONFLICT` 판정에 쓰이는 레거시
  전용 부가 상태(`last_synced_text` 등).
- 이 세 파일은 모두 **레거시 시스템 전용**이다 — `shared_messages`는 이
  파일들을 전혀 읽거나 쓰지 않는다(별도의 로컬 캐시 파일을 두지 않고,
  Realtime 재연결 시 항상 서버에서 다시 전체 조회한다).
- `session.dat` — 로그인 세션(암호화). 두 시스템이 공유하는 `AuthService`가
  관리하며, 어느 한쪽 전용이 아니다.

## 5. `shared_messages` 사용 위치

- `services/shared_message_service.py` — RPC 래퍼.
- `services/realtime_message_sync_service.py` — Realtime 구독.
- `core/shared_message_coordinator.py` — PC 쪽 revision 상태 기계.
- `gui/main_window.py`의 "Mobile 실시간 동기화" 섹션 전체.
- `mobile/` 전체(모바일 웹은 오직 `shared_messages`만 다루고, 레거시
  `messages` 테이블은 전혀 알지 못한다 — 모바일 코드 어디에도 `messages`
  테이블 참조가 없다).

## 6. 카카오톡 발송 시 실제 사용되는 메시지 출처

`core/scheduler.py`의 `AutoScheduler._send_group()`이 그룹 시작 시점에
`get_messages_fn()`(=`ControlPanel`에 현재 표시된 텍스트)을 스냅샷으로 뜬
뒤, `verify_message_fn`이 주입되어 있으면(=`shared_messages` 연동이 켜져
있으면) 그 스냅샷을 `shared_messages`의 최신 값으로 갱신한다
(`core/send_verification.py`). 즉:

- **`shared_messages`가 활성화된 정상 상태**: 실제 발송 내용은 항상
  `shared_messages`의 최신 값(또는 확인 실패 시 정책에 따른 캐시)이다.
- **`shared_messages`가 비활성화되었거나 이번 배포에 아예 없는 경우**:
  `ControlPanel`에 표시된 값이 곧 발송 내용이다 — 그 값은 레거시
  `CloudSyncCoordinator`의 로컬/클라우드 동기화 결과다.

두 시스템이 동시에 켜져 있을 때, 화면에 표시되는 값은 "누가 마지막으로
`ControlPanel`에 값을 썼는가"로 결정된다 — 레거시가 먼저 로컬 캐시로 채우고,
이후 `shared_messages`가 연결되면 그 값으로 덮어쓴다(요구사항 12절 우선순위
표와 일치 — `gui/main_window.py`의 `_mark_message_source()` 호출 지점들이
이 순서를 그대로 기록한다). 이 기록은 "진단정보" 화면에서 확인할 수 있다.

## 7. 레거시를 제거하면 영향을 받는 기능

- PC 프로그램의 클라우드 동기화 상태 라벨(☁ 아이콘) — 레거시 전용 표시다.
- `LOCAL_PENDING`/`CONFLICT` 세분화 로직(Phase 2E 후속) — `shared_messages`는
  이미 서버 OCC(revision)로 더 단순하게 같은 문제를 해결하므로 대체 가능하나,
  코드 자체는 별개다.
- 기존 테스트 다수: `tests/test_cloud_sync_coordinator.py`,
  `tests/test_cloud_sync_service_rls.py`,
  `tests/test_local_pending_vs_conflict.py`,
  `tests/test_message_cloud_sync_phase2e.py`,
  `tests/test_phase2a_regression.py`,
  `tests/test_client_sharing.py`(레거시/공유 클라이언트 검증 부분).
- `docs/sql/phase2b_schema.sql`의 `messages`/`message_history` 테이블 정의
  자체(단, `app_users`/역할 관련 부분은 `shared_messages`도 계속 재사용하므로
  삭제 대상이 아니다).

## 8. 단계별 전환 계획

1. **지금(이번 스프린트)**: 병행 운영. `SHARED_MESSAGES_ENABLED`,
   `LEGACY_MESSAGES_SYNC_ENABLED`, `SHARED_MESSAGES_PRIMARY` 세 플래그를
   모두 `true`로 둔다(`config/cloud_settings.py`) — 기본값 변경 없음, 실제
   동작 변화 없음.
2. **검증 기간**: 실제 운영 환경에서 `shared_messages` + Realtime이 최소
   수 주간 안정적으로 동작하는지 확인한다(`docs/e2e_realtime_test_plan.md`
   시나리오 기준). 이 기간에는 두 시스템이 계속 함께 동작해 레거시가
   안전망 역할을 한다.
3. **레거시 폴링 비활성화(가역적)**: 확신이 서면
   `LEGACY_MESSAGES_SYNC_ENABLED=false`로 레거시 클라우드 폴링/업로드만
   끈다 — 로컬 자동저장(2초 디바운스, `storage/*.json` 파일 저장 기능)은
   클라우드와 무관하므로 계속 정상 동작한다. 문제가 생기면 플래그를 다시
   `true`로 되돌리기만 하면 된다(코드 삭제 없음 — 완전히 가역적).
4. **레거시 코드/스키마 실제 제거(별도 스프린트, 비가역적)**: 3단계가
   충분히 안정적으로 유지된 뒤에만 진행한다. 이때 위 7절에 나열된 파일들을
   함께 정리하고, 관련 테스트를 이관/삭제한다. `messages`/`message_history`
   테이블은 데이터 보존이 필요하면 삭제 대신 이름을 바꿔 보관하는 것을
   권장한다(예: `messages_legacy_archived`).

이 문서는 실제 전환이 시작되기 전까지 계속 최신 상태로 유지해야 한다.
