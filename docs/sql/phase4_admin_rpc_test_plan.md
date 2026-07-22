# Phase 4-1 관리자 RPC 테스트 계획

이 문서는 `docs/sql/phase4_admin_rpc.sql`을 실제 Supabase 테스트 프로젝트에 적용한 뒤
수동(또는 향후 자동화된) SQL Editor 테스트로 검증할 항목을 정리한다. **이 문서 자체는
테스트를 실행하지 않는다** — 실제 실행은 별도로, 반드시 운영 프로젝트가 아닌 테스트
프로젝트에서 수행한다. `docs/sql/phase2b_rls_tests.sql`의 관례(explicit BEGIN/ROLLBACK,
GET DIAGNOSTICS로 실제 결과 확인)를 따른다.

## A. 준비

테스트 프로젝트에 다음 계정을 준비한다(실제 Google 로그인 1회씩 필요 — auth.users에
행이 생겨야 app_users 트리거가 동작한다).

| 별칭 | role | status |
|---|---|---|
| admin1 | admin | approved |
| admin2 | admin | approved |
| editor1 | editor | approved |
| viewer1 | viewer | approved |
| pending1 | viewer(기본값) | pending |
| blocked1 | editor | blocked |

admin1/admin2 두 명을 반드시 준비한다 — "마지막 admin 보호"와 "admin 2명일 때 다른
admin 차단" 시나리오 모두 검증해야 하기 때문이다.

## B. admin_list_users

1. admin1로 `select * from admin_list_users()` → 성공, 전체 사용자 반환.
2. editor1로 동일 호출 → `ADMIN_REQUIRED` 오류.
3. 미로그인(anon key만, 세션 없음)으로 호출 → `ADMIN_REQUIRED` 오류(fn_is_admin()이
   auth.uid() NULL이라 false).
4. `p_status='approved'` → approved 사용자만 반환되는지.
5. `p_role='admin'` → admin만 반환되는지.
6. `p_search='관리자이메일일부'` → email/display_name 부분일치로 필터링되는지.
7. `p_limit=1, p_offset=0`과 `p_limit=1, p_offset=1`로 페이지네이션이 겹치지 않고
   순서(created_at desc)가 안정적인지.
8. `p_status='not_a_real_status'` → `INVALID_STATUS` 오류.
9. `p_role='not_a_real_role'` → `INVALID_ROLE` 오류.
10. `p_limit=0`, `p_limit=9999`, `p_offset=-1` → 오류 없이 1~200/0 이상으로 clamp되어
    실행되는지(SQL 레벨은 방어적으로 clamp, 엄격 거부는 Python 쪽 책임).

## C. admin_approve_user

1. admin1이 pending1 승인(`p_role='editor'`) → status='approved', role='editor',
   approved_at이 채워짐, `admin_audit_logs`에 `user_approved` 1건 생성(old_status='pending',
   new_status='approved').
2. `p_role='not_a_real_role'` → `INVALID_ROLE` 오류, 아무 것도 바뀌지 않았는지 확인.
3. 존재하지 않는 UUID → `TARGET_USER_NOT_FOUND`.
4. blocked1 대상으로 호출 → `TARGET_BLOCKED` 오류(정책: blocked→approved는
   admin_unblock_user 전용).
5. 이미 approved+동일 role인 사용자 재승인 → 값 변경 없음, `admin_audit_logs`에 새
   행이 **추가되지 않음**(no-op 정책 확인).
6. approved_at 정책: 이미 approved_at이 있던 사용자를 role만 바꿔 다시
   approve해도 approved_at이 갱신되지 않고 유지되는지(`coalesce(approved_at, now())`).

## D. admin_block_user

1. admin1이 editor1 차단 → status='blocked', `user_blocked` 로그 1건.
2. admin1이 자기 자신(admin1) 차단 시도 → `SELF_BLOCK_FORBIDDEN`.
3. admin이 1명뿐인 상태를 만들고(테스트용으로 admin2를 임시로 editor로 낮춰둔 뒤)
   그 마지막 admin을 차단 시도 → `LAST_ADMIN_PROTECTED`. 테스트 후 admin2를 다시
   admin으로 복원.
4. admin1, admin2 둘 다 approved admin인 상태에서 admin1이 admin2를 차단 →
   성공(마지막 admin이 아니므로 허용). 이후 admin2를 다시 unblock+approve로 복원.
5. 이미 blocked인 사용자 재차단 → no-op, 로그 미추가.

## E. admin_unblock_user

1. blocked1을 `p_restore_status='approved'`로 해제 → status='approved',
   `user_unblocked` 로그(old_status='blocked', new_status='approved').
2. 다른 blocked 사용자를 `p_restore_status='pending'`으로 해제 → status='pending'.
3. blocked가 아닌 사용자(예: editor1, status='approved')에 호출 → `USER_NOT_BLOCKED`
   오류(정책: 조용한 no-op이 아니라 명시적 오류로 통일).
4. `p_restore_status='blocked'`(허용되지 않는 값) → `INVALID_STATUS`.

## F. admin_update_user_role

1. viewer1을 editor로 승격 → 성공, `user_role_changed` 로그.
2. editor1을 admin으로 승격 → 성공(대상이 approved라면 즉시 fn_is_admin() true가 됨).
3. pending1의 role을 admin으로 바꾼 뒤(승인 전) 해당 계정으로 로그인해
   `AppUserProfile.is_admin`이 여전히 False인지 확인(status가 approved가 아니므로) —
   role 변경과 권한 활성화가 분리되어 있음을 검증하는 핵심 케이스.
4. admin1이 자기 자신을 editor로 강등 시도 → `SELF_DEMOTION_FORBIDDEN`
   (다른 admin이 있어도 항상 거부되는지 확인 — "마지막 admin"과 무관한 규칙).
5. 마지막 approved admin(다른 admin을 전부 editor로 낮춘 상태)을 **다른 admin
   계정으로** 강등 시도 → `LAST_ADMIN_PROTECTED`.
6. blocked 사용자의 role만 변경(status는 그대로 blocked 유지) → 허용되는지,
   status가 바뀌지 않는지 확인(정책 문서화된 대로).

## G. 감사로그(admin_audit_logs)

1. B~F의 각 성공 케이스마다 정확히 1건씩 로그가 생성되는지, action 값이
   의도한 값과 일치하는지.
2. no-op(이미 원하는 상태)로 끝난 호출은 로그가 **생성되지 않는지**.
3. `actor_user_id`가 항상 호출한 계정의 `auth.uid()`와 일치하는지(테스트 중
   RPC 파라미터로 actor를 조작하려는 시도가 애초에 불가능함 — 함수 시그니처에
   actor 파라미터 자체가 없음).
4. old_role/old_status가 실제 변경 "전" 값과 정확히 일치하는지(변경 후 값을
   실수로 넣지 않았는지) — UPDATE 전에 캡처한 `v_before`를 사용하는지 코드 리뷰로도 확인.
5. editor1/viewer1 계정으로 `select * from admin_audit_logs` 직접 조회 →
   RLS에 의해 0행 반환(오류가 아니라 빈 결과 — SELECT 정책이 `using
   (fn_is_admin())`이므로).
6. 아무 계정으로나(admin 포함) `insert into admin_audit_logs (...) values (...)` 직접
   시도 → 거부됨(INSERT 정책이 하나도 없으므로 RLS가 전면 차단).
7. `update admin_audit_logs set reason = '...' where id = ...` /
   `delete from admin_audit_logs where id = ...` 직접 시도(admin 포함) → 거부됨.

## H. 동시성

1. 두 개의 별도 세션(admin1, admin2 각각의 클라이언트)에서 **동시에** 서로를
   차단하려고 시도(`admin_block_user`를 거의 동시에 호출) — 두 트랜잭션이
   `pg_advisory_xact_lock(hashtext('app_users_admin_guard'))`로 직렬화되므로,
   먼저 잠금을 얻은 트랜잭션이 완료된 후에야 두 번째가 진행된다. 최종적으로
   승인된 admin이 0명이 되는 결과가 나오지 않는지 확인(적어도 한쪽은
   `LAST_ADMIN_PROTECTED`로 거부되어야 함).
2. admin이 2명일 때, 한 세션은 `admin_block_user(admin2)`를, 다른 세션은
   `admin_update_user_role(admin2, 'editor')`를 거의 동시에 호출 — 두 함수가
   동일한 advisory lock 이름을 사용하므로 직렬화되어 순차 처리되는지, 최종
   상태가 일관적인지(레이스로 인한 이상 상태가 생기지 않는지) 확인.
3. 위 시나리오 실행 후 `select fn_approved_admin_count()`(admin 세션에서, 또는
   `admin_list_users(p_role:='admin', p_status:='approved')`의 행 수로) 최소
   1 이상이 항상 유지되는지 최종 확인.

## 회귀 확인

- `docs/sql/phase2b_rls_tests.sql`의 기존 RLS 테스트가 이번 변경 이후에도 그대로
  통과하는지(특히 messages_insert_editor/messages_update_editor는 이번 파일에서
  전혀 건드리지 않았으므로 영향이 없어야 한다).
- `app_users_update_admin_only` 정책 제거 후, admin 계정으로 직접
  `update app_users set role='admin' where id = '...'`을 시도하면 **거부되는지**
  (더 이상 어떤 UPDATE 정책도 없으므로 RLS가 전면 차단해야 한다) — 이것이 이번
  변경의 핵심 보안 목표이므로 반드시 확인한다.
