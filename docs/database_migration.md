# Supabase 마이그레이션 — shared_messages (Mobile 실시간 동기화 스프린트)

## 1. 적용 대상 SQL

`docs/sql/shared_messages_realtime.sql` — **아직 실제 Supabase 프로젝트에
적용되지 않은 초안**이다(이 프로젝트 전체 원칙: 실제 Supabase SQL을 자동
실행하지 않는다). 검토 후 Supabase 대시보드의 SQL Editor에서 수동으로
실행해야 한다.

**전제조건**: `docs/sql/phase2b_schema.sql`이 이미 적용되어 있어야 한다
(`app_users` 테이블, `app_role`/`app_user_status` enum,
`fn_is_approved()`/`fn_can_edit()`/`fn_is_admin()` 함수를 그대로 재사용한다
— 이번 스프린트는 새 사용자/역할 테이블을 만들지 않는다).

## 2. 이 마이그레이션이 만드는 것

- `public.shared_messages` — 1~12번 메시지의 현재 상태(Single Source of Truth).
  적용 시 SQL 자체가 1~12번 행을 자동으로 채운다(비어 있지 않으면 손대지
  않는다 — idempotent).
- `public.shared_message_history` — 모든 변경의 감사 이력(클라이언트 직접
  쓰기 불가, RPC만 기록).
- `update_shared_message(...)` RPC — 일반 저장(OCC).
- `force_update_shared_message(...)` RPC — 관리자 전용 강제 저장(충돌 무시,
  초기 마이그레이션에도 사용).
- `shared_messages`에 대한 RLS 정책, Realtime 발행(publication) 등록.

**레거시 `public.messages`/`public.message_history` 테이블은 전혀 건드리지
않는다** — 기존 PC legacy messages 동기화(Phase 2A~2E, 로그인 직후 1회 조회 +
저장/발송 시작·종료/15분 조건부 시 push + 수동 새로고침 + 발송 직전 단건 확인,
상시 polling 없음)는 이 마이그레이션 이후에도 완전히 그대로 동작한다.

## 3. 적용 절차

1. Supabase 대시보드 → SQL Editor.
2. `docs/sql/shared_messages_realtime.sql` 전체 내용을 그대로 붙여넣고 실행한다.
3. 실행 로그에서 오류가 없는지 확인한다(idempotent이므로 재실행해도 안전).
4. `select * from shared_messages order by message_no;`으로 1~12번 행이
   모두 생성되었는지 확인한다(`content=''`, `revision=1`,
   `update_source='system'` 상태여야 정상).

## 4. 초기 데이터 이전 (PC 로컬 메시지 → shared_messages)

기존 PC에 이미 입력해 둔 1~12번 메시지가 있다면, `shared_messages`가 아직
비어 있는(시드 상태) 동안에만 이전해야 한다.

**규칙(요구사항 13절)**:

- 서버에 아직 실제로 수정된 적 없는(`revision=1`이고 `update_source='system'`)
  행만 마이그레이션 대상이다 — 이미 누군가 실제로 수정한 메시지는 건드리지
  않는다(서버 데이터 우선).
- 로컬에도 내용이 있어야 프롬프트가 뜬다(로컬이 비어 있으면 이전할 것이 없음).
- **관리자 계정으로 PC 프로그램을 실행 중일 때만** 이전을 진행할 수 있다
  (`force_update_shared_message` RPC가 admin만 허용).
- 이전 직전 자동으로 로컬 백업이 생성된다(`services/backup_service.py`,
  "진단정보 → 백업 관리"에서도 확인 가능).
- 한 번 이전하면 로컬에 완료 표시 파일
  (`storage/cloud_sync/shared_messages_migration_state.json`)이 남아 같은
  PC에서 다시 묻지 않는다.

### 절차

1. Supabase에 마이그레이션 SQL이 적용된 뒤, 관리자 계정으로 로그인한 상태로
   PC 프로그램을 실행한다.
2. 서버의 1~12번이 모두 시드 상태이고 PC에 입력된 메시지가 있으면, "초기
   메시지 이전" 확인 대화상자가 자동으로 뜬다.
3. 확인하면 PC에 입력된 각 번호의 내용이 `force_update_shared_message`로
   서버에 반영된다(`update_source='migration'`).
4. 완료 로그와 함께 각 메시지의 상태 라벨이 "동기화됨"으로 바뀐다.

### 서버와 로컬이 둘 다 있고 내용이 다른 경우

이 마이그레이션 로직은 "서버가 시드 상태일 때만" 동작하도록 설계되어 있어,
서버에 이미 실제 데이터가 있으면(누군가 이미 한 번이라도 저장했으면) 자동
이전 프롬프트 자체가 뜨지 않는다 — 이 경우 서버 데이터가 곧 정답이다
(요구사항 3절 "Supabase를 Single Source of Truth로 사용"). 그럼에도 로컬
값을 서버에 강제로 반영하고 싶다면, 관리자가 PC의 메시지 편집 화면에서
해당 번호를 직접 열어 서버 최신 내용을 확인한 뒤 수동으로 다시 입력해
저장한다(일반 저장 경로 — OCC가 정상적으로 적용된다).

## 5. 키 로테이션/스키마 변경 시 주의

- `shared_messages`/`shared_message_history`의 컬럼을 바꾸는 경우, 기존
  RPC(`update_shared_message`/`force_update_shared_message`)도 함께 갱신해야
  한다 — 컬럼과 RPC 파라미터/반환값이 항상 일치해야 한다.
- PC(`services/shared_message_service.py`)와 모바일
  (`mobile/src/types.ts`)의 컬럼 이름 정의를 SQL과 동시에 갱신한다.
