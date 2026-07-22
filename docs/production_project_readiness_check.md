# 운영 후보 Supabase 프로젝트(`nojdwuoronqmvpdptvlr`) 준비 상태 점검 절차

> ⚠️ **폐기됨(2026-07 운영 전환 결정)**: `nojdwuoronqmvpdptvlr` 프로젝트는
> 삭제하고, `cacao-macro-test`(`kdyxxkltafeuucijiyzp`)를 운영으로 승격하는
> 방식으로 최종 결정됐다 — 이 문서의 점검 대상 프로젝트 자체가 더 이상
> 존재하지 않는다. **실행 준비는 [`docs/production_switch_execution.md`](production_switch_execution.md)를
> 참고할 것** — 이 문서는 과거 조사 기록으로만 남겨둔다.

이 문서는 `nojdwuoronqmvpdptvlr`가 실제로 운영 배포에 쓸 수 있는 상태인지
**사람이 Supabase 대시보드에서 직접 확인**하기 위한 체크리스트다. Claude는
Supabase API/CLI 접근 권한이 없어 이 프로젝트의 실제 상태를 직접 조회할 수
없으므로, 아래 절차를 사용자가 직접 수행한 뒤 결과를 알려주면 Claude가
판정(11절)을 대신 정리해 줄 수 있다.

**절대 원칙**: 이 문서의 어떤 단계도 데이터를 쓰거나 지우지 않는다. 10절의
SQL은 전부 `select`/조회 전용이며, `insert`/`update`/`delete`/`drop`은 어디에도
없다. 환경변수나 `.env`도 이 점검 중에는 변경하지 않는다.

**대상 프로젝트 확인**: 아래 모든 단계를 시작하기 전에, Supabase 대시보드
좌측 상단의 프로젝트 선택 드롭다운에서 **반드시 `nojdwuoronqmvpdptvlr`
프로젝트가 선택되어 있는지 먼저 확인한다** — `kdyxxkltafeuucijiyzp`(테스트
프로젝트)와 혼동하지 않도록 프로젝트 ref 문자열을 URL 표시줄에서도 한 번 더
대조할 것.

---

## 1. 프로젝트 기본정보

**메뉴 경로**: `Project Settings`(좌측 하단 톱니바퀴) → `General`

| 확인 항목 | 위치 | 기록할 값 |
|---|---|---|
| 프로젝트 이름 | General 탭 상단 "Project Name" | |
| 상태(Active/Paused 등) | 대시보드 프로젝트 목록 카드 또는 General 탭 상단 배지 | |
| 리전(Region) | General 탭 "Region" | |
| 요금제(Plan) | `Project Settings` → `Billing` → `Subscription Plan` (또는 조직 단위 Billing 페이지) | |

> 이름이 "cacao-macro-test"나 그와 유사한 테스트 성격의 이름이라면, 애초에
> 잘못된 프로젝트를 열었을 가능성이 있으니 프로젝트 ref를 다시 확인한다.

---

## 2~3. Database Tables — 존재 여부와 데이터 건수

**메뉴 경로**: 좌측 사이드바 `Table Editor` (또는 `Database` → `Tables`)

확인할 테이블 6개와 존재 시 함께 기록할 정보:

| 테이블 | 존재 여부(예/아니오) | 대략 행 수 | RLS 표시(테이블 목록 옆 자물쇠 아이콘) |
|---|---|---|---|
| `app_users` | | | |
| `messages` | | | |
| `message_history` | | | |
| `shared_messages` | | | |
| `shared_message_history` | | | |
| `admin_audit_logs` | | | |

- `Table Editor`에서 `public` 스키마를 선택한 상태로 좌측 테이블 목록에 위
  6개 이름이 보이는지 확인한다. 하나도 안 보이면 스키마가 전혀 적용되지
  않은 상태다(10절 판정 B).
- 각 테이블을 클릭하면 우측 상단에 대략적인 행 수가 표시된다(정확한 값이
  필요하면 10절의 SQL 또는 Table Editor에서 직접 `count` 실행 — 읽기 전용).
- 테이블 이름 옆 자물쇠 아이콘이 잠겨 있으면 RLS가 켜진 것이다(잠금 해제
  아이콘이면 RLS 꺼짐 — 운영에는 반드시 켜져 있어야 한다).

정확한 건수·RLS 상태는 10절의 SQL 결과가 더 신뢰할 수 있다(대시보드 카운트는
근사치를 보여줄 때가 있음).

---

## 4. Database Functions — RPC 17개 확인

**메뉴 경로**: 좌측 사이드바 `Database` → `Functions`

목록에서 아래 17개 함수명이 모두 보이는지 하나씩 대조한다:

1. `fn_handle_new_auth_user`
2. `fn_log_message_history`
3. `fn_is_approved`
4. `fn_current_role`
5. `fn_is_admin`
6. `fn_can_edit`
7. `fn_restore_message`
8. `fn_approved_admin_count`
9. `fn_assert_not_last_admin`
10. `admin_list_users`
11. `admin_approve_user`
12. `admin_block_user`
13. `admin_unblock_user`
14. `admin_update_user_role`
15. `admin_list_audit_logs`
16. `update_shared_message`
17. `force_update_shared_message`

목록이 비어 있으면 스키마가 전혀 적용되지 않은 상태다. 일부만 보이면 부분
적용 상태(10절 판정 D) — 정확히 몇 개가 빠졌는지는 10절 SQL 결과가 더
정확하다.

---

## 5. RLS 활성화 여부와 필수 정책 확인

**메뉴 경로**: 좌측 사이드바 `Authentication` → `Policies` (테이블별 정책을
한 화면에서 볼 수 있음), 또는 `Table Editor`에서 테이블별 자물쇠 아이콘.

확인할 정책 10개(테이블별):

| 테이블 | 정책명 |
|---|---|
| `app_users` | `app_users_select_self` |
| `app_users` | `app_users_select_all_if_admin` |
| `app_users` | `app_users_update_admin_only` |
| `messages` | `messages_select_approved` |
| `messages` | `messages_insert_editor` |
| `messages` | `messages_update_editor` |
| `message_history` | `message_history_select_approved` |
| `admin_audit_logs` | `admin_audit_logs_select_admin_only` |
| `shared_messages` | `shared_messages_select_approved` |
| `shared_message_history` | `shared_message_history_select_approved` |

`Authentication → Policies` 화면에서 각 테이블을 펼치면 정책 목록과 RLS
전체 활성화 여부(테이블 상단 토글)가 함께 보인다. 6개 테이블 모두 RLS가
"Enabled"로 표시되어야 하고, 위 10개 정책명이 정확히 일치해야 한다(이름이
다르면 다른 버전의 정책일 수 있음 — 10절 SQL로 정밀 대조 권장).

---

## 6. Database Publications — `shared_messages`의 Realtime 등록 확인

**메뉴 경로**: 좌측 사이드바 `Database` → `Replication`

`supabase_realtime` publication을 클릭하면 등록된 테이블 목록이 나온다.
`public.shared_messages`가 목록에 있는지 확인한다. 없으면 Realtime 구독이
동작하지 않는다(모바일 실시간 갱신 불가).

---

## 7. `shared_messages`의 REPLICA IDENTITY 확인 (SQL 필요)

이 값은 대시보드 UI에 노출되지 않으므로 SQL Editor에서 아래 **읽기 전용**
쿼리로만 확인 가능하다(10절의 통합 진단 SQL에도 포함되어 있음):

```sql
select relreplident
from pg_catalog.pg_class
where relname = 'shared_messages'
  and relnamespace = 'public'::regnamespace;
```

결과가 `f`이면 FULL(정상)이다. `d`(default), `n`(nothing), `i`(index)면
Realtime UPDATE 이벤트에 변경되지 않은 컬럼이 누락될 수 있어 재설정이
필요하다(`alter table public.shared_messages replica identity full;` —
**이 점검 단계에서는 실행하지 않는다**, 판정 후 별도 작업으로 진행).

---

## 8. Authentication Users

**메뉴 경로**: 좌측 사이드바 `Authentication` → `Users`

| 확인 항목 | 방법 |
|---|---|
| 사용자 수 | Users 목록 페이지 상단/하단에 전체 건수 표시 |
| 승인(approved) 사용자 존재 여부 | Users 페이지만으로는 알 수 없음(승인 상태는 `public.app_users`에 있음) — `Table Editor` → `app_users` 테이블에서 `status` 컬럼이 `approved`인 행이 있는지 필터링해서 확인, 또는 10절 SQL 사용 |
| 최초 관리자(admin) 존재 여부 | 위와 동일하게 `app_users`에서 `status='approved' AND role='admin'`인 행 존재 여부 확인 |

사용자가 0명이면 이 프로젝트로 아직 아무도 로그인한 적이 없다는 뜻이다
(Auth 자체는 켜져 있어도 실제 사용 이력이 없을 수 있음).

---

## 9. Authentication Providers

**메뉴 경로**: 좌측 사이드바 `Authentication` → `Providers` (Google 활성화
여부), `Authentication` → `URL Configuration` (Redirect URL 목록)

| 확인 항목 | 판정 기준 |
|---|---|
| Google Provider 활성화 여부 | `Providers` 목록에서 Google 항목의 토글이 켜져 있고 Client ID가 입력되어 있는지(값 자체를 기록/공유하지 말 것) |
| Redirect URL — PC 데스크톱 콜백 | `URL Configuration → Redirect URLs`에 `http://127.0.0.1:*/oauth/**` 패턴 또는 PC `.env`의 `SUPABASE_OAUTH_CALLBACK_PORTS`에 지정된 고정 포트별 URL(`http://127.0.0.1:<포트>/oauth/**`)이 등록되어 있는지 |
| Redirect URL — 모바일 배포 도메인 | 실제 Vercel Production 도메인의 콜백 URL(예: `https://<도메인>/**`)이 등록되어 있는지 — Vercel 쪽 실제 도메인은 별도로 확인 필요 |
| Google Cloud Console OAuth 동의 화면 상태 | Supabase 대시보드가 아니라 [Google Cloud Console](https://console.cloud.google.com/auth) → OAuth 동의 화면에서 "테스트" 또는 "프로덕션" 게시 상태 확인(테스트 상태면 허용목록에 없는 사용자는 로그인 불가) |

---

## 10. SQL Editor — 읽기 전용 운영 준비 상태 진단

**메뉴 경로**: 좌측 사이드바 `SQL Editor` → `New query`

아래 쿼리는 **오직 `information_schema`/`pg_catalog`/`pg_stat_user_tables`만
조회**하며, 어떤 데이터도 수정하지 않는다. 두 단계로 나뉘어 있다 — **1단계를
먼저 실행**하고, 그 결과에서 `app_users`/`shared_messages`가 "존재"로 나올
때만 2단계를 실행할 것(존재하지 않는 테이블을 직접 참조하는 일반 SQL은
그 자체로 오류가 나므로, 순서를 지켜야 한다).

### 1단계 — 구조 진단 (테이블/함수/정책/Realtime/REPLICA IDENTITY)

```sql
-- ============================================================
-- cacao-macro 운영 준비 상태 진단 (1단계 — 구조)
-- 읽기 전용: information_schema/pg_catalog/pg_stat_user_tables만 조회한다.
-- insert/update/delete/drop 없음 — 아무 데이터도 변경하지 않는다.
-- ============================================================

-- 1) 필수 테이블 존재 여부 + 대략 행 수 + RLS 활성화 여부
with expected_tables(table_name) as (
  values ('app_users'), ('messages'), ('message_history'),
         ('shared_messages'), ('shared_message_history'), ('admin_audit_logs')
)
select
  '1.테이블' as category,
  e.table_name as item,
  case when c.relname is null then '❌ 없음' else '✅ 존재' end as status,
  case when c.relname is null then ''
       else format('행수≈%s / RLS %s',
                    coalesce(s.n_live_tup, 0),
                    case when c.relrowsecurity then '켜짐' else '꺼짐' end)
  end as detail
from expected_tables e
left join pg_catalog.pg_class c
  on c.relname = e.table_name and c.relnamespace = 'public'::regnamespace
left join pg_stat_user_tables s
  on s.relname = e.table_name and s.schemaname = 'public'

union all

-- 2) RPC 17개 존재 여부
select
  '2.함수(RPC)' as category,
  e.function_name as item,
  case when count(p.proname) > 0 then '✅ 존재' else '❌ 없음' end as status,
  case when count(p.proname) > 0 then format('%s개 오버로드', count(p.proname)) else '' end as detail
from (values
  ('fn_handle_new_auth_user'), ('fn_log_message_history'), ('fn_is_approved'),
  ('fn_current_role'), ('fn_is_admin'), ('fn_can_edit'), ('fn_restore_message'),
  ('fn_approved_admin_count'), ('fn_assert_not_last_admin'), ('admin_list_users'),
  ('admin_approve_user'), ('admin_block_user'), ('admin_unblock_user'),
  ('admin_update_user_role'), ('admin_list_audit_logs'),
  ('update_shared_message'), ('force_update_shared_message')
) as e(function_name)
left join pg_catalog.pg_proc p
  on p.proname = e.function_name and p.pronamespace = 'public'::regnamespace
group by e.function_name

union all

-- 3) RLS 정책 10개 존재 여부
select
  '3.RLS정책' as category,
  e.table_name || ' / ' || e.policy_name as item,
  case when pol.policyname is not null then '✅ 존재' else '❌ 없음' end as status,
  '' as detail
from (values
  ('app_users', 'app_users_select_self'),
  ('app_users', 'app_users_select_all_if_admin'),
  ('app_users', 'app_users_update_admin_only'),
  ('messages', 'messages_select_approved'),
  ('messages', 'messages_insert_editor'),
  ('messages', 'messages_update_editor'),
  ('message_history', 'message_history_select_approved'),
  ('admin_audit_logs', 'admin_audit_logs_select_admin_only'),
  ('shared_messages', 'shared_messages_select_approved'),
  ('shared_message_history', 'shared_message_history_select_approved')
) as e(table_name, policy_name)
left join pg_catalog.pg_policies pol
  on pol.schemaname = 'public'
 and pol.tablename = e.table_name
 and pol.policyname = e.policy_name

union all

-- 4) Realtime publication에 shared_messages 등록 여부
select
  '4.Realtime' as category,
  'shared_messages → supabase_realtime' as item,
  case when exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'shared_messages'
  ) then '✅ 등록됨' else '❌ 미등록' end as status,
  '' as detail

union all

-- 5) shared_messages의 REPLICA IDENTITY
select
  '5.REPLICA IDENTITY' as category,
  'shared_messages' as item,
  case
    when not exists (
      select 1 from pg_catalog.pg_class
      where relname = 'shared_messages' and relnamespace = 'public'::regnamespace
    ) then '❌ 테이블 없음'
    when (
      select relreplident from pg_catalog.pg_class
      where relname = 'shared_messages' and relnamespace = 'public'::regnamespace
    ) = 'f' then '✅ FULL'
    else '⚠ FULL 아님'
  end as status,
  '' as detail

order by category, item;
```

### 2단계 — 데이터 수준 진단 (1단계에서 `app_users`가 "✅ 존재"로 나온 경우에만 실행)

```sql
-- ============================================================
-- cacao-macro 운영 준비 상태 진단 (2단계 — 데이터)
-- 읽기 전용: select만 사용. 반드시 1단계에서 app_users가 존재로 확인된
-- 뒤에만 실행할 것(존재하지 않는 테이블 참조 시 이 쿼리 자체가 오류남).
-- ============================================================
select '6.Auth' as category, '전체 auth.users 수' as item,
       (select count(*)::text from auth.users) as status, '' as detail
union all
select '6.Auth', 'app_users 승인(approved) 사용자 존재',
       case when exists (select 1 from public.app_users where status = 'approved')
            then '✅ 존재' else '❌ 없음' end, ''
union all
select '6.Auth', '승인된 admin(최초 관리자) 존재',
       case when exists (select 1 from public.app_users where status = 'approved' and role = 'admin')
            then '✅ 존재' else '❌ 없음' end, ''
union all
select '6.Auth', 'pending 상태 사용자 수',
       (select count(*)::text from public.app_users where status = 'pending'), ''
order by category, item;
```

두 쿼리 모두 실행 후 "Results" 탭에 표(카테고리/항목/상태/상세) 형태로 한
번에 표시된다. 결과를 그대로 캡처하거나 표를 복사해 전달해 주면 11절 판정을
대신 정리해 줄 수 있다.

---

## 11. 결과에 따른 판정과 다음 작업

| 판정 | 기준 | 다음 작업 |
|---|---|---|
| **A. 운영 준비 완료** | 1단계의 테이블 6개·함수 17개·정책 10개·Realtime 등록·REPLICA IDENTITY(FULL) 전부 ✅, 9절 Google Provider 활성화+Redirect URL 등록 완료, 2단계에서 승인된 admin 1명 이상 존재 | 곧바로 `.env`(PC)/Vercel 환경변수 전환 절차로 진행 가능. 전환 전 소규모 파일럿 로그인 테스트 권장 |
| **B. 스키마만 미적용** | 1단계 테이블 6개 전부 ❌(즉 `public` 스키마가 사실상 비어 있음) | `docs/sql/phase2b_schema.sql` → `phase4_admin_rpc.sql` → `shared_messages_realtime.sql` 순서로 SQL Editor에 수동 적용(각 파일 헤더의 사전조건 재확인). 적용 후 이 문서의 1~7절을 다시 점검 |
| **C. Auth만 미설정** | 1단계 구조는 전부 ✅인데 9절 Google Provider 비활성화이거나, 2단계에서 승인된 admin이 0명 | 9절 절차대로 Google Provider 활성화 + Redirect URL 등록, 이후 `docs/sql/phase2b_schema.sql` 8절 방식으로 최초 관리자 1명을 SQL Editor에서 수동 UPDATE(이 점검 문서 범위 밖 — 별도로 검토 후 진행) |
| **D. 일부 구조 누락** | 1단계에서 테이블/함수/정책/Realtime 중 일부만 ✅, 일부는 ❌(부분 적용 상태) | 어느 파일의 어느 객체가 빠졌는지 1단계 결과를 3개 SQL 파일과 대조 → 누락된 원본 파일 전체를 다시 실행(3개 파일 모두 idempotent라 재실행해도 기존 데이터가 지워지지 않음) → 재점검 |
| **E. 사용 중인 다른 프로젝트** | 위 6개 테이블 이외의 낯선 테이블이 다수 존재하거나, `app_users`에 이 프로젝트와 무관해 보이는 대량의 실사용자 데이터가 이미 있음(테스트 계정 패턴과 다름) | **아무 작업도 진행하지 말고 중단** — 이 프로젝트가 다른 목적으로 이미 쓰이고 있을 가능성이 있으므로, 프로젝트 소유자/조직 내 다른 담당자에게 먼저 확인. 착오로 다른 프로젝트를 열었을 가능성도 재확인(프로젝트 ref 재대조) |

이 표의 판정 근거(각 절 확인 결과)를 전달해 주면, 그에 맞는 다음 작업을
구체적으로 이어서 진행할 수 있다.
