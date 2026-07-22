# 운영 전환 계획 — cacao-macro-test → cacao-macro (v1.2.1 기준)

> ⚠️ **이 문서는 폐기되었습니다(전제 오류, 이후 전략도 재변경됨)**. 최종
> 결정은 `nojdwuoronqmvpdptvlr` 삭제 + `cacao-macro-test` 승격이다.
> **실행 준비는 [`docs/production_switch_execution.md`](production_switch_execution.md)를
> 참고할 것** — 이 문서는 과거 조사 기록으로만 남겨둔다.

**전제(사용자 확인 사실, 이후 정정됨 — 위 경고 참고)**: 운영 프로젝트 `cacao-macro`(`nojdwuoronqmvpdptvlr`)는
테스트 프로젝트 `cacao-macro-test`(`kdyxxkltafeuucijiyzp`)를 만들기 이전
상태 — 즉 Supabase가 기본 제공하는 `auth` 스키마 외에는 이 프로젝트가
필요로 하는 `public` 스키마 객체가 **아무것도 적용되어 있지 않다**고 가정한다
(실제 상태는 `docs/production_project_readiness_check.md`의 1단계 SQL로
먼저 재확인할 것을 권장 — 이 문서는 "완전히 빈 상태"를 전제로 작성됐다).

**이 문서는 조사·계획 문서다. 이 문서를 작성하는 과정에서 Supabase에는
어떤 SQL도 실행하지 않았고, `.env`/Vercel 환경변수도 변경하지 않았다.**

---

## 1. 코드 기준 필요한 Supabase 구조 (v1.2.1)

### 1-1. 테이블 (전부 필수)

| 테이블 | 컬럼 | 비고 |
|---|---|---|
| `app_users` | `id`(uuid, PK, FK→auth.users, cascade), `email`(text not null), `display_name`(text), `status`(app_user_status, default pending), `role`(app_role, default viewer), `approved_by`(uuid FK→app_users), `approved_at`(timestamptz), `created_at`/`updated_at`(timestamptz), `updated_by`(uuid FK→app_users, phase4에서 추가) | 필수 — 인증/승인의 핵심 |
| `messages` | `message_number`(int PK, 1~12), `text`(text), `version`(int), `updated_by`(uuid FK), `updated_at`(timestamptz), `source`(message_source), `device_id`(text) | 필수 — Legacy PC 동기화 경로 유지 |
| `message_history` | `id`(bigint identity PK), `message_number`(int), `text_before`/`text_after`(text), `version_before`/`version_after`(int), `updated_by`(uuid), `updated_by_email`(text), `updated_at`(timestamptz), `source`(message_source), `device_id`(text) | 필수 — 트리거로만 기록 |
| `shared_messages` | `id`(uuid PK), `message_no`(int, 1~12 unique), `title`(text), `content`(text), `revision`(bigint), `is_active`(bool), `updated_at`(timestamptz), `updated_by`(uuid FK, set null), `updated_by_name`(text), `update_source`(shared_message_source), `created_at`(timestamptz) | 필수 — PC/모바일 공용 현재 주력 시스템 |
| `shared_message_history` | `id`(uuid PK), `message_id`(uuid FK, set null), `message_no`(int), `previous_content`/`new_content`(text), `previous_revision`/`new_revision`(bigint), `changed_by`(uuid FK, set null), `changed_by_name`(text), `changed_from`(shared_message_source), `changed_at`(timestamptz) | 필수 — RPC 내부에서만 기록 |
| `admin_audit_logs` | `id`(uuid PK), `actor_user_id`(uuid FK→auth.users, restrict), `target_user_id`(uuid FK→auth.users, set null), `action`(text, 5값 제한), `old_role`/`new_role`(app_role), `old_status`/`new_status`(app_user_status), `reason`(text), `metadata`(jsonb), `created_at`(timestamptz) | 필수 — 관리자 작업 감사 |

### 1-2. enum/type (전부 필수)

- `app_role` — `viewer`/`editor`/`admin`
- `app_user_status` — `pending`/`approved`/`blocked`
- `message_source` — `mobile`/`pc`/`restore`
- `shared_message_source` — `desktop`/`mobile`/`migration`/`system`/`admin_force`

### 1-3. 인덱스 (전부 필수 — 성능/조회 패턴에 직접 대응)

`idx_message_history_number`, `idx_shared_messages_updated_at`,
`idx_shared_message_history_no`, `idx_admin_audit_logs_created_at`,
`idx_admin_audit_logs_actor`, `idx_admin_audit_logs_target`,
`idx_admin_audit_logs_action`, `idx_admin_audit_logs_target_created` (8개)

### 1-4. RPC (Function) — 17개

| 구분 | 함수 |
|---|---|
| 필수 | `fn_handle_new_auth_user`, `fn_log_message_history`, `fn_is_approved`, `fn_current_role`, `fn_is_admin`, `fn_can_edit`, `fn_approved_admin_count`, `fn_assert_not_last_admin`, `admin_list_users`, `admin_approve_user`, `admin_block_user`, `admin_unblock_user`, `admin_update_user_role`, `admin_list_audit_logs`, `update_shared_message`, `force_update_shared_message` (16개) |
| 선택 | `fn_restore_message` — SQL/RLS/권한 체계는 존재하나, **현재 PC/모바일 코드 어디에서도 호출하지 않는다**(직접 확인함). 스키마 완전성을 위해 함께 적용하는 것을 권장하지만, 당장 UI가 없어 실사용되지 않는 기능이다 |

### 1-5. Trigger (전부 필수)

- `trg_on_auth_user_created` (auth.users → app_users 자동 pending 생성)
- `trg_messages_history` (messages 변경 → message_history 자동 기록)

### 1-6. RLS Policy — 10개 (전부 필수)

`app_users_select_self`, `app_users_select_all_if_admin`,
`app_users_update_admin_only`, `messages_select_approved`,
`messages_insert_editor`, `messages_update_editor`,
`message_history_select_approved`, `admin_audit_logs_select_admin_only`,
`shared_messages_select_approved`, `shared_message_history_select_approved`

모든 테이블에서 RLS는 **활성화(enable) 상태여야 하고, FORCE ROW LEVEL
SECURITY는 어디에도 걸지 않는다**(SECURITY DEFINER 함수의 "소유자는 우회"
전제가 깨지므로 — `phase2b_schema.sql` 7절 주석 참고).

### 1-7. Realtime Publication (필수)

`public.shared_messages`를 `supabase_realtime` publication에 등록.
(`messages`/`message_history`/`admin_audit_logs`/`app_users`는 Realtime에
등록하지 않는다 — 코드 어디에도 이들을 구독하는 로직이 없음)

### 1-8. Replica Identity (필수)

`shared_messages`만 `REPLICA IDENTITY FULL` — Realtime UPDATE 이벤트에
변경 안 된 컬럼까지 포함시켜 클라이언트가 revision 비교를 할 수 있게 함.

### 1-9. Auth 설정 (필수)

- Google OAuth Provider 활성화 (Client ID/Secret은 프로젝트별로 Supabase
  대시보드에만 입력 — 저장소 코드에는 절대 넣지 않음)
- Redirect URLs: PC 데스크톱 콜백 패턴(`http://127.0.0.1:*/oauth/**` 또는
  `.env`의 `SUPABASE_OAUTH_CALLBACK_PORTS`로 고정한 포트별 URL) + 모바일
  배포 도메인 콜백 URL
- Google Cloud Console의 OAuth 동의 화면이 실사용자 전원을 커버하는 상태인지
  (테스트 모드면 허용목록 등록된 계정만 로그인 가능)

### 1-10. Storage

**사용 안 함** — 코드 전체(Python/TS)에서 Supabase Storage API 호출을
검색했으나 사용 흔적이 전혀 없다. Edge Functions도 마찬가지로 **사용 안 함**.

---

## 2. 운영 프로젝트에 부족할 것으로 예상되는 객체

사용자가 확인해 준 전제("테스트 프로젝트 생성 이후 운영 프로젝트는 변경
없음")에 따르면, 운영 프로젝트에는 **1절에 나열한 모든 객체가 하나도 없다고
가정**해야 한다:

- 테이블 6개 전부 없음(따라서 그 안의 컬럼도 전부 없음)
- enum/type 4개 전부 없음
- 인덱스 8개 전부 없음
- RPC 17개 전부 없음(선택 항목인 `fn_restore_message` 포함)
- Trigger 2개 전부 없음
- RLS 정책 10개 전부 없음(RLS 자체도 꺼져 있음 — 테이블이 없으니 당연히)
- Realtime publication에 `shared_messages` 미등록
- Google OAuth Provider 미설정, Redirect URL 미등록

즉 이번 작업은 "차이만 보정"이 아니라 **`docs/sql/`의 3개 스키마 파일을
빈 프로젝트에 처음부터 순서대로 적용하는 최초 적용**이다(`docs/production_project_readiness_check.md`의
판정 기준으로는 "B. 스키마만 미적용"에 해당). 다만 **실행 직전에 반드시
1단계 진단 SQL로 이 가정이 실제로 맞는지 재확인**해야 한다(사람의 기억이나
문서가 실제 대시보드 상태와 다를 가능성은 항상 있음).

---

## 3. SQL 파일 분석

| 파일 | 줄 수 | 만드는 것 | 전제 |
|---|---|---|---|
| `docs/sql/phase2b_schema.sql` | 341 | `app_role`/`app_user_status`/`message_source` enum, `app_users`/`messages`/`message_history` 테이블, `fn_handle_new_auth_user`/`fn_log_message_history`/`fn_is_approved`/`fn_current_role`/`fn_is_admin`/`fn_can_edit`/`fn_restore_message` 함수, 트리거 2개 중 1개(`trg_on_auth_user_created`, `trg_messages_history`), RLS 정책 7개 | 없음(가장 먼저 적용) |
| `docs/sql/phase4_admin_rpc.sql` | 654 | `app_users.updated_by` 컬럼 추가, `admin_audit_logs` 테이블, `fn_approved_admin_count`/`fn_assert_not_last_admin`/`admin_list_users`/`admin_approve_user`/`admin_block_user`/`admin_unblock_user`/`admin_update_user_role`/`admin_list_audit_logs` 함수, 인덱스 5개, RLS 정책 1개 | phase2b_schema.sql 적용 후 |
| `docs/sql/shared_messages_realtime.sql` | 373 | `shared_message_source` enum, `shared_messages`/`shared_message_history` 테이블(+시드 1~12행), `update_shared_message`/`force_update_shared_message` 함수, RLS 정책 2개, Realtime publication 등록, REPLICA IDENTITY FULL | phase2b_schema.sql 적용 후(app_users/fn_is_approved 등 재사용) |

**충돌 여부 분석**: 세 파일 모두 다음 idempotent 패턴만 사용하며, 실행
가능한 `DROP TABLE`/`DROP FUNCTION` 문은 (주석 처리된 예시를 제외하고)
**어디에도 없음**을 직접 확인했다.

- `create table if not exists` — 이미 있으면 건너뜀, 기존 데이터 보존
- `create or replace function` — 함수 본문만 갱신, 데이터에 영향 없음
- `create index if not exists` — 이미 있으면 건너뜀
- `drop policy if exists` → `create policy` — 정책만 재정의(테이블 데이터와 무관), 이미 같은 이름의 정책이 있어도 안전하게 교체됨
- `alter table ... add column if not exists`(phase4_admin_rpc.sql의 `app_users.updated_by`) — 이미 컬럼이 있으면 건너뜀, nullable이라 기존 행에 영향 없음
- `do $$ ... exception when duplicate_object ...`(enum 생성) — 이미 있으면 `raise notice`만 하고 통과

**빈 프로젝트에 적용하는 지금 상황에서는** "이미 존재하는 객체와의 충돌"
자체가 발생할 수 없다(전부 신규 생성). 이 idempotency는 오히려 **나중에
실수로 두 번 실행하거나, 부분 실패 후 재실행할 때의 안전장치**로서
의미가 크다.

---

## 4. 운영 반영 절차 (A~J)

> **중요**: phase2b_schema.sql 한 파일 안에 테이블 생성·트리거·RLS가 함께
> 들어 있어, 사용자가 요청한 A~J 카테고리와 "파일 단위 실행"이 1:1로
> 깔끔하게 나뉘지 않는다. 파일을 임의로 쪼개 재작성하면 테스트되지 않은
> 새 SQL을 만드는 셈이라 오히려 위험이 커진다고 판단해, **각 파일은
> 전체를 하나의 단위로 실행**하고 아래 표에서 "이 파일이 어떤 카테고리를
> 담당하는지"만 매핑했다.

| 단계 | 내용 | 실행 SQL | 검증 방법 | 롤백 가능 여부 |
|---|---|---|---|---|
| **A. 사전 점검** | 운영 프로젝트가 정말 비어 있는지 재확인 | `docs/production_project_readiness_check.md`의 1단계 진단 쿼리(읽기 전용) | 결과에서 6개 테이블 전부 "❌ 없음"인지 확인 | 해당 없음(읽기 전용) |
| **B. 백업** | 적용 전 스냅샷 확보 | Supabase 대시보드 `Database → Backups`(플랜에 따라 Point-in-Time Recovery 또는 일일 백업 제공, 무료 플랜은 수동 백업 기능이 제한적일 수 있음 — 플랜 확인 필요) | 백업 목록에 새 백업 항목이 생겼는지 확인 | 해당 없음(생성만 하는 단계) |
| **C. 신규 테이블 생성** | `app_users`/`messages`/`message_history` | `docs/sql/phase2b_schema.sql` 1~4절(파일 전체 실행의 일부) | J단계 진단 SQL에서 3개 테이블 "✅ 존재" | 가능 — `drop table`(단, 지금은 빈 테이블이라 데이터 손실 없음) |
| **D. 컬럼 추가** | `app_users.updated_by` | `docs/sql/phase4_admin_rpc.sql` 0절(파일 전체 실행의 일부, C 다음) | `\d app_users` 또는 Table Editor에서 컬럼 목록 확인 | 가능 — `alter table app_users drop column updated_by`(nullable이라 안전) |
| **E. RPC 생성** | 17개 함수 | `phase2b_schema.sql` 5~6절 + `phase4_admin_rpc.sql` 3~8절 + `shared_messages_realtime.sql` 3~4절(각 파일 실행에 포함됨, 별도 실행 없음) | J단계 진단 SQL에서 17개 함수 전부 "✅ 존재" | 가능 — `drop function`(사용 중인 세션이 없다면 안전) |
| **F. Trigger 생성** | `trg_on_auth_user_created`, `trg_messages_history` | `phase2b_schema.sql`에 포함(C단계와 같은 파일) | `select * from information_schema.triggers where trigger_schema='public';` | 가능 — `drop trigger` |
| **G. RLS 적용** | 10개 정책 + RLS 활성화 | 3개 파일 각각의 마지막 "RLS 활성화 및 정책" 절(각 파일 실행에 포함됨) | J단계 진단 SQL, 또는 `Authentication → Policies` | 가능 — `drop policy`(단, RLS를 끄기 전까지는 서비스에 영향 없음) |
| **H. Realtime 적용** | `shared_messages` → `supabase_realtime` | `shared_messages_realtime.sql` 6절(파일 실행에 포함됨) | `Database → Replication`에서 `shared_messages` 확인 | 가능 — `alter publication supabase_realtime drop table public.shared_messages;` |
| **I. Replica Identity 적용** | `shared_messages` FULL | `shared_messages_realtime.sql` 6절(파일 실행에 포함됨) | 7절의 `select relreplident from pg_class where relname='shared_messages';` = `f` | 가능 — `alter table shared_messages replica identity default;` |
| **J. 사후 검증** | 전체 구조 재확인 | `docs/production_project_readiness_check.md`의 1단계+2단계 진단 쿼리(읽기 전용) | 1절의 필수 항목이 전부 "✅"인지 확인, 6절 체크리스트 수행 | 해당 없음(읽기 전용) |

**실제 실행 순서(파일 단위)**: A → B → `phase2b_schema.sql`(C/D 일부/E 일부/F/G 일부) → `phase4_admin_rpc.sql`(D/E 일부/G 일부) → `shared_messages_realtime.sql`(C 일부/E 일부/G 일부/H/I) → J → Auth 설정(9절, SQL 아님, 대시보드 수동) → 최초 관리자 지정(phase2b_schema.sql 8절 예시, **실제 이메일로 직접 실행 필요 — 이 계획 문서 범위 밖**)

---

## 5. 절대 하면 안 되는 작업 (이번 계획에 포함되지 않음, 앞으로도 자동 실행 금지)

- `DROP TABLE` — 4절 롤백란에 "가능"이라고 적은 것은 어디까지나 사람이 **필요시 수동으로 판단해 실행**할 수 있다는 의미이며, 이 계획의 어떤 자동 절차에도 포함되지 않는다
- `TRUNCATE`
- 운영 데이터에 대한 `DELETE FROM`
- `auth.users` 삭제
- `app_users` 초기화(전체 UPDATE/DELETE로 초기 상태로 되돌리는 것)
- 테스트 데이터 삽입(`phase2b_rls_tests.sql` 같은 테스트 전용 스크립트는 운영 프로젝트에 실행하지 않음)
- `service_role` 키 조회/출력(이 계획 어디에도 `service_role` 키가 필요한 단계 없음 — 전부 `anon` key + 로그인 세션 또는 대시보드 수동 조작만으로 충분)
- 운영 데이터 변경(현재는 데이터가 없으므로 해당 없음이지만, 향후에도 이 계획의 SQL은 스키마 정의만 다루고 실제 메시지/사용자 데이터를 건드리지 않음)

---

## 6. 운영 반영 후 검증 체크리스트

- [ ] 로그인 — 관리자 계정으로 Google 로그인 성공, `app_users`에 pending 행 자동 생성 확인
- [ ] 승인 사용자 — `phase2b_schema.sql` 8절 방식으로 최초 관리자 수동 승인 후, `admin_approve_user` RPC로 추가 사용자 승인 가능한지
- [ ] Realtime 연결 — PC 앱 로그에서 `Subscribed to PostgreSQL` 확인, 모바일에서 연결 상태 "연결됨" 표시
- [ ] shared_messages 조회 — PC/모바일 모두 1~12번 메시지 목록 표시(시드 상태 `content=''`)
- [ ] shared_messages 수정 — `update_shared_message` RPC로 저장 성공, revision 증가 확인
- [ ] Legacy messages 정상 — PC 로그인 직후 1회 조회, 저장 시 push, 상시 polling 없음(기존 회귀 테스트로 검증됨)
- [ ] 발송 전 검증 — shared/legacy 메시지 발송 직전 최신 상태 확인 로직 정상 동작
- [ ] 모바일 조회 — Vercel 배포본에서 로그인 후 메시지 목록/편집 화면 정상 표시
- [ ] 관리자 기능 — 사용자 목록/승인/차단/역할변경/감사로그 조회(운영 관리자 패널)
- [ ] RPC 호출 — `admin_*`/`update_shared_message`/`force_update_shared_message` 전부 정상 응답(권한 없는 역할은 정확히 거부되는지도 함께)
- [ ] RLS 동작 — pending/blocked 계정으로 로그인 시 messages/shared_messages가 보이지 않는지, editor 권한으로 저장 시도 시 허용/viewer는 거부되는지
- [ ] 초기 메시지 이전 — `docs/database_migration.md` 4절 절차대로 PC 로컬 메시지가 있다면 관리자 로그인 시 이전 프롬프트가 뜨는지

---

## 7. PC 프로그램 전환 작업

| 항목 | 필요 여부 | 내용 |
|---|---|---|
| `.env` 변경 | **필요** | 운영 배포 대상 모든 PC의 `.env`를 아래 항목 기준으로 갱신 |
| `APP_ENV` | 변경 | 비워두거나 아예 설정하지 않음(운영 표식 없음 — 테스트 배지 안 뜸) |
| `SUPABASE_ENVIRONMENT` | 변경 | 위와 동일하게 비워둠 |
| `SUPABASE_URL` | **변경** | `nojdwuoronqmvpdptvlr` 프로젝트의 URL로 |
| `SUPABASE_ANON_KEY` | **변경** | 해당 프로젝트의 anon key로 |
| `LICENSE_SECRET_KEY` | **변경 불필요** | 이 값은 Supabase와 무관한 별개의 exe 서명 검증용 비밀키다(v1.2.1의 `verify_build_signature()`). Supabase 프로젝트 전환과 관계없이 그대로 둔다 — 단, 별도로 키 로테이션을 하고 싶다면 그건 완전히 독립적인 결정 |
| 기존 `session.dat` 처리 | **삭제 권장** | 다른 프로젝트(테스트)에서 발급된 세션 토큰은 새 프로젝트(운영)의 Auth 서버가 발급한 것이 아니므로 재사용할 수 없다 — 갱신 시도가 실패하고 애매한 오류로 보일 수 있으므로, 전환 시 `storage/cloud_sync/session.dat`를 삭제해 깨끗하게 재로그인하도록 안내 |
| 로그인 여부 | **전원 재로그인 필요** | `auth.users`는 프로젝트 간 이전되지 않는다 — 기존에 테스트 프로젝트에서 승인됐던 계정이라도 운영 프로젝트에서는 처음 로그인하는 것과 동일하게 취급되어 `pending`으로 새로 생성된다. 관리자가 각 사용자를 다시 승인해야 한다 |
| 최초 데이터 이전 여부 | **필요(자동 프롬프트)** | 관리자 계정으로 최초 로그인 시 `shared_messages`가 시드 상태이므로, PC에 로컬로 남아있던 메시지가 있다면 "초기 메시지 이전" 대화상자가 자동으로 뜬다(`docs/database_migration.md` 4절) — 별도 수동 작업 불필요, 확인만 하면 됨 |

---

## 8. Vercel(모바일 웹) 환경변수

**현재 Vercel에 실제로 어떤 값이 설정되어 있는지는 로컬에서 확인할 수 없다
(이전 조사에서 확인됨 — `.vercel` 링크/설정 파일이 이 저장소에 없음).
아래는 목표로 해야 할 "권장 최종 상태"이며, 실제 변경 전 Vercel 대시보드에서
현재 값을 먼저 확인해야 한다.**

| 환경 | `VITE_SUPABASE_URL` | `VITE_SUPABASE_ANON_KEY` | `VITE_APP_ENV` |
|---|---|---|---|
| **Production** | `nojdwuoronqmvpdptvlr` 프로젝트 URL로 설정/변경 | 해당 프로젝트 anon key로 설정/변경 | 비워두거나 `production`(테스트 배지 안 뜨게) |
| **Preview** | `kdyxxkltafeuucijiyzp`(테스트 프로젝트) URL 유지 | 테스트 프로젝트 anon key 유지 | `test` (PR 미리보기가 실수로 운영 DB를 건드리지 않도록) |
| **Development**(로컬 `vercel dev`/`npm run dev`) | 팀 각자의 `.env.local`에서 테스트 프로젝트 사용 권장 | 위와 동일 | `test` |

**Auth 설정과의 연동**: Production 도메인(Vercel이 발급하는 `*.vercel.app`
도메인 또는 커스텀 도메인)을 운영 Supabase 프로젝트의 `Authentication →
URL Configuration → Redirect URLs`에 반드시 등록해야 모바일 로그인이
동작한다(9절 인증 설정과 함께 진행).

---

## 9. 최종 보고

### 9-1. 운영 프로젝트에 반드시 필요한 변경

1. `docs/sql/phase2b_schema.sql` → `phase4_admin_rpc.sql` → `shared_messages_realtime.sql` 순서로 전체 적용(테이블 6개, enum 4개, RPC 17개, 트리거 2개, RLS 정책 10개, 인덱스 8개)
2. Realtime publication에 `shared_messages` 등록, REPLICA IDENTITY FULL 설정(위 3번 파일에 포함)
3. Google OAuth Provider 활성화 + Redirect URL 등록(PC 콜백 패턴 + 모바일 Production 도메인)
4. 최초 관리자 1명 수동 승인(`phase2b_schema.sql` 8절 예시 UPDATE문, 실제 이메일로)

### 9-2. 선택 사항

- `fn_restore_message` RPC — 스키마에는 포함하는 것을 권장(파일에 이미 들어있어 별도 작업 불필요)하지만, 현재 어떤 클라이언트도 호출하지 않으므로 당장 UI 연결은 불필요
- 프로젝트 이름을 대시보드에서 더 명확하게(예: `cacao-macro-prod`) 바꾸는 것 — 사용자가 이미 "cacao-macro"로 명명했다고 밝혔으므로 필수는 아님

### 9-3. 위험 요소

- **auth.users는 프로젝트 간 이전되지 않는다** — 테스트 프로젝트에서 승인된 모든 사용자가 운영 프로젝트에서는 처음부터 다시 로그인·승인받아야 한다(7절). 사전에 관련자에게 공지 필요
- Google OAuth 동의 화면이 테스트 모드로 남아있으면 실사용자가 로그인 자체를 못 할 수 있음(로컬에서 확인 불가 — Google Cloud Console 직접 확인 필요)
- `session.dat`를 삭제하지 않고 전환하면 사용자 PC에서 로그인 갱신 실패로 인한 혼란스러운 오류가 발생할 수 있음
- 무료/저사양 요금제라면 백업 기능이 제한적일 수 있음(2절 B단계에서 실제 확인 필요)
- 이 계획은 "운영 프로젝트가 정말 비어 있다"는 사용자 확인에 의존한다 — A단계(사전 점검)를 건너뛰면 예상과 다른 상태를 실수로 덮어쓸 위험이 있음

### 9-4. 예상 작업 시간

- SQL 3개 파일 적용 + 사후 검증: 30분~1시간(idempotent라 재시도 부담 적음)
- Google OAuth 설정(Redirect URL 등록·검증 포함): 30분~1시간(포트 와일드카드 동작 여부에 따라 재시도 필요할 수 있음, `docs/PHASE2D_GOOGLE_OAUTH.md` 참고)
- 최초 관리자 승인 + 파일럿 로그인 테스트: 30분
- PC `.env` 전환(배포 대상 PC 수에 비례) + Vercel 환경변수 변경: 30분~수 시간(PC 대수에 따라)
- 전체 검증 체크리스트(6절) 수행: 30분~1시간
- **합계**: 준비된 상태에서 반나절 내외, PC 대수가 많거나 OAuth 리디렉션 문제가 생기면 1일까지 소요 가능

### 9-5. 실행할 SQL 순서

1. (읽기 전용) `docs/production_project_readiness_check.md` 1단계 진단
2. `docs/sql/phase2b_schema.sql` 전체
3. `docs/sql/phase4_admin_rpc.sql` 전체
4. `docs/sql/shared_messages_realtime.sql` 전체
5. (읽기 전용) `docs/production_project_readiness_check.md` 1단계+2단계 진단으로 재확인
6. `phase2b_schema.sql` 8절 예시를 실제 관리자 이메일로 바꿔 수동 1회 실행(최초 관리자 지정)

### 9-6. 실행 전 체크리스트

- [ ] Supabase 대시보드에서 선택된 프로젝트가 `nojdwuoronqmvpdptvlr`(cacao-macro)가 맞는지 URL/프로젝트 ref로 재확인
- [ ] `docs/production_project_readiness_check.md` 1단계 진단으로 정말 비어 있는지 확인
- [ ] 요금제에서 가능한 백업 방식 확인, 가능하면 사전 백업 생성
- [ ] 최초 관리자로 지정할 실제 이메일 확정
- [ ] Google Cloud Console의 OAuth 클라이언트(신규 발급 또는 기존 재사용) 및 Redirect URI 값 준비
- [ ] 모바일 Vercel Production 도메인 확정(Redirect URL 등록에 필요)

### 9-7. 실행 후 체크리스트

- 6절의 전체 항목(로그인/승인/Realtime/shared_messages 조회·수정/Legacy/발송전검증/모바일/관리자기능/RPC/RLS/초기이전) 전부 통과
- PC `.env` 전환 대상 전수 확인, `session.dat` 삭제 여부 확인
- Vercel Production 환경변수 반영 확인, Preview는 여전히 테스트 프로젝트를 가리키는지 확인(운영 오염 방지)
- 테스트 프로젝트(`cacao-macro-test`)는 계속 테스트 환경으로 유지(이번 작업으로 손대지 않음)
