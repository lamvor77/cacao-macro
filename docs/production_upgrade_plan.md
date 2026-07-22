# 운영 프로젝트 업그레이드 절차 — cacao-macro (기존 Phase 2 구조 유지)

> ⚠️ **폐기됨(2026-07 운영 전환 결정)**: 실제로는 운영 프로젝트에 데이터가
> 없는 것으로 재확인되어, 이 문서의 "기존 Phase 2 데이터를 보존하며 마이그레이션"
> 전략 대신 `nojdwuoronqmvpdptvlr` 삭제 + `cacao-macro-test` 승격 방식으로
> 최종 결정됐다. **실행 준비는 [`docs/production_switch_execution.md`](production_switch_execution.md)를
> 참고할 것** — 이 문서는 과거 조사 기록으로만 남겨둔다.

**전제(정정됨)**: 운영 프로젝트 `cacao-macro`(`nojdwuoronqmvpdptvlr`)는
비어 있지 않다. `app_users`/`messages`/`message_history`(Phase 2 수준)가
이미 존재하고 실제 데이터가 들어 있다. 테스트 프로젝트
`cacao-macro-test`(`kdyxxkltafeuucijiyzp`)를 만든 뒤로 운영 프로젝트에는
아무 것도 적용하지 않았다.

**목표**: 기존 데이터(app_users/messages/message_history)를 그대로 유지한
채, v1.2.1이 필요로 하는 나머지 구조(`admin_audit_logs`, `shared_messages`,
`shared_message_history`, 관련 RPC/RLS/Realtime)만 추가한다.

**이 문서는 계획 문서다 — 작성 중 Supabase에 어떤 SQL도 실행하지 않았다.**
아래 SQL은 전부 "실행 예정 SQL을 정리한 것"이며, 실제 실행은 사용자가
Supabase 대시보드 SQL Editor에서 직접, 순서대로 진행한다.

---

## ⚠️ 실행 전 반드시 알아야 할 사실 — 순수 추가가 아닌 부분이 1곳 있음

`docs/sql/phase4_admin_rpc.sql`의 10절은 phase2b_schema.sql이 만든 기존
정책 **`app_users_update_admin_only`를 제거하고 다시 만들지 않는다**
(파일 원문 그대로):

```sql
drop policy if exists app_users_update_admin_only on public.app_users;
-- app_users에 UPDATE 정책이 하나도 남지 않으므로, RLS가 켜진 상태에서 UPDATE는
-- (SECURITY DEFINER RPC 경로를 제외하면) 어떤 역할에게도 전면 거부된다.
```

**의미**: 지금까지는 `fn_is_admin()`인 사용자가 `app_users` 테이블을
**클라이언트에서 직접 UPDATE**할 수 있었다(phase2b_schema.sql 정책).
이 파일을 적용하면 그 경로가 완전히 막히고, 이후로는 반드시
`admin_approve_user`/`admin_block_user`/`admin_unblock_user`/
`admin_update_user_role` RPC를 통해서만 `app_users`를 수정할 수 있다.
**이건 버그가 아니라 의도된 보안 강화**(감사로그 누락·마지막 admin 보호
우회를 막기 위함, 파일 원문 주석 참고)이지만, **기존 운영 환경에 이
정책에 의존하는 다른 도구/스크립트가 있다면 그 즉시 동작하지 않게
된다** — 이 문서 0단계에서 이 정책에 의존하는 클라이언트가 없는지
반드시 먼저 확인한다.

---

## 0단계 — 실행 전 호환성 확인 (읽기 전용, 반드시 먼저 실행)

**메뉴 경로**: Supabase 대시보드 → `SQL Editor` → `New query`. 아래 6개
쿼리를 **하나씩 순서대로** 실행하고 결과를 저장해 둔다(2단계 이후 SQL이
안전한지 판단하는 근거가 된다). 전부 `select`만 사용하며 아무것도
수정하지 않는다.

### 0-1) 기존 enum 확인

```sql
select n.nspname as schema, t.typname as enum_name,
       string_agg(e.enumlabel, ', ' order by e.enumsortorder) as values
from pg_type t
join pg_enum e on t.oid = e.enumtypid
join pg_namespace n on n.oid = t.typnamespace
where n.nspname = 'public'
  and t.typname in ('app_role', 'app_user_status', 'message_source')
group by n.nspname, t.typname
order by t.typname;
```

**기대값**: `app_role` = `viewer, editor, admin` / `app_user_status` =
`pending, approved, blocked` / `message_source` = `mobile, pc, restore`.
다르면(값 순서가 아니라 값 자체가 다르면) 1단계를 실행하지 말고 먼저
차이를 검토한다.

### 0-2) 기존 테이블 컬럼 목록 (기대값과 직접 대조)

```sql
select table_name, column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public'
  and table_name in ('app_users', 'messages', 'message_history')
order by table_name, ordinal_position;
```

`docs/production_cutover_plan.md` 1-1절의 기대 컬럼 목록과 눈으로
대조한다. 컬럼이 몇 개 빠져 있거나(예: `approved_by`) 타입이 다르면
2단계를 진행하기 전에 반드시 알려줄 것 — idempotent 파일이라 없는
컬럼을 자동으로 추가해 주지는 않는다(`CREATE TABLE IF NOT EXISTS`는
테이블이 이미 있으면 컬럼 차이를 보정하지 않고 그냥 건너뛴다).

### 0-3) Phase 2 함수/트리거/정책 존재 여부

```sql
select 'function' as kind, e.name as name,
       case when exists(
         select 1 from pg_proc p
         where p.proname = e.name and p.pronamespace = 'public'::regnamespace
       ) then '✅ 존재' else '❌ 없음' end as status
from (values ('fn_handle_new_auth_user'), ('fn_log_message_history'), ('fn_is_approved'),
             ('fn_current_role'), ('fn_is_admin'), ('fn_can_edit'), ('fn_restore_message')
     ) as e(name)
union all
select 'trigger', e.name,
       case when exists(
         select 1 from information_schema.triggers it
         where it.trigger_schema = 'public' and it.trigger_name = e.name
       ) then '✅ 존재' else '❌ 없음' end
from (values ('trg_on_auth_user_created'), ('trg_messages_history')) as e(name)
union all
select 'policy', e.table_name || '/' || e.policy_name,
       case when pol.policyname is not null then '✅ 존재' else '❌ 없음' end
from (values
  ('app_users','app_users_select_self'),
  ('app_users','app_users_select_all_if_admin'),
  ('app_users','app_users_update_admin_only'),
  ('messages','messages_select_approved'),
  ('messages','messages_insert_editor'),
  ('messages','messages_update_editor'),
  ('message_history','message_history_select_approved')
) as e(table_name, policy_name)
left join pg_policies pol
  on pol.schemaname = 'public' and pol.tablename = e.table_name and pol.policyname = e.policy_name
order by kind, name;
```

이 쿼리 결과가 **전부 "✅ 존재"이면 1단계(phase2b_schema.sql 재실행)를
건너뛴다** — 이미 다 있는 것을 다시 만들 이유가 없다(운영 테이블을
불필요하게 다시 건드리지 않는 것이 더 안전하다). **하나라도 "❌
없음"이면 1단계를 실행**해 누락분을 보강한다.

### 0-4) 기존 데이터 규모 확인 (실제 운영 데이터임을 재확인)

```sql
select 'app_users' as table_name, count(*) as row_count from public.app_users
union all
select 'messages', count(*) from public.messages
union all
select 'message_history', count(*) from public.message_history;
```

행 수가 0이 아님을 확인한다(0이면 애초에 이 문서가 아니라
`docs/production_cutover_plan.md`의 전제가 맞는 상황이므로 다시 확인
필요).

### 0-5) phase4/shared_messages 객체가 이미 부분적으로라도 있는지 확인

```sql
select 'admin_audit_logs 테이블' as item,
       exists(select 1 from information_schema.tables where table_schema='public' and table_name='admin_audit_logs') as exists
union all
select 'app_users.updated_by 컬럼',
       exists(select 1 from information_schema.columns where table_schema='public' and table_name='app_users' and column_name='updated_by')
union all
select 'shared_messages 테이블',
       exists(select 1 from information_schema.tables where table_schema='public' and table_name='shared_messages')
union all
select 'shared_message_history 테이블',
       exists(select 1 from information_schema.tables where table_schema='public' and table_name='shared_message_history');
```

전부 `false`로 예상된다(사용자 확인 사실과 일치). `true`가 하나라도
나오면 이전에 부분 적용을 시도한 적이 있다는 뜻이므로, 2~3단계 실행
전에 그 부분을 짚어야 한다.

### 0-6) 롤백 대비 — 기존 함수/정책 원문 백업 (1단계를 실행하게 될 경우 대비)

0-3에서 "❌ 없음"이 하나라도 있어 1단계를 실행하게 된다면, 실행 **전에**
아래로 현재 상태를 반드시 저장해 둔다(이미 있는 함수는 `CREATE OR
REPLACE`로 덮어써지므로, 되돌리려면 원래 정의가 필요하다):

```sql
-- 기존 함수 원문(있는 것만 나옴)
select p.proname as function_name, pg_get_functiondef(p.oid) as current_definition
from pg_proc p
where p.pronamespace = 'public'::regnamespace
  and p.proname in ('fn_handle_new_auth_user','fn_log_message_history','fn_is_approved',
                     'fn_current_role','fn_is_admin','fn_can_edit','fn_restore_message');

-- 기존 정책 원문(있는 것만 나옴)
select tablename, policyname, cmd, qual, with_check
from pg_policies
where schemaname = 'public'
  and tablename in ('app_users','messages','message_history');
```

결과를 복사해 별도 텍스트 파일로 저장해 둔다.

---

## 1. 운영 프로젝트에 실제 실행할 SQL (순서대로)

| 순서 | 파일명 | 실행 조건 | 실행 이유 |
|---|---|---|---|
| 1 | `docs/sql/phase2b_schema.sql` | **조건부** — 0-3 결과에 "❌ 없음"이 하나라도 있을 때만 | 누락된 Phase2 함수/트리거/정책만 보강. `CREATE TABLE IF NOT EXISTS`라 기존 `app_users`/`messages`/`message_history`는 손대지 않는다(존재하면 스킵). 0-3에서 전부 ✅면 **건너뛴다** |
| 2 | `docs/sql/phase4_admin_rpc.sql` | **항상 실행** | `admin_audit_logs` 테이블, `app_users.updated_by` 컬럼(nullable 추가라 안전), 관리자 RPC 8개가 아직 없음(0-5로 확인). **단, 위 "⚠️ 실행 전 알아야 할 사실"의 정책 제거가 함께 실행됨을 인지하고 진행** |
| 3 | `docs/sql/shared_messages_realtime.sql` | **항상 실행** | `shared_messages`/`shared_message_history`가 아직 없음. `phase2b_schema.sql`의 `app_users`/`fn_is_approved()` 등을 재사용하며 새로 만들지 않는다(파일 자체의 전제) |

**세 파일 모두 SQL Editor에 파일 전체 내용을 그대로 붙여넣어 한 번에
실행한다** — 파일 내부를 임의로 잘라서 일부만 실행하지 않는다(각 파일은
내부적으로 이미 안전한 순서로 작성되어 있고, 일부만 발췌 실행하면 오히려
검증되지 않은 조합이 된다).

---

## 2. 운영 프로젝트에서 절대 실행하면 안 되는 SQL

| SQL/파일 | 이유 |
|---|---|
| `docs/sql/phase2b_rls_tests.sql` | 테스트 전용 스크립트 — `auth.users`에 `test-%@example.com` 가짜 계정을 직접 INSERT한다. 운영 계정과 섞이면 안 됨. 파일 자체의 안전장치(`app_users`/`messages`가 비어있어야 통과)도 지금 운영 프로젝트에는 데이터가 있으므로 실행 시 스스로 즉시 중단되지만,애초에 시도해서는 안 된다 |
| `docs/sql/phase2b_rls_tests_cleanup.sql` | 위와 짝을 이루는 정리 스크립트 — `test-%@example.com` 패턴이 아닌 행이 하나라도 있으면 중단하는 안전장치가 있지만(현재 운영 계정들이 이 패턴이 아니므로 중단될 것), 애초에 운영 프로젝트에서 실행할 이유가 없다 |
| 3개 스키마 파일 헤더의 **주석 처리된 롤백용 DROP 문**(`shared_messages_realtime.sql` 27~45행) | 이번 반영 절차에는 포함되지 않는다 — 문제가 생겨도 먼저 이 문서 4절의 개별 롤백을 시도하고, 전체 DROP은 최후의 수단으로 사람이 직접 판단해서만 실행 |
| `TRUNCATE table_name` (어떤 테이블이든) | 운영 데이터 전체 삭제 |
| `DELETE FROM app_users` / `DELETE FROM messages` / `DELETE FROM message_history` (조건 없이) | 기존 운영 데이터 삭제 |
| `DROP TABLE app_users` / `messages` / `message_history` | 기존 운영 데이터가 있는 테이블 — 이번 3개 파일 어디에도 이런 문장은 없지만, 사람이 실수로라도 실행하면 안 됨 |
| `ALTER TABLE app_users ALTER COLUMN ...` 류로 기존 컬럼 타입/제약을 바꾸는 임의 SQL | 이번 3개 파일에는 없음. 0-2에서 컬럼 차이가 발견되더라도, 컬럼 타입을 변경하는 SQL은 이 계획에 포함하지 않는다 — 별도로 영향도 분석 후 판단 |
| `phase2b_schema.sql` 8절의 관리자 지정 UPDATE문을 **확인 없이** 실행 | 운영에는 이미 실사용 데이터가 있으므로 기존에 이미 admin이 지정되어 있을 가능성이 높다 — 3단계 실행 후 검증 SQL로 기존 admin 존재 여부부터 확인하고, 필요한 경우에만(신규 admin 추가가 필요한 경우) 실제 이메일로 실행 |
| `service_role` 키를 조회/입력하는 어떤 SQL이나 API 호출 | 이번 계획의 어떤 단계도 `service_role` 키가 필요하지 않다 |

---

## 3. 각 SQL 실행 직후 즉시 확인할 검증 SQL

### 3-1) `phase2b_schema.sql` 실행한 경우 (실행했다면)

```sql
-- 0-3과 동일한 쿼리를 다시 실행 — 전부 "✅ 존재"로 바뀌었는지 확인
-- (0-3 쿼리 재사용 — 위 0단계 참고)

-- 기존 행 수가 그대로인지 재확인(데이터 유실 없음 재확인)
select 'app_users' as table_name, count(*) as row_count from public.app_users
union all
select 'messages', count(*) from public.messages
union all
select 'message_history', count(*) from public.message_history;
```

0-4에서 기록해 둔 행 수와 **정확히 같아야 한다**(하나라도 줄었으면 즉시
중단하고 원인 파악).

### 3-2) `phase4_admin_rpc.sql` 실행 후

```sql
-- 신규 테이블/컬럼 생성 확인
select
  exists(select 1 from information_schema.tables where table_schema='public' and table_name='admin_audit_logs') as admin_audit_logs_exists,
  exists(select 1 from information_schema.columns where table_schema='public' and table_name='app_users' and column_name='updated_by') as updated_by_column_exists;

-- 신규 RPC 8개 확인
select proname, pg_get_function_identity_arguments(oid) as args
from pg_proc
where pronamespace = 'public'::regnamespace
  and proname in ('fn_approved_admin_count','fn_assert_not_last_admin','admin_list_users',
                   'admin_approve_user','admin_block_user','admin_unblock_user',
                   'admin_update_user_role','admin_list_audit_logs')
order by proname;
-- 8행이 나와야 한다

-- 기존 app_users_update_admin_only 정책이 예정대로 제거됐는지 확인(의도된 변경)
select exists(
  select 1 from pg_policies
  where schemaname='public' and tablename='app_users' and policyname='app_users_update_admin_only'
) as old_direct_update_policy_still_exists;
-- false여야 정상(제거됨) — true면 아직 옛 정책이 남아있다는 뜻(예상과 다름, 확인 필요)

-- 기존 app_users 행 수 재확인(유실 없음)
select count(*) from public.app_users;

-- 이미 승인된 관리자가 있는지 확인(있으면 8절 admin 지정 SQL 생략 가능)
select id, email, role, status from public.app_users where status='approved' and role='admin';
```

### 3-3) `shared_messages_realtime.sql` 실행 후

```sql
-- 12개 시드 행 생성 확인
select message_no, content, revision, update_source
from public.shared_messages
order by message_no;
-- 12행, content='', revision=1, update_source='system'이어야 정상

-- RPC 2개 확인
select proname, pg_get_function_identity_arguments(oid) as args
from pg_proc
where pronamespace = 'public'::regnamespace
  and proname in ('update_shared_message', 'force_update_shared_message');

-- Realtime publication 등록 확인
select exists (
  select 1 from pg_publication_tables
  where pubname = 'supabase_realtime' and schemaname='public' and tablename='shared_messages'
) as realtime_registered;

-- REPLICA IDENTITY 확인
select relreplident from pg_class
where relname = 'shared_messages' and relnamespace = 'public'::regnamespace;
-- 'f'(FULL)여야 정상

-- 레거시 messages/message_history가 이번 파일로 전혀 변경되지 않았는지 재확인
select count(*) from public.messages;
select count(*) from public.message_history;
-- 3-1/3-2에서 기록한 값과 동일해야 함
```

---

## 4. Rollback 방법

**원칙**: 운영에 이미 존재하던 `app_users`/`messages`/`message_history`는
**절대 DROP하지 않는다.** 아래 롤백은 전부 "이번에 새로 추가된 것만
제거"하는 방향이다.

### 4-1) `phase2b_schema.sql`을 실행했던 경우

기존 함수/정책을 덮어썼다면(0-6에서 백업한 원문이 있는 경우), 그 원문을
그대로 다시 `CREATE OR REPLACE FUNCTION` / `CREATE POLICY`로 재실행해
복원한다. **테이블(`app_users`/`messages`/`message_history`) 자체나
트리거 삭제는 하지 않는다** — 문제가 함수/정책 로직에 있는 것이지 테이블
구조에 있는 것이 아니기 때문에, 테이블까지 건드릴 필요가 없다.

### 4-2) `phase4_admin_rpc.sql` 롤백 (전부 신규 추가물이라 안전하게 되돌릴 수 있음)

```sql
-- 관리자 RPC 8개 제거
drop function if exists public.admin_list_audit_logs(uuid, text, integer, integer);
drop function if exists public.admin_update_user_role(uuid, text, text);
drop function if exists public.admin_unblock_user(uuid, text, text);
drop function if exists public.admin_block_user(uuid, text);
drop function if exists public.admin_approve_user(uuid, text, text);
drop function if exists public.admin_list_users(text, text, text, integer, integer);
drop function if exists public.fn_assert_not_last_admin(uuid);
drop function if exists public.fn_approved_admin_count();

-- admin_audit_logs 테이블 제거(신규 테이블 — 기존 데이터 아님)
drop table if exists public.admin_audit_logs;

-- 신규 컬럼 제거(nullable로 추가됐으므로 제거해도 다른 컬럼에 영향 없음)
alter table public.app_users drop column if exists updated_by;

-- 제거됐던 기존 정책을 원래대로 복원(선택 — 클라이언트 직접 UPDATE를 다시 허용하고 싶을 때만)
create policy app_users_update_admin_only
    on public.app_users for update
    using (public.fn_is_admin())
    with check (public.fn_is_admin());
```

**주의**: 마지막 정책 복원은 "직접 UPDATE 경로를 되살리는" 되돌림이므로,
admin_* RPC 쪽 감사로그/마지막 admin 보호를 이미 신뢰하고 쓰기 시작했다면
복원하지 않는 것을 권장한다(둘 중 하나만 선택).

### 4-3) `shared_messages_realtime.sql` 롤백

```sql
-- 1) 실행 권한만 우선 차단하고 싶을 때(테이블/데이터는 보존)
revoke execute on function public.update_shared_message(integer, text, text, bigint, public.shared_message_source) from authenticated;
revoke execute on function public.force_update_shared_message(integer, text, text, public.shared_message_source) from authenticated;

-- 2) Realtime 발행만 되돌리고 싶을 때(테이블은 유지, 이벤트만 중단)
alter publication supabase_realtime drop table public.shared_messages;

-- 3) 완전히 되돌리고 싶을 때(주의: 그 사이에 실제로 저장된 메시지가 있다면 함께 사라짐 —
--    3-3 검증에서 시드 상태 그대로였는지 먼저 확인할 것)
-- drop function if exists public.force_update_shared_message(integer, text, text, public.shared_message_source);
-- drop function if exists public.update_shared_message(integer, text, text, bigint, public.shared_message_source);
-- drop table if exists public.shared_message_history;
-- drop table if exists public.shared_messages;
```

(이 3단 구성은 `shared_messages_realtime.sql` 파일 자체의 공식 롤백
안내를 그대로 재사용한 것이다.)

---

## 5. 운영 반영 후 전체 확인 체크리스트

### PC
- [ ] 관리자 계정으로 로그인 성공(기존 승인된 계정 — 재승인 불필요, `auth.users`/`app_users`를 그대로 유지했으므로)
- [ ] 창 제목에 `v1.2.1` 정상 표시, TEST ENVIRONMENT 배지 없음(운영 `.env` 기준)
- [ ] Legacy 메시지(`messages`) 정상 조회 — 기존에 저장돼 있던 내용 그대로 보이는지
- [ ] Legacy 메시지 저장 시 `message_history`에 이력 기록되는지
- [ ] 로그인 직후 1회 조회, 상시 polling 없음(로그로 재확인)

### 모바일
- [ ] Vercel Production 배포본에서 Google 로그인 성공
- [ ] `shared_messages` 1~12번 목록 조회(초기엔 전부 시드 상태 `content=''`)
- [ ] 메시지 편집·저장 시 `revision` 증가, `shared_message_history`에 기록

### Realtime
- [ ] PC 로그에서 `Subscribed to PostgreSQL` 확인
- [ ] 모바일에서 한 클라이언트가 저장하면 다른 클라이언트 화면에 실시간 반영되는지(두 세션으로 확인)
- [ ] 연결 상태 표시가 "연결됨"으로 나오는지

### Legacy
- [ ] 기존 `app_users` 승인 상태가 전환 전후로 그대로인지(3-2 검증 결과와 비교)
- [ ] 기존 `messages`/`message_history` 행 수가 전환 전후로 정확히 같은지(0-4 vs 3-1/3-3 비교)
- [ ] `fn_is_admin()`/`fn_can_edit()`/`fn_is_approved()` 기반 권한 로직이 여전히 정상 동작(pending/blocked 계정으로 접근 제한 확인)

### Shared Messages
- [ ] `update_shared_message` RPC로 저장 성공 및 OCC(동시 저장 충돌) 정상 동작
- [ ] `force_update_shared_message`는 admin만 성공, 일반 사용자는 `PERMISSION_DENIED`
- [ ] 초기 메시지 이전 프롬프트(`docs/database_migration.md` 4절) — PC 로컬에 메시지가 있다면 관리자 최초 로그인 시 자동으로 뜨는지

### 관리자 기능(4-2에서 확인한 새 RPC 경로)
- [ ] `admin_list_users`/`admin_approve_user`/`admin_block_user`/`admin_unblock_user`/`admin_update_user_role`/`admin_list_audit_logs` 전부 관리자 계정으로 정상 호출
- [ ] 일반 사용자 계정으로 위 RPC 호출 시 전부 `ADMIN_REQUIRED`로 거부
- [ ] `app_users`를 RPC 없이 직접 UPDATE 시도 시 거부되는지(정책이 예정대로 제거됐으므로 — 이 동작 변화를 실제 운영자에게도 공지할 것)
