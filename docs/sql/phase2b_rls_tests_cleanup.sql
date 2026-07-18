-- Phase 2B RLS 테스트 잔여 데이터 정리 스크립트 (독립 실행용)
--
-- 용도: phase2b_rls_tests.sql이 중간에 실패해 9번(정리) 블록까지 도달하지 못했을 때,
-- 다시 실행하기 전에 이 스크립트로 잔여 테스트 데이터를 지운다. phase2b_rls_tests.sql의
-- -1번 안전장치는 app_users/messages가 비어있지 않으면 즉시 중단하도록 되어 있으므로,
-- 이전 실행이 실패해 데이터가 남아있는 상태에서는 이 스크립트를 먼저 실행해야 한다.
--
-- 안전장치: app_users에 test-%@example.com 이외의 이메일을 가진 행이 하나라도 있으면
-- (=실제 사용자 데이터가 섞여 있을 가능성) 아무것도 지우지 않고 즉시 중단한다.
-- 이 스크립트도 phase2b_rls_tests.sql과 마찬가지로 테스트/스테이징 프로젝트에서만
-- 실행한다.

begin;
do $$
declare
    v_non_test_count integer;
begin
    select count(*) into v_non_test_count
    from public.app_users
    where email not like 'test-%@example.com';

    if v_non_test_count > 0 then
        raise exception '중단: app_users에 테스트 계정(test-%%@example.com)이 아닌 행이 %건 있습니다. 실제 데이터가 섞여 있을 수 있어 삭제하지 않습니다. 수동으로 확인하세요.', v_non_test_count;
    end if;

    raise notice '확인 완료: app_users의 모든 행이 테스트 계정입니다 — 정리를 진행합니다.';
end $$;

-- messages.updated_by / message_history.updated_by가 app_users(id)를 참조하며
-- ON DELETE CASCADE가 아니므로, auth.users(→app_users cascade)보다 먼저
-- message_history/messages를 지워야 FK 위반이 나지 않는다.
delete from public.message_history;
delete from public.messages;
delete from auth.users where email like 'test-%@example.com';  -- app_users는 CASCADE로 함께 삭제됨

do $$ begin raise notice 'PASS: 잔여 테스트 데이터 정리 완료 — 이제 phase2b_rls_tests.sql을 처음부터 다시 실행할 수 있습니다.'; end $$;
commit;
