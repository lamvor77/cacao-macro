# Phase 2B — SQL 적용 및 검증 계획

관련 파일: [docs/sql/phase2b_schema.sql](sql/phase2b_schema.sql)(적용할 스키마),
[docs/sql/phase2b_rls_tests.sql](sql/phase2b_rls_tests.sql)(검증 스크립트).
둘 다 아직 실제 Supabase 프로젝트에 적용되지 않은 **초안**이다.

## 실행 라운드 2 — 실제 테스트 프로젝트 실행 중 발견된 버그와 수정 (트랜잭션 구조)

첫 실제 실행에서 `viewer는 messages 12건을 볼 수 있어야 함` ASSERT가 실패했고, 실패 후
확인해보니 `app_users`/`messages`/`message_history`가 전부 0건이었다(0번 준비 단계에서
만든 계정/메시지까지 사라짐). 코드를 처음부터 다시 추적해 원인을 확정했다.

**원인**: Postgres는 여러 SQL문을 한 Query 메시지로 받으면, 그 안에 명시적
BEGIN/COMMIT이 없는 구간을 암묵적으로 하나의 트랜잭션으로 묶는다. 이전 버전의
`phase2b_rls_tests.sql`은 -1번(안전장치)·0번(계정/메시지 생성)·1번(테이블 확인)이
전부 `BEGIN` 없이 "맨 statement"로 실행되었고, 이 셋은 전부 이 암묵적 트랜잭션
안에 있었다. 그 뒤 2-1번 테스트의 `BEGIN;`은 **이미 트랜잭션이 열려 있어서
새 트랜잭션을 시작하지 못하고 그냥 경고만 내며 통과**했다(Postgres에서 이미
열린 트랜잭션 안에서 BEGIN을 또 실행하면 아무 효과가 없다 — 진짜 서브트랜잭션이
생기지 않는다). 그래서 2-1번 끝의 `ROLLBACK;`이 2-1번 자신의 변경뿐 아니라
스크립트 맨 처음(암묵적 트랜잭션이 시작된 지점)까지 전부 취소해버렸다 — 즉 0번의
계정 생성과 메시지 시드까지 통째로 사라진 것이다. 2-3번(viewer)이 실행될 때는
이미 데이터가 하나도 없는 새 트랜잭션 상태였으므로 `messages` 12건 assert가 실패했다.

**수정**: 파일의 모든 섹션(안전장치, 준비, 테이블 확인 포함)을 예외 없이 명시적
`begin; ... commit;` 또는 `begin; ... rollback;`으로 감쌌다. 이렇게 하면 각 섹션이
독립된 트랜잭션이 되어, 한 섹션의 ROLLBACK이 다른 섹션에 영향을 주지 않는다. 이
구조가 실제로 올바른지 파이썬 스크립트로 BEGIN/COMMIT/ROLLBACK 짝을 전부 순서대로
추적하는 정적 검증을 만들어 돌렸다(트레일링 주석 포함 34개 트랜잭션 제어문 전부
1:1로 짝이 맞고, 짝 사이에 다른 BEGIN이 끼어들거나 파일 끝에 열린 트랜잭션이 남지
않음을 확인).

## 실행 라운드 2 — 잘못된 예외 테스트 패턴 수정

다음 패턴이 여러 곳에 있었다:

```sql
do $$
begin
    update ...;
    raise exception 'FAIL ...';
exception
    when others then
        raise notice 'PASS ...';
end $$;
```

문제: RLS의 USING절이 막는 UPDATE는 에러 없이 "0 rows affected"로 조용히 끝난다.
즉 위 패턴은 UPDATE가 (a) 정말로 차단되어 0건이든, (b) 버그로 실제로 수정되어
버렸든 상관없이 항상 그 다음 줄의 `raise exception 'FAIL ...'`이 실행되고, 그걸
같은 블록의 `when others`가 그대로 삼켜 PASS로 출력한다 — **차단 여부와 무관하게
항상 PASS가 나오는 무의미한 테스트였다.**

수정한 두 가지 패턴:

1. **RLS가 조용히 0건으로 막는 경우**(대부분의 UPDATE 차단 테스트): `GET DIAGNOSTICS
   v_row_count = ROW_COUNT;`로 실제 영향받은 행 수를 확인하고 `assert v_row_count = 0`
   으로 검증한다. 허용되어야 하는 경우(editor/admin의 정상 수정)는 반대로
   `assert v_row_count = 1`로 실제 반영을 확인한다.
2. **Postgres가 진짜 예외를 던지는 경우**(WITH CHECK 위반으로 인한 신원 사칭 차단,
   `fn_restore_message()` 내부의 `RAISE EXCEPTION`): 우리가 만든 FAIL 예외를 같은
   `exception` 절이 되잡는 문제를 피하기 위해, 실제 작업을 **중첩된**
   `begin ... exception when others then v_blocked := true; end;` 블록으로 감싸
   boolean 플래그에 결과를 기록하고, 그 플래그를 **그 바깥에서** `assert`한다.
   assert 실패는 더 이상 같은 exception 절에 잡히지 않고 진짜 에러로 전파된다.

## 실행 라운드 2 — 역할별 검증 행렬 확장

기존에는 일부 역할(pending 조회, blocked 조회, viewer 조회+수정, editor 수정+이력,
admin 사용자관리)만 흩어져 테스트되고 있었다. 이번에 2번 섹션을 "역할별 매트릭스"로
재구성해 pending/blocked/viewer/editor/admin **다섯 역할 모두**에 대해 (a) messages
조회 가능 여부, (b) messages 수정 가능 여부, (c) app_users 조회 범위, (d) app_users
관리(수정) 가능 여부를 빠짐없이 확인한다(전부 ROLLBACK으로 끝나 부작용 없음). 복원
RPC 권한은 기존 6번 섹션(6-1~6-5)이 다섯 역할 모두를 이미 다루고 있어 그대로 유지했다.

## 실행 라운드 2 — 재실행 안전장치 추가

`docs/sql/phase2b_rls_tests_cleanup.sql`을 새로 만들었다 — `phase2b_rls_tests.sql`이
중간에 실패해 9번(정리) 블록까지 도달하지 못하면 준비 데이터가 남아있게 되고, -1번
안전장치가 "기존 데이터 있음"으로 판단해 재실행을 거부한다. 이 독립 스크립트는
`app_users`에 `test-%@example.com` 이외의 이메일이 하나라도 있으면(실제 데이터가
섞여 있을 가능성) 아무것도 지우지 않고 중단하는 안전장치를 갖춘 채로 잔여 테스트
데이터만 지운다. 운영 프로젝트 보호(-1번 안전장치, 9번 정리 전 확인)는 이번에도
약화하지 않고 오히려 9번에도 동일한 "테스트 계정만 있는지" 확인을 추가했다.

## 적용 방법 (승인 후)

1. **반드시 테스트용/스테이징 Supabase 프로젝트**에서 먼저 적용한다 (운영 프로젝트 아님).
2. Supabase 대시보드 → SQL Editor에서 `phase2b_schema.sql` 전체 실행.
3. `phase2b_schema.sql` 8절 안내대로, 실제 관리자 이메일로 최초 admin 1명을 수동 지정
   (SQL 파일에는 예시 이메일만 있고 실제 값은 넣지 않았다 — 직접 실행해야 함).
4. `phase2b_rls_tests.sql` 전체 실행 → SQL Editor 결과 패널의 `NOTICE` 메시지에서
   `PASS: ...` 가 전부 출력되는지 확인 (`FAIL`이 하나라도 뜨면 예외가 발생해 해당 트랜잭션이
   중단되고 스크립트가 그 지점에서 멈춘다).
5. 테스트 스크립트 9번 블록이 테스트 계정/데이터를 자동 정리한다 (실행 후 남는 테스트
   데이터 없음).
6. 스테이징에서 전부 통과하면, 동일한 `phase2b_schema.sql`을 운영 프로젝트에 적용한다
   (`phase2b_rls_tests.sql`은 운영 프로젝트에서 실행하지 않는다 — 가짜 계정을 만들기 때문).

## 요청하신 7개 검증 항목 ↔ 실제 테스트 매핑

| # | 요청 항목 | 검증 방법 (phase2b_rls_tests.sql 블록) | 기대 결과 |
|---|---|---|---|
| 1 | 테이블, 함수, 트리거, RLS 적용 | 블록 1 — `information_schema`/`pg_class`/`pg_trigger` 조회 | 3개 테이블, 3개 RLS 활성화, 2개 트리거 확인 |
| 2 | pending/viewer/editor/admin/blocked 권한별 RLS 검증 | 블록 0(계정 5종 생성) + 블록 2 전체(역할별 세션 흉내) | 아래 "역할별 기대 동작 표" 참고 |
| 3 | 승인 전 사용자의 접근 차단 검증 | 블록 2 "2-1. pending 사용자" | pending은 messages/history 0건, UPDATE 시도 시 예외 |
| 4 | editor의 메시지 즉시 수정 및 이력 자동 생성 검증 | 블록 4 | UPDATE 즉시 반영(재확인 절차 없음) + `message_history`에 자동 1건 추가 + `updated_by` 위조 시도 차단 |
| 5 | admin의 사용자 승인·차단·권한 변경 검증 | 블록 5 | 승인/역할변경/차단/재활성화 모두 admin만 가능, editor는 전부 차단 |
| 6 | 메시지 이전 버전 복원 및 복원 이력 생성 검증 | 블록 6 | `fn_restore_message()` 호출 시 과거 텍스트로 되돌아가고, `source='restore'`인 새 이력이 자동 생성됨. viewer는 호출 자체가 거부됨 |
| 7 | (추가) 테스트 데이터 정리 | 블록 9 | 테스트 계정/메시지가 흔적 없이 삭제됨 |

## 역할별 기대 동작 표 (요약)

| 역할/상태 | messages 읽기 | messages 쓰기 | app_users 읽기 | app_users 쓰기(승인/역할/차단) | fn_restore_message 실행 |
|---|---|---|---|---|---|
| pending | ✗ (0건) | ✗ | 자기 자신만 | ✗ | ✗ |
| blocked | ✗ (0건) | ✗ | 자기 자신만 | ✗ | ✗ |
| viewer(approved) | ✓ | ✗ | 자기 자신만 | ✗ | ✗ |
| editor(approved) | ✓ | ✓ (본인 명의로만) | 자기 자신만 | ✗ | ✓ |
| admin(approved) | ✓ | ✓ (본인 명의로만) | 전체 | ✓ | ✓ |

## SQL 초안 정적 검토에서 발견/수정한 사항 (1차: UUID·삭제순서, 2차: 정적 검토 10항목)

### 1차 검토(초안 작성 직후)에서 잡은 버그 2건

1. **잘못된 UUID 리터럴**: 테스트 계정 식별자로 `...0000p1`, `...0000v1` 같은 값을 썼는데
   UUID는 16진수(0-9, a-f)만 허용되어 `p`, `v` 등은 애초에 INSERT 시점에 문법 오류가 난다.
   `...0001` ~ `...0005` 형태의 순수 16진수로 교체했다.
2. **정리(cleanup) 순서 오류**: `messages.updated_by` / `message_history.updated_by`가
   `app_users(id)`를 참조하고 `ON DELETE CASCADE`가 아니므로, `auth.users`(→`app_users` cascade)를
   먼저 지우면 외래키 위반으로 실패한다. `message_history` → `messages` → `auth.users` 순서로
   고쳤다.

### 2차 정적 검토(10개 체크리스트) 결과

| # | 점검 항목 | 결과 | 조치 |
|---|---|---|---|
| 1 | `phase2b_schema.sql`이 두 번 실행돼도 실패하지 않는지 | ❌ 원래는 실패했음 | enum은 `duplicate_object` 예외를 잡는 DO 블록으로, 테이블/인덱스는 `IF NOT EXISTS`로, 트리거/정책은 `DROP ... IF EXISTS` 후 재생성으로 전부 재실행 안전하게 수정 |
| 2 | enum/trigger/function/policy 이름 충돌 처리 | ❌ 없었음 | 위와 동일 조치 + enum 타입을 `public.` 스키마로 명시 한정 |
| 3 | SECURITY DEFINER 함수에 search_path 명시 | ✅ 이미 충족 | 7개 SECURITY DEFINER 함수 모두 `set search_path = public` 확인. 변경 없음 |
| 4 | RLS 헬퍼 함수가 재귀를 일으키지 않는지 | ✅ 이미 충족(설계상 안전) | SECURITY DEFINER 함수는 함수 소유자(테이블 소유자) 권한으로 실행되어 자기 테이블의 RLS를 우회하므로 재귀가 발생하지 않음. 이 전제(함수 소유자를 바꾸지 말 것)를 스키마에 주석으로 명시 |
| 5 | `fn_restore_message()`가 함수 내부에서도 권한을 검증하는지 | ✅ **반영 완료(admin 전용으로 확정)** | 내부 가드를 `fn_can_edit()` → `fn_is_admin()`으로 교체. 일반 메시지 수정은 여전히 editor도 가능, "이력에서 골라 복원"만 admin 전용 |
| 6 | `message_history`가 클라이언트에 의해 직접 INSERT/UPDATE/DELETE되지 않는지 | ✅ 이미 충족 | RLS는 켜져 있고 SELECT 정책만 존재 — Postgres RLS는 정책이 없는 명령을 기본 전면 거부하므로 별도 조치 불필요. 이유를 주석으로 명시 |
| 7 | `messages.updated_by`가 INSERT/UPDATE 모두 `auth.uid()`로 강제되는지 | ✅ 이미 충족 | `messages_insert_editor`, `messages_update_editor` 정책의 `with check`에 둘 다 포함됨 (테스트 4-2가 검증) |
| 8 | pending/blocked가 RPC로 RLS를 우회할 수 없는지 | ❌ **실제 버그 발견 — 수정함** | 아래 "심각도 높은 버그" 참고 |
| 9 | 테스트 스크립트가 운영 환경에서 실행되지 않도록 방어 | ❌ 없었음 | `phase2b_rls_tests.sql` 최상단에 이중 안전장치 추가 (아래 참고) |
| 10 | 테스트 종료 후 auth.users 등 관련 데이터가 모두 정리되는지 | ✅ 1차 검토에서 이미 수정됨 | 삭제 순서 수정으로 완전 정리됨 (변경 없음) |

### 심각도 높은 버그 (8번) — pending/blocked가 RPC로 RLS를 우회할 수 있었음

`fn_current_role()`은 미승인/차단 사용자에 대해 **NULL**을 반환한다(해당 상태의 행이
`status='approved'` 조건에 안 걸려서 0건 조회됨 → 스칼라 서브쿼리는 NULL). 기존
`fn_can_edit()`은 `select fn_current_role() in ('editor','admin')`이었는데, `NULL in (...)`은
SQL에서 `NULL`을 반환한다. 문제는 `fn_restore_message()` 내부 가드가
`if not fn_can_edit() then raise exception ... end if;` 형태였다는 것 — plpgsql의 `IF` 문은
조건이 **NULL이면 FALSE 취급이 아니라 그냥 통과(THEN 블록 실행 안 함)**한다. 즉
`fn_can_edit()`이 NULL을 반환하는 pending/blocked 사용자는 `not NULL` = NULL이 되어
예외가 발생하지 않고 조용히 함수 나머지 로직(복원 실행)까지 도달할 수 있었다 —
`fn_restore_message`는 SECURITY DEFINER라 RLS로는 못 막고 이 IF 가드가 유일한 방어선이었으므로,
이 버그는 pending/blocked 사용자가 메시지를 복원(=사실상 수정)할 수 있는 실제 권한 우회였다.

**수정**: `fn_is_admin()`/`fn_can_edit()` 정의 자체에 `coalesce(..., false)`를 추가해 두
함수가 항상 TRUE/FALSE만 반환하고 NULL을 반환할 수 없게 만들었다 (호출하는 모든 곳이
자동으로 안전해짐 — 개별 호출부를 각각 고치는 대신 근본 원인을 막음).
`phase2b_rls_tests.sql`에 6-3/6-4 블록으로 pending·blocked의 `fn_restore_message` 호출이
실제로 차단되는지 확인하는 회귀 테스트를 추가했다.

### 안전장치 (9번) — `phase2b_rls_tests.sql`에 이중 방어 추가

1. **명시적 플래그**: 파일 맨 위 `SET app.confirm_test_run = 'RUN_ON_TEST_PROJECT_ONLY';`가
   기본적으로 주석 처리되어 있다. 주석을 해제하지 않고 그대로 실행하면 첫 DO 블록에서
   즉시 예외가 발생해 전체 스크립트가 멈춘다 — 파일을 있는 그대로 실행하면 항상 안전한 쪽으로
   실패한다(fail-closed).
2. **데이터 존재 여부 확인**: 플래그를 해제했더라도 `app_users`나 `messages`에 기존 데이터가
   있으면(운영 프로젝트이거나 정리 안 된 이전 테스트) 중단한다.

### 결정 완료 — `fn_restore_message()`의 권한 범위 (5번)

**확정: 복원은 admin 전용.** 일반 메시지 수정(messages 테이블 직접 UPDATE/INSERT)은
approved editor와 admin 모두 가능하게 유지하되, 과거 이력에서 특정 버전을 골라 현재 값으로
되돌리는 `fn_restore_message()`는 approved admin만 호출할 수 있도록 제한했다.

반영 내용:

1. 내부 권한 검사를 `fn_can_edit()` → `fn_is_admin()`으로 교체
2. `fn_is_admin()`은 `coalesce(..., false)`로 항상 TRUE/FALSE만 반환 (이미 반영됨, 8번 버그 수정과 동일한 방식)
3. pending/viewer/editor/blocked의 호출은 모두 거부 (테스트 6-1~6-4)
4. approved admin만 성공 (테스트 6-5)
5. `updated_by`는 항상 호출자 `auth.uid()`로 기록 (테스트 6-5에서 admin 계정 일치 확인)
6. `source='restore'`로 식별 가능하게 기록 (변경 없음, 기존대로 유지)
7. 트리거를 통해 `message_history`에 자동 기록 (변경 없음)
8. `SECURITY DEFINER` + 고정 `search_path` 유지 (변경 없음)
9. `EXECUTE`는 `authenticated`에 부여하되 함수 내부 admin 검사가 최종 방어선 (변경 없음, 구조 동일)
10. `anon`에는 `EXECUTE` 권한 없음 — `revoke execute ... from anon;`을 명시적으로 추가

테스트 스크립트(`phase2b_rls_tests.sql`) 6번 섹션을 6-1(pending 실패)~6-5(admin 성공 + 값
변경/`updated_by`/이력/복원값 일치 확인)로 재구성했다.

## 아직 실행하지 않은 이유

이 세션에는 실제 Supabase 프로젝트 URL/키가 설정되어 있지 않다(`SUPABASE_ENABLED=False`,
Phase 2A 로컬 서비스 계층만 존재). SQL을 실제로 적용하려면 다음 중 하나가 필요하다:

- 사용자가 Supabase 대시보드 SQL Editor에서 직접 두 파일을 실행하고 결과를 알려주는 방법, 또는
- 사용자가 프로젝트 연결 정보(DB 연결 문자열 등)를 제공하면 이쪽에서 직접 실행하는 방법

어느 쪽으로 진행할지 확인해달라.
