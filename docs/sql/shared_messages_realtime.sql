-- Mobile 실시간 동기화 — shared_messages / shared_message_history / RPC / RLS / Realtime
-- 상태: 초안 — Production Stabilization Sprint에서 운영 적용 가능한 수준으로 강화됨.
-- 아직 실제 Supabase 프로젝트에 적용되지 않음(검토 후 수동 적용 필요, 이 원칙은
-- 이번 스프린트에서도 유지된다 — 운영 DB에 자동으로 SQL을 적용하지 않는다).
--
-- ============================================================
-- 적용 전 확인사항 (반드시 순서대로 확인할 것)
-- ============================================================
-- 1. docs/sql/phase2b_schema.sql이 이미 적용되어 있는지 확인한다
--    (app_role/app_user_status enum, app_users 테이블, fn_is_approved()/
--    fn_can_edit()/fn_is_admin() 함수가 이미 있어야 한다). 없으면 먼저 그 파일을
--    적용한다 — 이 파일은 그 객체들을 그대로 재사용하며 새로 만들지 않는다.
-- 2. 대상 프로젝트에서 pgcrypto 확장이 활성화되어 있는지 확인한다(gen_random_uuid()
--    사용 — phase2b_schema.sql이 이미 활성화했겠지만, 이 파일 단독 적용 시나리오
--    대비 아래에서도 idempotent하게 다시 확인한다).
-- 3. 이 파일 전체를 SQL Editor에 붙여넣기 전에, 스테이징/테스트 프로젝트에서 먼저
--    실행해 보는 것을 권장한다.
-- 4. 이 파일은 여러 번 실행해도 안전하다(idempotent) — 이미 적용된 상태에서 다시
--    실행해도 데이터를 덮어쓰지 않는다. 단, 최초 적용과 재적용 모두 실행 후
--    scripts/check_shared_messages_schema.py로 결과를 검증할 것을 권장한다.
-- 5. 기존 messages/message_history 테이블은 이 파일 어디에서도 변경하지 않는다
--    (레거시 PC 30초 폴링 동기화가 그대로 동작해야 함).
-- 6. 실제 프로젝트 URL/키/비밀번호를 이 파일에 채워 넣지 않는다 — 이 파일은
--    Supabase SQL Editor에 값 없이 그대로 붙여넣는 스크립트다.
--
-- ============================================================
-- 롤백 / 수동 복구 절차
-- ============================================================
-- 이 마이그레이션에는 자동 DOWN 마이그레이션을 포함하지 않는다(데이터 삭제를
-- 자동화하는 것 자체가 위험하다고 판단함) — 문제가 생기면 아래를 수동으로 실행한다.
--
--   -- RPC 실행 권한만 우선 차단하고 싶을 때(테이블/데이터는 보존):
--   revoke execute on function public.update_shared_message(integer, text, text, bigint, public.shared_message_source) from authenticated;
--   revoke execute on function public.force_update_shared_message(integer, text, text, public.shared_message_source) from authenticated;
--
--   -- Realtime 발행만 되돌리고 싶을 때(테이블은 유지, 이벤트만 중단):
--   alter publication supabase_realtime drop table public.shared_messages;
--
--   -- 완전히 되돌리고 싶을 때(주의: shared_message_history의 이력 데이터가 함께 사라짐):
--   -- drop function if exists public.force_update_shared_message(integer, text, text, public.shared_message_source);
--   -- drop function if exists public.update_shared_message(integer, text, text, bigint, public.shared_message_source);
--   -- drop table if exists public.shared_message_history;
--   -- drop table if exists public.shared_messages;
--   -- 위 DROP 문은 기본적으로 주석 처리되어 있다 — 실제로 필요할 때만 관리자가
--   --   직접 주석을 해제하고 실행한다(이 파일을 그대로 실행해도 절대 삭제되지 않음).
--
-- 역할 매핑(이번 스프린트 스펙의 "employee/admin/disabled" ↔ 기존 스키마):
--   employee = app_users.status='approved' AND role IN ('editor','admin') (기존 fn_can_edit())
--   admin    = app_users.status='approved' AND role='admin' (기존 fn_is_admin())
--   disabled = app_users.status IN ('pending','blocked') (기존 fn_is_approved()=false)
-- 새 사용자 승인/차단은 기존 Phase 2B/4-1 흐름(app_users, admin_* RPC)을 그대로 쓴다.

-- ============================================================
-- 0. 확장 + update_source 열거형
-- ============================================================
create extension if not exists pgcrypto;

do $$ begin
    create type public.shared_message_source as enum ('desktop', 'mobile', 'migration', 'system', 'admin_force');
exception when duplicate_object then
    raise notice '건너뜀: public.shared_message_source 타입이 이미 존재함';
end $$;

-- 이미 이전 버전의 타입(admin_force 없음)이 적용되어 있을 수 있으므로, 값만
-- 추가로 보강한다(PostgreSQL 12+에서 ADD VALUE IF NOT EXISTS 지원).
do $$ begin
    alter type public.shared_message_source add value if not exists 'admin_force';
exception when others then
    raise notice '건너뜀: shared_message_source에 admin_force 추가 중 예외(이미 존재하거나 트랜잭션 제약) — 수동 확인 필요';
end $$;

-- ============================================================
-- 1. shared_messages — 1~12번 메시지의 현재 상태 (Single Source of Truth)
-- ============================================================
create table if not exists public.shared_messages (
    id              uuid primary key default gen_random_uuid(),
    message_no      integer not null,
    title           text,
    content         text not null default '',
    revision        bigint not null default 1,
    is_active       boolean not null default true,
    updated_at      timestamptz not null default now(),
    updated_by      uuid references public.app_users(id) on delete set null,
    updated_by_name text,
    update_source   public.shared_message_source not null default 'system',
    created_at      timestamptz not null default now(),
    constraint shared_messages_message_no_range check (message_no between 1 and 12),
    constraint shared_messages_message_no_unique unique (message_no),
    constraint shared_messages_revision_positive check (revision > 0)
);

comment on table public.shared_messages is
    '1~12번 메시지의 현재 상태. PC/모바일 모두 이 테이블을 통해서만 메시지를 주고받는다 '
    '(요구사항 3/8 — Supabase가 Single Source of Truth). revision은 낙관적 동시성 제어에 '
    '쓰인다(update_shared_message RPC 참고). 레거시 public.messages 테이블과는 별개다. '
    '이 테이블에는 클라이언트 직접 UPDATE 정책이 없다 — 반드시 RPC를 통해서만 쓴다(5절 참고).';
comment on column public.shared_messages.revision is
    '낙관적 동시성 제어(OCC) 버전(1 이상). 클라이언트는 편집 시작 시점의 revision을 '
    'base_revision으로 들고 있다가 저장할 때 함께 보내고, RPC가 현재 값과 비교한다.';
comment on column public.shared_messages.update_source is
    'system=마이그레이션으로 생성된 미수정 초기값, desktop/mobile=실제 클라이언트 수정, '
    'migration=관리자의 초기 데이터 이전, admin_force=관리자의 충돌 무시 강제 저장. '
    'revision=1 AND update_source=''system''이면 "한 번도 실제로 수정된 적 없는" 상태로 '
    '취급한다(초기 마이그레이션 판단 기준).';

create index if not exists idx_shared_messages_updated_at on public.shared_messages(updated_at desc);

-- 최초 12개 행 보장 — 이미 있으면 아무 것도 하지 않는다(idempotent, 데이터 덮어쓰지 않음).
insert into public.shared_messages (message_no, content, revision, update_source)
select gs, '', 1, 'system'
from generate_series(1, 12) as gs
where not exists (select 1 from public.shared_messages where message_no = gs);

-- ============================================================
-- 2. shared_message_history — 모든 변경의 감사 추적 (RPC 내부에서만 기록)
-- ============================================================
create table if not exists public.shared_message_history (
    id                 uuid primary key default gen_random_uuid(),
    message_id         uuid references public.shared_messages(id) on delete set null,
    message_no         integer not null check (message_no between 1 and 12),
    previous_content    text,
    new_content         text not null,
    previous_revision   bigint,
    new_revision        bigint not null check (new_revision > 0),
    changed_by          uuid references public.app_users(id) on delete set null,
    changed_by_name     text,
    changed_from        public.shared_message_source not null,
    changed_at          timestamptz not null default now()
);

create index if not exists idx_shared_message_history_no
    on public.shared_message_history(message_no, changed_at desc);

comment on table public.shared_message_history is
    'shared_messages 변경 이력. 클라이언트가 직접 INSERT할 수 없다 — '
    'update_shared_message/force_update_shared_message RPC만 기록한다(5절 RLS 참고). '
    '이력 기록과 shared_messages 갱신은 같은 함수 호출(=같은 트랜잭션) 안에서 이루어지므로 '
    '항상 원자적이다 — 하나만 성공하고 다른 하나만 실패하는 상태는 존재하지 않는다.';

-- ============================================================
-- 3. update_shared_message — 일반 저장 RPC (OCC, PC/모바일 공용, desktop/mobile 전용)
-- ============================================================
-- 4단계(현재 revision 확인 → base_revision 비교 → history 저장 → 메시지 업데이트)를
-- 하나의 함수 호출(=하나의 트랜잭션) 안에서 원자적으로 수행한다. "select ... for
-- update"로 행을 잠가 동시에 들어온 두 저장 요청이 순차적으로만 처리되게 한다 —
-- 둘 다 같은 base_revision으로 시도하면 먼저 커밋한 쪽만 성공하고, 나중 요청은
-- 잠금이 풀린 뒤 바뀐 revision을 보고 REVISION_CONFLICT로 실패한다(완료 기준 9).
--
-- updated_by/updated_by_name은 항상 이 함수 내부에서 auth.uid() 기준으로 서버가
-- 직접 계산한다 — 클라이언트가 파라미터로 전달할 방법이 없다(함수 시그니처 자체에
-- 그런 파라미터가 없음). 승인된 직원 여부(fn_can_edit())도 매 호출마다 서버가
-- 다시 검증한다 — 클라이언트가 보낸 어떤 값도 권한 판단에 쓰이지 않는다.
create or replace function public.update_shared_message(
    p_message_no integer,
    p_title text,
    p_content text,
    p_base_revision bigint,
    p_update_source public.shared_message_source
)
returns public.shared_messages
language plpgsql
security definer
set search_path = public
as $$
declare
    v_current public.shared_messages%rowtype;
    v_result public.shared_messages%rowtype;
    v_actor_name text;
begin
    if not public.fn_can_edit() then
        raise exception 'PERMISSION_DENIED: 메시지를 수정할 권한이 없습니다.';
    end if;

    if p_message_no is null or p_message_no < 1 or p_message_no > 12 then
        raise exception 'INVALID_MESSAGE_NO: message_no는 1~12 사이여야 합니다.';
    end if;

    -- migration/admin_force/system은 이 RPC로 지정할 수 없다 — 각각 전용 경로가
    -- 따로 있다(마이그레이션/강제저장은 force_update_shared_message, system은
    -- 마이그레이션 시드 전용 값으로 클라이언트가 지정할 방법 자체가 없음).
    if p_update_source not in ('desktop', 'mobile') then
        raise exception 'INVALID_UPDATE_SOURCE: update_source는 desktop 또는 mobile이어야 합니다.';
    end if;

    if p_base_revision is null or p_base_revision < 1 then
        raise exception 'REVISION_ERROR: base_revision 값이 올바르지 않습니다.';
    end if;

    select * into v_current
    from public.shared_messages
    where message_no = p_message_no
    for update;

    if not found then
        raise exception 'MESSAGE_NOT_FOUND: message_no=%를 찾을 수 없습니다.', p_message_no;
    end if;

    if v_current.revision != p_base_revision then
        raise exception 'REVISION_CONFLICT: 다른 사용자가 먼저 저장했습니다(현재 revision=%).', v_current.revision;
    end if;

    select coalesce(display_name, email) into v_actor_name
    from public.app_users where id = auth.uid();

    insert into public.shared_message_history (
        message_id, message_no, previous_content, new_content,
        previous_revision, new_revision, changed_by, changed_by_name, changed_from
    ) values (
        v_current.id, p_message_no, v_current.content, p_content,
        v_current.revision, v_current.revision + 1, auth.uid(), v_actor_name, p_update_source
    );

    update public.shared_messages
    set content = p_content,
        title = coalesce(p_title, title),
        revision = v_current.revision + 1,
        is_active = true,
        updated_at = now(),
        updated_by = auth.uid(),
        updated_by_name = v_actor_name,
        update_source = p_update_source
    where message_no = p_message_no
    returning * into v_result;

    return v_result;
end;
$$;

revoke all on function public.update_shared_message(integer, text, text, bigint, public.shared_message_source) from public;
revoke execute on function public.update_shared_message(integer, text, text, bigint, public.shared_message_source) from anon;
grant execute on function public.update_shared_message(integer, text, text, bigint, public.shared_message_source) to authenticated;

-- ============================================================
-- 4. force_update_shared_message — 관리자 전용 강제 덮어쓰기(충돌 무시)
-- ============================================================
-- 요구사항 8절 "관리자 권한일 경우에만 강제 덮어쓰기" + 이번 스프린트 10절
-- "관리자 강제 저장"에 대응한다. base_revision 비교 없이 항상 성공하지만,
-- history는 동일하게 남는다(어떤 revision을 덮어썼는지 previous_revision으로
-- 추적 가능). update_source는 'migration'(13절 초기 마이그레이션) 또는
-- 'admin_force'(일반 충돌 상황에서의 관리자 강제 저장)만 허용한다 — 일반
-- desktop/mobile 값은 이 함수로 저장할 수 없다(정상 저장은 반드시
-- update_shared_message로만, OCC를 우회하지 않도록 강제).
--
-- 권한은 fn_is_admin()으로 매 호출마다 서버가 재검증한다 — UI에서 버튼을
-- 숨기는 것은 편의 기능일 뿐 실제 방어선이 아니다(운영 관리자 RPC와 동일 원칙).
create or replace function public.force_update_shared_message(
    p_message_no integer,
    p_title text,
    p_content text,
    p_update_source public.shared_message_source
)
returns public.shared_messages
language plpgsql
security definer
set search_path = public
as $$
declare
    v_current public.shared_messages%rowtype;
    v_result public.shared_messages%rowtype;
    v_actor_name text;
begin
    if not public.fn_is_admin() then
        raise exception 'PERMISSION_DENIED: 강제 저장은 관리자만 수행할 수 있습니다.';
    end if;

    if p_message_no is null or p_message_no < 1 or p_message_no > 12 then
        raise exception 'INVALID_MESSAGE_NO: message_no는 1~12 사이여야 합니다.';
    end if;

    if p_update_source not in ('migration', 'admin_force') then
        raise exception 'INVALID_UPDATE_SOURCE: update_source는 migration 또는 admin_force여야 합니다.';
    end if;

    select * into v_current from public.shared_messages where message_no = p_message_no for update;
    if not found then
        raise exception 'MESSAGE_NOT_FOUND: message_no=%를 찾을 수 없습니다.', p_message_no;
    end if;

    select coalesce(display_name, email) into v_actor_name
    from public.app_users where id = auth.uid();

    insert into public.shared_message_history (
        message_id, message_no, previous_content, new_content,
        previous_revision, new_revision, changed_by, changed_by_name, changed_from
    ) values (
        v_current.id, p_message_no, v_current.content, p_content,
        v_current.revision, v_current.revision + 1, auth.uid(), v_actor_name, p_update_source
    );

    update public.shared_messages
    set content = p_content,
        title = coalesce(p_title, title),
        revision = v_current.revision + 1,
        is_active = true,
        updated_at = now(),
        updated_by = auth.uid(),
        updated_by_name = v_actor_name,
        update_source = p_update_source
    where message_no = p_message_no
    returning * into v_result;

    return v_result;
end;
$$;

revoke all on function public.force_update_shared_message(integer, text, text, public.shared_message_source) from public;
revoke execute on function public.force_update_shared_message(integer, text, text, public.shared_message_source) from anon;
grant execute on function public.force_update_shared_message(integer, text, text, public.shared_message_source) to authenticated;
-- EXECUTE는 authenticated 전체에 열려 있지만 함수 내부 fn_is_admin() 검사가 최종
-- 방어선이다(admin_* RPC와 동일한 설계 원칙 — docs/sql/phase4_admin_rpc.sql 참고).
-- 일반 직원이 이 함수를 호출하면 항상 PERMISSION_DENIED로 거부된다(테스트로 고정됨).

-- ============================================================
-- 5. RLS 활성화 및 정책
-- ============================================================
alter table public.shared_messages enable row level security;
alter table public.shared_message_history enable row level security;

drop policy if exists shared_messages_select_approved on public.shared_messages;
create policy shared_messages_select_approved
    on public.shared_messages for select
    using (public.fn_is_approved());

-- 중요: shared_messages에는 클라이언트가 직접 쓸 수 있는 UPDATE/INSERT/DELETE
-- 정책이 "하나도 없다" — 이전 버전 초안에는 UPDATE 정책이 있었으나, 그 정책은
-- RPC(OCC/history/서버측 updated_by 계산)를 완전히 우회할 수 있는 구멍이었다
-- (Production Stabilization Sprint에서 발견 및 제거함, 최종 완료 보고서 참고).
-- 이제 이 테이블에 쓰는 유일한 방법은 update_shared_message/
-- force_update_shared_message RPC뿐이다(SECURITY DEFINER, 테이블 소유자 권한으로
-- RLS를 우회) — "메시지 13번 이상 생성 금지"(요구사항 20)도 이 구조로 함께 강제된다.

drop policy if exists shared_messages_update_employee on public.shared_messages;
-- (레거시 정책 제거 — 위 설명 참고. 재적용 시 항상 제거되도록 DROP만 남겨둔다.)

drop policy if exists shared_message_history_select_approved on public.shared_message_history;
create policy shared_message_history_select_approved
    on public.shared_message_history for select
    using (public.fn_is_approved());

-- shared_message_history는 INSERT/UPDATE/DELETE 정책이 없다 = 전원 거부.
-- 유일한 쓰기 경로는 update_shared_message/force_update_shared_message RPC뿐이다.

-- ============================================================
-- 6. Realtime 활성화
-- ============================================================
-- Supabase 프로젝트에는 기본적으로 supabase_realtime publication이 존재한다.
-- 이미 추가되어 있으면 오류가 나므로 존재 여부를 먼저 확인한다(idempotent).
do $$ begin
    if not exists (
        select 1 from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'public'
          and tablename = 'shared_messages'
    ) then
        alter publication supabase_realtime add table public.shared_messages;
    end if;
end $$;

-- Realtime UPDATE 이벤트에는 기본적으로 변경된 컬럼만 포함될 수 있다 — 클라이언트가
-- revision 비교를 위해 전체 행(특히 변경되지 않은 컬럼 포함)을 받도록 REPLICA IDENTITY를
-- FULL로 설정한다.
alter table public.shared_messages replica identity full;

-- ============================================================
-- 7. 적용 후 검증 (수동)
-- ============================================================
-- 이 SQL을 실행한 뒤에는 scripts/check_shared_messages_schema.py를 실행해
-- 아래 항목이 모두 [PASS]인지 확인한다(운영 DB에 쓰기는 하지 않는 read-only 도구):
--   - shared_messages / shared_message_history 테이블 존재
--   - message_no 1~12 존재, 중복 없음
--   - update_shared_message / force_update_shared_message RPC 존재
--   - 현재 로그인 사용자가 SELECT 가능
--   - Realtime publication에 shared_messages 포함
