-- Phase 2B — 역할 기반 접근제어 / 이력 / 복원 스키마
-- 상태: 초안, 아직 실제 Supabase 프로젝트에 적용되지 않음(승인 대기).
-- 대상: 새 Supabase 프로젝트(빈 public 스키마)에 적용. 전체 문(statement)이
-- 재실행에도 안전(idempotent)하도록 작성했다 — 중간에 실패해서 다시 실행해도
-- "이미 존재함" 오류로 죽지 않는다 (단, IF NOT EXISTS는 기존 객체의 컬럼/정의가
-- 이 파일과 동일한지까지 검증하지는 않는다 — 부분 적용된 다른 버전이 있다면
-- 수동으로 확인할 것).
--
-- 이 파일은 cacao_macro(PC 매크로) 저장소가 아니라 향후 cacao-message-admin
-- 저장소로 이관될 예정이며, 지금은 검토를 위해 이 저장소의 docs/sql/ 에 초안으로 둔다.

-- ============================================================
-- 0. 확장
-- ============================================================
create extension if not exists pgcrypto;

-- ============================================================
-- 1. 열거형 (CREATE TYPE은 IF NOT EXISTS를 지원하지 않으므로 DO 블록으로 감싼다)
-- ============================================================
do $$ begin
    create type public.app_role as enum ('viewer', 'editor', 'admin');
exception when duplicate_object then
    raise notice '건너뜀: public.app_role 타입이 이미 존재함';
end $$;

do $$ begin
    create type public.app_user_status as enum ('pending', 'approved', 'blocked');
exception when duplicate_object then
    raise notice '건너뜀: public.app_user_status 타입이 이미 존재함';
end $$;

do $$ begin
    create type public.message_source as enum ('mobile', 'pc', 'restore');
exception when duplicate_object then
    raise notice '건너뜀: public.message_source 타입이 이미 존재함';
end $$;

-- ============================================================
-- 2. app_users — 사용자 프로필 / 승인 상태 / 역할
-- ============================================================
create table if not exists public.app_users (
    id            uuid primary key references auth.users(id) on delete cascade,
    email         text not null,
    display_name  text,
    status        public.app_user_status not null default 'pending',
    role          public.app_role not null default 'viewer',
    approved_by   uuid references public.app_users(id),
    approved_at   timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

comment on table public.app_users is
    'Google 로그인 사용자의 승인 상태와 역할. status=approved 이고 role이 editor/admin이어야 messages 쓰기가 가능하다.';

-- Google 로그인으로 auth.users에 새 행이 생기면 app_users에 pending 상태로 자동 등록.
-- SECURITY DEFINER: auth.users에 INSERT할 수 있는 권한이 없는 신규 로그인 사용자 세션이
-- 트리거를 통해 간접적으로 app_users에 자기 행을 만들 수 있어야 하므로 필요하다.
-- search_path를 고정해 다른 스키마의 동명 객체가 끼어드는 것을 막는다 (search_path 하이재킹 방지).
create or replace function public.fn_handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.app_users (id, email, display_name)
    values (
        new.id,
        new.email,
        coalesce(new.raw_user_meta_data ->> 'full_name', new.email)
    )
    on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists trg_on_auth_user_created on auth.users;
create trigger trg_on_auth_user_created
after insert on auth.users
for each row execute function public.fn_handle_new_auth_user();

-- ============================================================
-- 3. messages — 메시지 1~12의 현재 상태
-- ============================================================
create table if not exists public.messages (
    message_number integer primary key check (message_number between 1 and 12),
    text           text not null default '',
    version        integer not null default 1,
    updated_by     uuid not null references public.app_users(id),
    updated_at     timestamptz not null default now(),
    source         public.message_source not null default 'mobile',
    device_id      text  -- source='pc'일 때 SUPABASE_DEVICE_ID(어느 PC인지 진단용). 계정 식별자 아님.
);

comment on column public.messages.updated_by is
    '실제 수정한 계정(app_users.id). RLS에서 auth.uid()와 일치하는지 강제 검증한다 — 클라이언트가 다른 사용자를 사칭해 기록할 수 없다.';
comment on column public.messages.device_id is
    'PC에서 저장한 경우의 SUPABASE_DEVICE_ID (진단용 보조 정보, 계정 대체 아님).';

-- ============================================================
-- 4. message_history — 자동 이력 (트리거로만 기록, 앱 코드가 빼먹을 수 없음)
-- ============================================================
create table if not exists public.message_history (
    id               bigint generated always as identity primary key,
    message_number   integer not null check (message_number between 1 and 12),
    text_before      text,              -- 최초 삽입이면 NULL
    text_after       text not null,
    version_before   integer,
    version_after    integer not null,
    updated_by       uuid not null references public.app_users(id),
    updated_by_email text,              -- 계정이 삭제돼도 이력에서 누구인지 보이도록 스냅샷 저장
    updated_at       timestamptz not null default now(),
    source           public.message_source not null default 'mobile',
    device_id        text
);

create index if not exists idx_message_history_number on public.message_history(message_number, updated_at desc);

-- SECURITY DEFINER: message_history는 클라이언트의 직접 INSERT를 전면 차단하므로
-- (7절 RLS 참고), 이 트리거 함수만이 이력을 쓸 수 있는 유일한 경로다.
create or replace function public.fn_log_message_history()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.message_history (
        message_number, text_before, text_after,
        version_before, version_after,
        updated_by, updated_by_email, updated_at, source, device_id
    )
    values (
        new.message_number,
        case when tg_op = 'UPDATE' then old.text else null end,
        new.text,
        case when tg_op = 'UPDATE' then old.version else null end,
        new.version,
        new.updated_by,
        (select email from public.app_users where id = new.updated_by),
        new.updated_at,
        new.source,
        new.device_id
    );
    return new;
end;
$$;

drop trigger if exists trg_messages_history on public.messages;
create trigger trg_messages_history
after insert or update on public.messages
for each row execute function public.fn_log_message_history();

-- ============================================================
-- 5. RLS 헬퍼 함수 (fn_restore_message보다 먼저 정의 — 내부에서 fn_is_admin()을 참조하므로)
--
-- 재귀 방지: app_users 자체에도 RLS가 걸려 있지만, 이 함수들은 SECURITY DEFINER로
-- "함수 소유자"(이 마이그레이션을 실행하는 postgres/테이블 소유자 역할) 권한으로 실행된다.
-- Postgres는 기본적으로 "테이블 소유자"에게는 그 테이블의 RLS를 적용하지 않으므로
-- (FORCE ROW LEVEL SECURITY를 걸지 않는 한), 함수 내부의 app_users 조회는 RLS를 우회하고
-- 즉시 끝난다 — app_users RLS 정책이 이 함수들을 다시 호출하는 재귀가 발생하지 않는다.
-- ※ 주의: 이 함수들의 소유자를 나중에 다른 역할로 바꾸면(ALTER FUNCTION ... OWNER TO) 이
--   우회가 깨질 수 있으니 소유자를 바꾸지 말 것.
--
-- NULL 방지: fn_current_role()은 미승인/차단 사용자에 대해 NULL을 반환한다. plpgsql의
-- `IF NOT x THEN ... END IF;` 패턴은 x가 NULL이면 THEN 블록을 "실행하지 않는다"
-- (false와 다르게 취급되지 않고 그냥 통과됨) — 즉 그대로 두면 "권한 없음" 가드가
-- 조용히 무력화된다. 그래서 fn_is_admin()/fn_can_edit()은 반드시 COALESCE로 boolean을
-- 강제해 NULL이 나올 수 없게 한다 (이 함수를 호출하는 모든 곳에서 자동으로 안전해짐).
-- ============================================================
create or replace function public.fn_is_approved()
returns boolean
language sql stable security definer set search_path = public as $$
    select exists (
        select 1 from public.app_users
        where id = auth.uid() and status = 'approved'
    );
$$;

create or replace function public.fn_current_role()
returns public.app_role
language sql stable security definer set search_path = public as $$
    select role from public.app_users
    where id = auth.uid() and status = 'approved';
$$;

create or replace function public.fn_is_admin()
returns boolean
language sql stable security definer set search_path = public as $$
    select coalesce(public.fn_current_role() = 'admin', false);
$$;

create or replace function public.fn_can_edit()
returns boolean
language sql stable security definer set search_path = public as $$
    select coalesce(public.fn_current_role() in ('editor', 'admin'), false);
$$;

-- ============================================================
-- 6. 복원 — 일반 UPDATE와 동일한 경로를 타지만, 호출 권한은 admin 전용이다.
--
-- 일반 메시지 수정(messages 테이블에 대한 직접 UPDATE/INSERT)은 approved editor와
-- admin 모두 가능하다 (7절 messages_update_editor 정책 참고, 변경 없음). 반면
-- "과거 이력에서 특정 버전을 골라 현재 값으로 되돌리는" 이 RPC는 admin 전용으로
-- 제한한다 — editor는 여전히 같은 내용을 직접 수정해서 저장할 수 있으므로 실질적으로
-- 못 하게 되는 일은 없고, "복원"이라는 명시적 관리자 행위만 admin으로 좁히는 것이다.
-- ============================================================
create or replace function public.fn_restore_message(
    p_message_number integer,
    p_history_id bigint
)
returns public.messages
language plpgsql
security definer
set search_path = public
as $$
declare
    v_text text;
    v_current public.messages%rowtype;
    v_result public.messages%rowtype;
begin
    -- SECURITY DEFINER 함수는 RLS를 우회하므로, 여기서 반드시 직접 권한을 검사해야
    -- 최종 방어선이 된다 (EXECUTE 권한 자체는 authenticated 전체에 열려 있음 — 아래
    -- GRANT 참고). fn_is_admin()은 COALESCE로 NULL이 나올 수 없게 되어 있으므로
    -- (5절 참고) pending/viewer/editor/blocked 전부에 대해 항상 정확히 예외를 던진다.
    if not public.fn_is_admin() then
        raise exception '이 작업은 admin만 수행할 수 있습니다.';
    end if;

    select text_after into v_text
    from public.message_history
    where id = p_history_id and message_number = p_message_number;

    if v_text is null then
        raise exception '복원할 이력을 찾을 수 없습니다 (history_id=%, message_number=%)', p_history_id, p_message_number;
    end if;

    select * into v_current from public.messages where message_number = p_message_number for update;

    -- updated_by는 항상 호출자 본인(auth.uid())으로 기록된다 — admin이 다른 계정을
    -- 사칭해 복원 이력을 남길 방법이 없다 (messages 테이블의 updated_by=auth.uid() 강제와
    -- 동일한 원칙, 여기서는 RLS가 아니라 이 함수 코드 자체가 강제한다).
    -- source='restore'로 이 변경이 일반 수정이 아니라 복원임을 식별 가능하게 남긴다.
    update public.messages
    set text = v_text,
        version = v_current.version + 1,
        updated_by = auth.uid(),
        updated_at = now(),
        source = 'restore',
        device_id = null
    where message_number = p_message_number
    returning * into v_result;

    return v_result;
end;
$$;

revoke all on function public.fn_restore_message(integer, bigint) from public;
revoke execute on function public.fn_restore_message(integer, bigint) from anon;
grant execute on function public.fn_restore_message(integer, bigint) to authenticated;
-- EXECUTE는 anon을 제외한 모든 로그인 사용자(authenticated)에게 열려 있지만,
-- 함수 본문 맨 앞의 fn_is_admin() 검사가 최종 방어선으로 pending/viewer/editor/blocked를
-- 전부 걸러낸다 — "누가 호출할 수 있는가"가 아니라 "호출했을 때 무엇이 허용되는가"를
-- 함수 내부에서 판단하는 구조다.

-- ============================================================
-- 7. RLS 활성화 및 정책
--
-- 정책은 DROP POLICY IF EXISTS 후 CREATE POLICY로 재실행 안전하게 만든다
-- (CREATE POLICY는 IF NOT EXISTS를 지원하지 않음).
--
-- FORCE ROW LEVEL SECURITY는 어느 테이블에도 걸지 않는다 — 걸면 테이블 소유자에게도
-- RLS가 적용되어 위 5번 섹션의 SECURITY DEFINER 함수들과 트리거(fn_log_message_history 등)의
-- "소유자는 우회" 전제가 깨지고, 이력 자동 기록과 헬퍼 함수 재귀 방지가 동시에 무너진다.
-- ============================================================
alter table public.app_users enable row level security;
alter table public.messages enable row level security;
alter table public.message_history enable row level security;

-- ---- app_users ----
drop policy if exists app_users_select_self on public.app_users;
create policy app_users_select_self
    on public.app_users for select
    using (id = auth.uid());

drop policy if exists app_users_select_all_if_admin on public.app_users;
create policy app_users_select_all_if_admin
    on public.app_users for select
    using (public.fn_is_admin());

drop policy if exists app_users_update_admin_only on public.app_users;
create policy app_users_update_admin_only
    on public.app_users for update
    using (public.fn_is_admin())
    with check (public.fn_is_admin());

-- app_users INSERT/DELETE 정책 없음 = 클라이언트발 직접 insert/delete 전면 차단
-- (신규 가입 행은 trg_on_auth_user_created가 security definer로 생성)

-- ---- messages ----
drop policy if exists messages_select_approved on public.messages;
create policy messages_select_approved
    on public.messages for select
    using (public.fn_is_approved());

-- updated_by = auth.uid() 를 INSERT/UPDATE 양쪽 with check에 강제해, 로그인한 editor라도
-- 다른 계정을 사칭해 updated_by를 기록할 수 없게 한다.
drop policy if exists messages_insert_editor on public.messages;
create policy messages_insert_editor
    on public.messages for insert
    with check (public.fn_can_edit() and updated_by = auth.uid());

drop policy if exists messages_update_editor on public.messages;
create policy messages_update_editor
    on public.messages for update
    using (public.fn_can_edit())
    with check (public.fn_can_edit() and updated_by = auth.uid());

-- messages DELETE 정책 없음 = 삭제 전면 차단 (복원은 UPDATE로 처리)

-- ---- message_history ----
drop policy if exists message_history_select_approved on public.message_history;
create policy message_history_select_approved
    on public.message_history for select
    using (public.fn_is_approved());

-- message_history는 INSERT/UPDATE/DELETE 정책이 하나도 없다 = RLS가 켜진 상태에서
-- 정책이 없으면 해당 명령은 전원 거부된다. 클라이언트(anon/authenticated 어떤 역할이든)는
-- 직접 이력을 쓰거나 고치거나 지울 수 없다 — 유일한 쓰기 경로는 trg_messages_history
-- 트리거(SECURITY DEFINER, 테이블 소유자 권한이라 RLS 자체를 우회)뿐이다.

-- ============================================================
-- 8. 초기 관리자 지정 (수동 실행 — 마이그레이션에 실제 이메일을 넣지 말 것)
-- ============================================================
-- 최초 admin은 SQL로 수동 지정해야 한다(닭과 달걀 문제 — admin이 없으면 아무도
-- app_users를 approve할 수 없음). 아래는 예시이며 실제 이메일로 바꿔서 별도 실행한다.
--
-- update public.app_users
--    set status = 'approved', role = 'admin', approved_at = now()
--  where email = '실제관리자@example.com';
