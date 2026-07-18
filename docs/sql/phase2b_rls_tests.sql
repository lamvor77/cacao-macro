-- Phase 2B RLS/이력/복원 검증 스크립트
--
-- 실행 전제: docs/sql/phase2b_schema.sql 이 이미 적용된 "테스트용" Supabase
-- 프로젝트(또는 운영과 분리된 스키마)에서 실행한다. 운영 데이터가 있는
-- 프로젝트에서 실행하지 않는다 — 아래 스크립트는 테스트용 가짜 계정을
-- auth.users에 직접 INSERT한다.
--
-- 실행 방법:
--   1) 지금 연결된 프로젝트가 테스트/스테이징 프로젝트가 맞는지 다시 한 번 확인한다.
--   2) 아래 -1번 섹션의 `SET app.confirm_test_run = ...;` 줄 주석을 해제한다
--      (이걸 하지 않으면 스크립트가 맨 앞에서 즉시 멈춘다 — 의도된 기본 동작).
--   3) Supabase 대시보드의 SQL Editor에서 전체를 한 번에 실행한다.
-- 각 블록은 RAISE NOTICE로 'PASS: ...' 또는 예외(FAIL)를 출력한다 — 결과
-- 패널의 메시지를 확인한다. 테스트 후 9번 블록으로 데이터를 정리한다.
--
-- 중간에 실패해서 다시 실행해야 한다면, 먼저 phase2b_rls_tests_cleanup.sql을
-- 실행해 잔여 테스트 데이터를 지운 뒤 이 파일을 처음부터 다시 실행한다
-- (그렇지 않으면 -1번 안전장치가 "기존 데이터 있음"으로 판단해 즉시 중단한다).
--
-- 사용 기법: request.jwt.claims 세션 변수로 auth.uid()를 흉내낸다
-- (Supabase auth.uid()는 이 세션 변수의 'sub' 클레임을 읽는 SQL 함수이므로,
--  실제 로그인 없이도 SQL Editor에서 특정 사용자인 것처럼 RLS를 검증할 수 있다).
--
-- ★★★ 트랜잭션 구조에 대한 중요한 주의사항 (실제로 발생했던 버그의 원인) ★★★
-- Postgres는 여러 SQL문이 한 번에(하나의 Query 메시지로) 전송되면, 그 안에 명시적
-- BEGIN/COMMIT/ROLLBACK이 없는 한 전체를 암묵적으로 하나의 트랜잭션으로 묶는다.
-- 그리고 이미 트랜잭션이 열려 있는 상태에서 다시 BEGIN을 실행하면 새 트랜잭션이
-- 시작되는 게 아니라 "there is already a transaction in progress" 경고만 내고
-- 기존 트랜잭션을 그대로 이어간다 — 즉 그 뒤의 ROLLBACK은 그 블록만 취소하는 게
-- 아니라 스크립트 맨 처음(첫 암묵적 트랜잭션 시작 지점)까지 전부 취소해버린다.
-- 이 문제를 피하기 위해 이 스크립트의 모든 섹션은 예외 없이 명시적으로
-- `begin; ... commit;` 또는 `begin; ... rollback;` 으로 감싸여 있다 — 어떤 섹션도
-- BEGIN/COMMIT 밖의 "맨 statement" 상태로 남겨두지 않는다. 섹션을 추가/수정할 때도
-- 이 규칙을 반드시 지킬 것.

-- ============================================================
-- -1. 안전장치 — 운영 프로젝트에서 실수로 실행되는 것을 막는다
--
-- 이중 방어:
--   (a) 명시적 플래그: 아래 SET 문은 기본적으로 주석 처리되어 있다. 지금 연결된
--       프로젝트가 진짜 테스트/스테이징 프로젝트임을 다시 한 번 확인한 뒤에만
--       주석을 해제한다. 주석을 풀지 않고 그대로 실행하면 이 블록에서 즉시
--       예외가 발생해 스크립트 전체가 멈춘다(그 아래 어떤 DML도 실행되지 않음).
--   (b) 데이터 존재 여부: 플래그를 해제했더라도, app_users/messages에 이미 데이터가
--       있으면(운영 프로젝트이거나 정리 안 된 이전 테스트) 중단한다. 정상적인
--       테스트 프로젝트는 phase2b_schema.sql 적용 직후 이 두 테이블이 비어있어야 한다.
--       (이전 실행이 중간에 실패해 데이터가 남아있다면 phase2b_rls_tests_cleanup.sql을
--       먼저 실행한다.)
-- ============================================================

-- 아래 줄의 주석을 해제해야만 스크립트가 진행된다 (기본값 = 미설정 = 중단):
SET app.confirm_test_run = 'RUN_ON_TEST_PROJECT_ONLY';

begin;
do $$
begin
    if current_setting('app.confirm_test_run', true) is distinct from 'RUN_ON_TEST_PROJECT_ONLY' then
        raise exception '중단: 테스트 실행 플래그가 설정되지 않았습니다. 이 스크립트는 테스트/스테이징 Supabase 프로젝트에서만 실행해야 합니다. 정말 테스트 프로젝트가 맞다면, 이 파일 상단의 SET app.confirm_test_run = ''RUN_ON_TEST_PROJECT_ONLY''; 주석을 해제한 뒤 다시 실행하세요.';
    end if;

    if (select count(*) from public.app_users) > 0 then
        raise exception '중단: public.app_users에 기존 데이터가 %건 있습니다. 운영 프로젝트이거나 이전 테스트 데이터가 정리되지 않은 상태로 보입니다. phase2b_rls_tests_cleanup.sql을 먼저 실행하거나, 비어있는 테스트 프로젝트에서만 실행하세요.',
            (select count(*) from public.app_users);
    end if;

    if (select count(*) from public.messages) > 0 then
        raise exception '중단: public.messages에 기존 데이터가 %건 있습니다. phase2b_rls_tests_cleanup.sql을 먼저 실행하거나, 비어있는 테스트 프로젝트에서만 실행하세요.',
            (select count(*) from public.messages);
    end if;

    raise notice 'PASS: -1. 안전장치 통과 (테스트 실행 플래그 확인 + 기존 데이터 없음 확인)';
end $$;
commit;

-- ============================================================
-- 0. 준비: 테스트 계정 5종 생성 (pending / viewer / editor / admin / blocked) + 메시지 12건 시드
--
-- 이 블록은 반드시 COMMIT으로 끝난다 — 아래 2번부터는 각 역할을 흉내내는 독립된
-- BEGIN/ROLLBACK 트랜잭션이 이어지는데, 만약 이 준비 데이터가 커밋되지 않은 채로
-- 남아있으면 그 뒤에 나오는 첫 BEGIN이 "이미 열려있는" 이 트랜잭션에 합류해버리고,
-- 그 트랜잭션의 ROLLBACK이 이 준비 데이터까지 통째로 지워버린다 (실제로 발생했던 버그).
-- ============================================================
begin;
do $$
declare
    v_instance uuid := '00000000-0000-0000-0000-000000000000';
begin
    -- 주의: UUID는 16진수(0-9, a-f)만 허용된다. 역할별로 구분하기 쉽도록 마지막
    -- 그룹만 001~005로 다르게 부여한다 (p/v/e/a/b 같은 영문 접미사는 잘못된 UUID이므로 사용하지 않음).
    insert into auth.users (
        instance_id, id, aud, role, email,
        encrypted_password, email_confirmed_at, created_at, updated_at,
        raw_app_meta_data, raw_user_meta_data
    ) values
    (v_instance, '00000000-0000-0000-0000-000000000001', 'authenticated', 'authenticated',
     'test-pending@example.com', crypt('test-pass', gen_salt('bf')), now(), now(), now(), '{}', '{}'),
    (v_instance, '00000000-0000-0000-0000-000000000002', 'authenticated', 'authenticated',
     'test-viewer@example.com', crypt('test-pass', gen_salt('bf')), now(), now(), now(), '{}', '{}'),
    (v_instance, '00000000-0000-0000-0000-000000000003', 'authenticated', 'authenticated',
     'test-editor@example.com', crypt('test-pass', gen_salt('bf')), now(), now(), now(), '{}', '{}'),
    (v_instance, '00000000-0000-0000-0000-000000000004', 'authenticated', 'authenticated',
     'test-admin@example.com', crypt('test-pass', gen_salt('bf')), now(), now(), now(), '{}', '{}'),
    (v_instance, '00000000-0000-0000-0000-000000000005', 'authenticated', 'authenticated',
     'test-blocked@example.com', crypt('test-pass', gen_salt('bf')), now(), now(), now(), '{}', '{}');

    -- trg_on_auth_user_created 트리거가 위 5명에 대해 app_users(status=pending, role=viewer)를 자동 생성함.
    -- 테스트 목적에 맞게 상태/역할을 조정한다.
    update public.app_users set status = 'approved', role = 'viewer' where id = '00000000-0000-0000-0000-000000000002';
    update public.app_users set status = 'approved', role = 'editor' where id = '00000000-0000-0000-0000-000000000003';
    update public.app_users set status = 'approved', role = 'admin'  where id = '00000000-0000-0000-0000-000000000004';
    update public.app_users set status = 'blocked',  role = 'editor' where id = '00000000-0000-0000-0000-000000000005';
    -- pending 계정(...0001)은 트리거 기본값(pending/viewer) 그대로 둔다.

    raise notice 'PASS: 0-1. 테스트 계정 5종 생성 완료';
end $$;

-- 메시지 1~12 초기값 (admin 계정으로 직접 삽입 — INSERT 정책은 updated_by=auth.uid() 검증하므로
-- superuser로 실행하는 이 블록에서는 RLS를 우회하는 별도 삽입을 사용한다)
insert into public.messages (message_number, text, version, updated_by, source)
select gs, '초기 메시지 ' || gs, 1, '00000000-0000-0000-0000-000000000004', 'mobile'
from generate_series(1, 12) as gs
on conflict (message_number) do nothing;

do $$ begin raise notice 'PASS: 0-2. 메시지 12건 시드 완료'; end $$;
commit;

-- ============================================================
-- 1. 테이블/함수/트리거/RLS 적용 여부 확인
-- ============================================================
begin;
do $$
begin
    assert (select count(*) from information_schema.tables
            where table_schema='public' and table_name in ('app_users','messages','message_history')) = 3,
        'FAIL: 테이블 3개가 모두 존재해야 함';

    assert (select relrowsecurity from pg_class where relname='app_users' and relnamespace='public'::regnamespace) = true,
        'FAIL: app_users RLS가 활성화되어야 함';
    assert (select relrowsecurity from pg_class where relname='messages' and relnamespace='public'::regnamespace) = true,
        'FAIL: messages RLS가 활성화되어야 함';
    assert (select relrowsecurity from pg_class where relname='message_history' and relnamespace='public'::regnamespace) = true,
        'FAIL: message_history RLS가 활성화되어야 함';

    assert (select count(*) from pg_trigger where tgname='trg_messages_history') = 1,
        'FAIL: trg_messages_history 트리거가 있어야 함';
    assert (select count(*) from pg_trigger where tgname='trg_on_auth_user_created') = 1,
        'FAIL: trg_on_auth_user_created 트리거가 있어야 함';

    raise notice 'PASS: 1. 테이블/트리거/RLS 활성화 확인';
end $$;
commit;

-- ============================================================
-- 2. 역할별 RLS 매트릭스 — pending/blocked/viewer/editor/admin 각각에 대해
--    (a) messages 조회 가능 여부, (b) messages 수정 가능 여부,
--    (c) app_users 조회 범위, (d) app_users 관리(수정) 가능 여부
--    를 모두 검증한다. 이 섹션은 전부 ROLLBACK으로 끝나 부작용을 남기지 않는다.
--
-- 수정 차단 검증 방식에 대한 주의: RLS의 USING 절이 막는 UPDATE는 "에러가 나는 게
-- 아니라 대상 행이 0건으로 필터링되어 조용히 0 rows affected로 끝난다." 그래서
-- "UPDATE 실행 → 곧바로 raise exception 'FAIL' → exception when others로 잡아서
-- PASS 출력" 같은 패턴은 절대 쓰지 않는다 — UPDATE가 실제로 성공(=권한 우회)하든
-- 실패하든 상관없이 그 직후에 우리가 직접 raise exception 'FAIL'을 실행하고 그걸
-- when others가 그대로 삼켜버리므로 항상 PASS로 나온다(실제로 이 버그가 있었다).
-- 대신 GET DIAGNOSTICS ... = ROW_COUNT로 "몇 건이 실제로 바뀌었는지"를 직접 확인한다.
-- ============================================================

-- 2-1. pending
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000001", "role": "authenticated"}';
do $$
declare
    v_row_count integer;
begin
    -- (a) messages 조회
    assert (select count(*) from public.messages) = 0, 'FAIL: pending은 messages를 하나도 못 봐야 함';
    assert (select count(*) from public.message_history) = 0, 'FAIL: pending은 이력도 못 봐야 함';

    -- (b) messages 수정 — RLS가 USING절에서 행을 필터링하므로 에러 없이 0 rows여야 함
    update public.messages set text = text || ' [무단수정시도]' where message_number = 1;
    get diagnostics v_row_count = row_count;
    assert v_row_count = 0, format('FAIL: pending이 messages를 실제로 수정함 (영향받은 행=%s)', v_row_count);

    -- (c) app_users 조회 범위 — 자기 자신 1건만
    assert (select count(*) from public.app_users) = 1, 'FAIL: pending은 app_users에서 자기 자신 1건만 봐야 함';

    -- (d) app_users 관리 — admin 전용이므로 0 rows
    update public.app_users set role = 'admin' where id = '00000000-0000-0000-0000-000000000001';
    get diagnostics v_row_count = row_count;
    assert v_row_count = 0, format('FAIL: pending이 app_users를 실제로 수정함 (영향받은 행=%s)', v_row_count);

    raise notice 'PASS: 2-1. pending 매트릭스(조회 차단/수정 차단/자기자신만 조회/관리권한 없음) 확인';
end $$;
rollback;

-- 2-2. blocked (과거에 editor였더라도 지금은 전부 차단되어야 함)
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000005", "role": "authenticated"}';
do $$
declare
    v_row_count integer;
begin
    assert (select count(*) from public.messages) = 0, 'FAIL: blocked는 messages를 못 봐야 함';

    update public.messages set text = text || ' [무단수정시도]' where message_number = 1;
    get diagnostics v_row_count = row_count;
    assert v_row_count = 0, format('FAIL: blocked가 messages를 실제로 수정함 (영향받은 행=%s)', v_row_count);

    assert (select count(*) from public.app_users) = 1, 'FAIL: blocked는 app_users에서 자기 자신 1건만 봐야 함';

    update public.app_users set status = 'approved' where id = '00000000-0000-0000-0000-000000000005';
    get diagnostics v_row_count = row_count;
    assert v_row_count = 0, format('FAIL: blocked가 app_users를 실제로 수정함(자기 자신 재승인 시도) (영향받은 행=%s)', v_row_count);

    raise notice 'PASS: 2-2. blocked 매트릭스(조회 차단/수정 차단/자기자신만 조회/관리권한 없음) 확인';
end $$;
rollback;

-- 2-3. viewer (읽기는 되지만 쓰기는 전부 안 됨)
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000002", "role": "authenticated"}';
do $$
declare
    v_row_count integer;
begin
    assert (select count(*) from public.messages) = 12, 'FAIL: viewer는 messages 12건을 볼 수 있어야 함';

    update public.messages set text = text || ' [무단수정시도]' where message_number = 1;
    get diagnostics v_row_count = row_count;
    assert v_row_count = 0, format('FAIL: viewer가 messages를 실제로 수정함 (영향받은 행=%s)', v_row_count);

    assert (select count(*) from public.app_users) = 1, 'FAIL: viewer는 app_users에서 자기 자신 1건만 봐야 함';

    update public.app_users set role = 'admin' where id = '00000000-0000-0000-0000-000000000002';
    get diagnostics v_row_count = row_count;
    assert v_row_count = 0, format('FAIL: viewer가 app_users를 실제로 수정함(자기 자신 승격 시도) (영향받은 행=%s)', v_row_count);

    raise notice 'PASS: 2-3. viewer 매트릭스(조회 허용/수정 차단/자기자신만 조회/관리권한 없음) 확인';
end $$;
rollback;

-- 2-4. editor (읽기+쓰기는 허용, app_users 관리는 불허) — 여기서는 매트릭스만 빠르게 확인하고
-- (쓰기는 rollback으로 되돌림), 이력 자동 생성을 포함한 상세 검증은 4번에서 별도로 한다.
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000003", "role": "authenticated"}';
do $$
declare
    v_row_count integer;
begin
    assert (select count(*) from public.messages) = 12, 'FAIL: editor는 messages 12건을 볼 수 있어야 함';

    -- messages_update_editor 정책의 WITH CHECK는 fn_can_edit() 뿐 아니라
    -- updated_by = auth.uid()도 함께 요구한다. text만 바꾸고 updated_by를 그대로
    -- 두면(=이전 값이 자기 자신이 아니면) WITH CHECK 위반으로 42501 예외가 나므로,
    -- 실제 클라이언트가 하듯 updated_by/updated_at/version을 함께 갱신한다.
    update public.messages
       set text = text || ' [editor 매트릭스 테스트]',
           updated_by = auth.uid(),
           updated_at = now(),
           version = version + 1
     where message_number = 1;
    get diagnostics v_row_count = row_count;
    assert v_row_count = 1, format('FAIL: editor는 messages를 수정할 수 있어야 하는데 영향받은 행=%s', v_row_count);

    assert (select count(*) from public.app_users) = 1, 'FAIL: editor도 app_users에서 자기 자신 1건만 봐야 함';

    update public.app_users set role = 'admin' where id = '00000000-0000-0000-0000-000000000003';
    get diagnostics v_row_count = row_count;
    assert v_row_count = 0, format('FAIL: editor가 app_users를 실제로 수정함(자기 자신 승격 시도) (영향받은 행=%s)', v_row_count);

    raise notice 'PASS: 2-4. editor 매트릭스(조회 허용/수정 허용/자기자신만 조회/관리권한 없음) 확인';
end $$;
rollback;

-- 2-5. admin (읽기+쓰기+app_users 관리 전부 허용)
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000004", "role": "authenticated"}';
do $$
declare
    v_row_count integer;
begin
    assert (select count(*) from public.messages) = 12, 'FAIL: admin은 messages 12건을 볼 수 있어야 함';

    -- 2-4와 동일한 이유로 updated_by/updated_at/version을 함께 갱신한다. 시드 데이터의
    -- updated_by가 우연히 admin 자신의 id와 같아 이 값을 안 바꿔도 통과할 수 있지만,
    -- 그건 우연에 의존하는 것이므로 실제 클라이언트 동작과 동일하게 명시적으로 갱신한다.
    update public.messages
       set text = text || ' [admin 매트릭스 테스트]',
           updated_by = auth.uid(),
           updated_at = now(),
           version = version + 1
     where message_number = 1;
    get diagnostics v_row_count = row_count;
    assert v_row_count = 1, format('FAIL: admin은 messages를 수정할 수 있어야 하는데 영향받은 행=%s', v_row_count);

    assert (select count(*) from public.app_users) = 5, 'FAIL: admin은 app_users 전체 5건을 볼 수 있어야 함';

    update public.app_users set role = 'editor' where id = '00000000-0000-0000-0000-000000000001';
    get diagnostics v_row_count = row_count;
    assert v_row_count = 1, format('FAIL: admin은 다른 사용자의 role을 변경할 수 있어야 하는데 영향받은 행=%s', v_row_count);

    raise notice 'PASS: 2-5. admin 매트릭스(조회 허용/수정 허용/전체 조회/관리권한 있음) 확인';
end $$;
rollback;

-- ============================================================
-- 4. editor의 메시지 즉시 수정 + 이력 자동 생성 + updated_by 위조 차단 (상세)
-- ============================================================
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000003", "role": "authenticated"}';
do $$
declare
    v_history_count_before integer;
    v_history_count_after integer;
    v_version integer;
    v_row_count integer;
begin
    select count(*) into v_history_count_before from public.message_history where message_number = 1;

    update public.messages
       set text = '편집자가 즉시 수정한 메시지1',
           version = version + 1,
           updated_by = '00000000-0000-0000-0000-000000000003',
           updated_at = now(),
           source = 'mobile'
     where message_number = 1;
    get diagnostics v_row_count = row_count;
    assert v_row_count = 1, format('FAIL: editor의 정상 수정이 실제로 반영되어야 하는데 영향받은 행=%s', v_row_count);

    select version into v_version from public.messages where message_number = 1;
    assert v_version = 2, 'FAIL: version이 1 증가해야 함';

    select count(*) into v_history_count_after from public.message_history where message_number = 1;
    assert v_history_count_after = v_history_count_before + 1,
        'FAIL: 승인/확인 절차 없이도 UPDATE 즉시 message_history에 1건 자동 기록되어야 함';

    assert exists (
        select 1 from public.message_history
        where message_number = 1 and text_after = '편집자가 즉시 수정한 메시지1'
          and updated_by = '00000000-0000-0000-0000-000000000003'
    ), 'FAIL: 이력에 수정자/변경후 내용이 정확히 남아야 함';

    raise notice 'PASS: 4-1. editor 즉시 수정 + 자동 이력 생성 확인';
end $$;

-- editor가 다른 사람 행세로 저장 시도(신원 사칭) — 이 경우는 USING은 통과하지만 WITH CHECK가
-- 실패해 Postgres가 실제 예외(42501, "new row violates row-level security policy")를 던진다.
-- "우리가 직접 raise exception 'FAIL'을 실행하고 그걸 잡아서 PASS로 위장"하는 안티패턴을
-- 피하기 위해, 예외 발생 여부를 별도 boolean 플래그로 기록하고 그 플래그를 바깥에서 assert한다
-- (assert 자체가 실패하면 이 do 블록의 EXCEPTION 절 밖에서 진짜 에러로 전파되어 스크립트가 멈춘다).
do $$
declare
    v_blocked boolean := false;
begin
    begin
        update public.messages
           set text = '사칭시도', updated_by = '00000000-0000-0000-0000-000000000004'
         where message_number = 2;
    exception
        when others then
            v_blocked := true;
    end;

    assert v_blocked, 'FAIL: editor가 updated_by를 다른 계정으로 위조했는데 예외가 발생하지 않음';

    -- 위조 시도가 실제로 반영되지 않았는지(값이 안 바뀌었는지)도 함께 확인
    assert (select text from public.messages where message_number = 2) = '초기 메시지 2',
        'FAIL: 위조 시도 이후에도 message_number=2의 값은 원래 그대로여야 함';

    raise notice 'PASS: 4-2. updated_by 위조(다른 계정 사칭) 시도가 차단됨(값도 변경되지 않음)';
end $$;
commit;  -- 4번은 이후 6번 복원 테스트에서 쓸 이력을 남기기 위해 commit한다

-- ============================================================
-- 5. admin의 사용자 승인 / 차단 / 권한 변경 검증 (+ editor는 접근 불가 확인)
-- ============================================================
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000004", "role": "authenticated"}';
do $$
declare
    v_row_count integer;
begin
    -- 5-1. pending 승인
    update public.app_users
       set status = 'approved', role = 'viewer',
           approved_by = '00000000-0000-0000-0000-000000000004', approved_at = now()
     where id = '00000000-0000-0000-0000-000000000001';
    get diagnostics v_row_count = row_count;
    assert v_row_count = 1, format('FAIL: admin이 pending 사용자를 승인할 수 있어야 하는데 영향받은 행=%s', v_row_count);
    assert (select status from public.app_users where id = '00000000-0000-0000-0000-000000000001') = 'approved',
        'FAIL: 승인 후 status가 approved여야 함';

    -- 5-2. 역할 변경 (방금 승인한 사용자를 editor로 승격)
    update public.app_users set role = 'editor' where id = '00000000-0000-0000-0000-000000000001';
    assert (select role from public.app_users where id = '00000000-0000-0000-0000-000000000001') = 'editor',
        'FAIL: admin이 역할을 변경할 수 있어야 함';

    -- 5-3. 차단
    update public.app_users set status = 'blocked' where id = '00000000-0000-0000-0000-000000000001';
    assert (select status from public.app_users where id = '00000000-0000-0000-0000-000000000001') = 'blocked',
        'FAIL: admin이 사용자를 차단할 수 있어야 함';

    -- 5-4. 재활성화
    update public.app_users set status = 'approved' where id = '00000000-0000-0000-0000-000000000001';
    assert (select status from public.app_users where id = '00000000-0000-0000-0000-000000000001') = 'approved',
        'FAIL: admin이 사용자를 재활성화할 수 있어야 함';

    raise notice 'PASS: 5. admin의 승인/역할변경/차단/재활성화 확인';
end $$;
rollback;  -- 다음 블록에서 원래 pending 상태로 다시 테스트할 수 있도록 되돌림

-- 5-5. editor는 app_users를 수정할 수 없어야 함 (관리자 기능 접근 불가 확인, ROW_COUNT 방식)
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000003", "role": "authenticated"}';
do $$
declare
    v_row_count integer;
begin
    update public.app_users set status = 'approved' where id = '00000000-0000-0000-0000-000000000001';
    get diagnostics v_row_count = row_count;
    assert v_row_count = 0, format('FAIL: editor가 app_users를 실제로 수정함(admin 전용이어야 함) (영향받은 행=%s)', v_row_count);
    raise notice 'PASS: 5-5. editor의 app_users 수정 시도가 차단됨(관리자 기능 접근 불가, 0 rows)';
end $$;
rollback;

-- ============================================================
-- 6. 이전 버전 복원 — admin 전용 검증 (pending/viewer/editor/blocked 전부 거부,
--    admin만 성공) + 복원 이력 생성 검증
--
-- fn_restore_message()는 fn_is_admin()으로 내부 권한을 검사하며, 권한이 없으면
-- RAISE EXCEPTION으로 실제 예외를 던진다(RLS의 USING절 필터링과 달리 함수 코드가
-- 직접 예외를 던지는 것이므로, 여기서는 "예외가 실제로 발생했는지"를 boolean 플래그로
-- 확인하는 패턴을 쓴다 — 4-2와 동일한 이유로, 우리가 만든 FAIL 예외를 스스로 잡아
-- PASS로 오판하는 일이 없도록 한다).
-- ============================================================

-- 6-1. pending은 거부되어야 함
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000001", "role": "authenticated"}';
do $$
declare
    v_any_history_id bigint;
    v_blocked boolean := false;
begin
    select id into v_any_history_id from public.message_history where message_number = 3 order by id asc limit 1;
    begin
        perform public.fn_restore_message(3, v_any_history_id);
    exception
        when others then
            v_blocked := true;
    end;
    assert v_blocked, 'FAIL: pending이 복원을 실행했는데 예외가 발생하지 않음';
    raise notice 'PASS: 6-1. pending 복원 실패(정상) 확인';
end $$;
rollback;

-- 6-2. viewer는 거부되어야 함
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000002", "role": "authenticated"}';
do $$
declare
    v_any_history_id bigint;
    v_blocked boolean := false;
begin
    select id into v_any_history_id from public.message_history where message_number = 2 order by id asc limit 1;
    begin
        perform public.fn_restore_message(2, v_any_history_id);
    exception
        when others then
            v_blocked := true;
    end;
    assert v_blocked, 'FAIL: viewer가 복원을 실행했는데 예외가 발생하지 않음';
    raise notice 'PASS: 6-2. viewer 복원 실패(정상) 확인';
end $$;
rollback;

-- 6-3. editor는 거부되어야 함 (일반 수정은 되지만 복원 RPC는 admin 전용)
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000003", "role": "authenticated"}';
do $$
declare
    v_any_history_id bigint;
    v_blocked boolean := false;
begin
    select id into v_any_history_id from public.message_history where message_number = 4 order by id asc limit 1;
    begin
        perform public.fn_restore_message(4, v_any_history_id);
    exception
        when others then
            v_blocked := true;
    end;
    assert v_blocked, 'FAIL: editor가 복원을 실행했는데 예외가 발생하지 않음(복원은 admin 전용이어야 함)';
    raise notice 'PASS: 6-3. editor 복원 실패(정상, 복원은 admin 전용) 확인';
end $$;
rollback;

-- 6-4. blocked는 거부되어야 함
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000005", "role": "authenticated"}';
do $$
declare
    v_any_history_id bigint;
    v_blocked boolean := false;
begin
    select id into v_any_history_id from public.message_history where message_number = 5 order by id asc limit 1;
    begin
        perform public.fn_restore_message(5, v_any_history_id);
    exception
        when others then
            v_blocked := true;
    end;
    assert v_blocked, 'FAIL: blocked가 복원을 실행했는데 예외가 발생하지 않음';
    raise notice 'PASS: 6-4. blocked 복원 실패(정상) 확인';
end $$;
rollback;

-- 6-5. admin은 성공해야 함 — 값 변경, updated_by 기록, source='restore' 이력,
--      복원된 값이 과거 대상 값과 정확히 일치하는지까지 함께 확인한다.
begin;
set local role authenticated;
set local request.jwt.claims = '{"sub": "00000000-0000-0000-0000-000000000004", "role": "authenticated"}';
do $$
declare
    v_target_history_id bigint;
    v_target_text text;          -- 복원 목표로 삼은 과거 이력의 text_after (기대값)
    v_before_text text;          -- 복원 직전 현재 messages.text (복원 후 반드시 달라져야 함)
    v_restored_text text;
    v_restored_updated_by uuid;
    v_history_count_before integer;
    v_history_count_after integer;
begin
    -- 4번 블록에서 editor가 message_number=1을 수정했으므로, 그 최초(=최초 삽입 시점) 값을
    -- 복원 목표로 삼는다. 이 값은 현재 messages.text(=4번의 수정 결과)와 달라야 의미 있는 테스트가 된다.
    select id, text_after into v_target_history_id, v_target_text
      from public.message_history
     where message_number = 1
     order by id asc
     limit 1;

    select text into v_before_text from public.messages where message_number = 1;
    assert v_before_text is distinct from v_target_text,
        'FAIL: 테스트 전제 오류 — 복원 전 현재 값과 목표 값이 이미 같음(의미있는 복원 테스트가 아님)';

    select count(*) into v_history_count_before from public.message_history where message_number = 1;

    perform public.fn_restore_message(1, v_target_history_id);

    select text, updated_by into v_restored_text, v_restored_updated_by
      from public.messages where message_number = 1;

    -- 복원된 값과 과거 대상 값 일치 확인
    assert v_restored_text = v_target_text,
        'FAIL: 복원된 텍스트가 목표로 삼은 과거 이력 값과 일치해야 함';

    -- 복원 수행자(admin, ...0004)가 updated_by에 정확히 기록되는지 확인
    assert v_restored_updated_by = '00000000-0000-0000-0000-000000000004',
        'FAIL: 복원 후 updated_by가 복원을 수행한 admin 계정이어야 함';

    -- 복원 행위 자체가 트리거로 message_history에 새 이력을 남기는지 확인
    select count(*) into v_history_count_after from public.message_history where message_number = 1;
    assert v_history_count_after = v_history_count_before + 1,
        'FAIL: 복원 행위 자체도 새 이력으로 자동 기록되어야 함';

    -- 새 이력의 source가 restore인지 확인
    assert exists (
        select 1 from public.message_history
        where message_number = 1 and source = 'restore'
          and updated_by = '00000000-0000-0000-0000-000000000004'
          and text_after = v_target_text
        order by id desc limit 1
    ), 'FAIL: 새 이력의 source=restore, updated_by=admin, text_after=목표값이 모두 일치해야 함';

    raise notice 'PASS: 6-5. admin 복원 성공 + 값 변경 + updated_by 기록 + source=restore 이력 + 복원값 일치 확인';
end $$;
commit;

-- ============================================================
-- 9. 정리 — 테스트 계정과 테스트 메시지 삭제
--
-- messages.updated_by / message_history.updated_by가 app_users(id)를 참조하며
-- ON DELETE CASCADE가 아니므로, auth.users(→app_users cascade)보다 먼저
-- message_history/messages를 지워야 FK 위반이 나지 않는다.
--
-- 삭제 전 안전 확인: app_users에 test-%@example.com 이외의 이메일이 하나라도 있으면
-- (실제 데이터가 섞여 있을 가능성) 아무것도 지우지 않고 중단한다 — -1번 안전장치와
-- 별개로 한 번 더 확인하는 방어선이다.
-- ============================================================
begin;
do $$
declare
    v_non_test_count integer;
begin
    select count(*) into v_non_test_count
    from public.app_users
    where email not like 'test-%@example.com';

    if v_non_test_count > 0 then
        raise exception '중단: app_users에 테스트 계정이 아닌 행이 %건 있어 정리를 건너뜁니다. 수동으로 확인하세요.', v_non_test_count;
    end if;

    raise notice '확인 완료: app_users의 모든 행이 테스트 계정입니다 — 정리를 진행합니다.';
end $$;

delete from public.message_history;
delete from public.messages;
delete from auth.users where email like 'test-%@example.com';  -- app_users는 CASCADE로 함께 삭제됨

do $$ begin raise notice 'PASS: 9. 테스트 데이터 정리 완료'; end $$;
commit;
