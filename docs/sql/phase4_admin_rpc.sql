-- Phase 4-1 — 운영 관리자 전용 사용자 관리 RPC + 감사로그
-- 상태: 초안, 아직 실제 Supabase 프로젝트에 적용되지 않음(검토 후 수동 적용 필요).
-- 전제: docs/sql/phase2b_schema.sql이 이미 적용된 프로젝트에 "이어서" 적용한다
-- (app_role/app_user_status enum, app_users 테이블, fn_is_admin() 등 기존 객체를 그대로 사용).
--
-- 이 파일은 여러 번 실행해도 안전하도록(idempotent) 작성했다 — create table/index는
-- IF NOT EXISTS, 함수는 CREATE OR REPLACE, 정책은 DROP POLICY IF EXISTS 후 CREATE POLICY를
-- 사용한다. 단, 기존 데이터 손실 가능성이 있는 DROP TABLE은 어디에도 없다.
--
-- 이 저장소가 아니라 향후 cacao-message-admin 저장소로 이관될 예정이라는
-- phase2b_schema.sql의 전제를 그대로 따른다 — 지금은 검토용 초안으로 여기 둔다.

-- ============================================================
-- 0. app_users 확장 — updated_by 컬럼 추가
-- ============================================================
-- 기존 app_users에는 approved_by/approved_at은 있지만 "마지막으로 이 행을 수정한
-- 관리자"를 기록하는 컬럼이 없었다(messages 테이블의 updated_by와 대응되는 개념이
-- 빠져 있었음). 기존 컬럼/데이터는 건드리지 않고 nullable 컬럼만 추가한다.
alter table public.app_users
    add column if not exists updated_by uuid references public.app_users(id);

comment on column public.app_users.updated_by is
    '이 행을 마지막으로 수정한 관리자(app_users.id). admin_* RPC가 auth.uid()로 채운다 — '
    '클라이언트가 임의의 값을 전달할 수 없다(RPC 파라미터로 받지 않음).';

-- ============================================================
-- 1. admin_audit_logs — 관리자 작업 감사로그
-- ============================================================
create table if not exists public.admin_audit_logs (
    id              uuid primary key default gen_random_uuid(),
    actor_user_id   uuid not null references auth.users(id) on delete restrict,
    target_user_id  uuid references auth.users(id) on delete set null,
    action          text not null check (action in (
        'user_approved',
        'user_blocked',
        'user_unblocked',
        'user_role_changed',
        'user_profile_updated'
    )),
    old_role        public.app_role,
    new_role        public.app_role,
    old_status      public.app_user_status,
    new_status      public.app_user_status,
    reason          text,
    metadata        jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now()
);

comment on table public.admin_audit_logs is
    '관리자 RPC(admin_*)가 실행될 때마다 자동으로 남기는 감사로그. 클라이언트가 직접 '
    'INSERT할 수 없다 — SECURITY DEFINER RPC 내부에서만 기록되는, message_history와 '
    '동일한 패턴(7절 RLS 참고).';

comment on column public.admin_audit_logs.actor_user_id is
    '실제로 이 작업을 수행한 관리자. 항상 auth.uid()로 기록되며 RPC 파라미터로 받지 않는다 '
    '— 클라이언트가 actor를 사칭할 방법이 없다.';

-- actor_user_id: RESTRICT/NO ACTION — 감사로그는 "누가 했는지"가 핵심 기록이므로
--   actor 계정이 삭제되어도(auth.users에서 지워져도) 로그 자체가 actor 정보를
--   잃은 채 남는 것을 막는다(삭제를 막아 감사 추적성을 보존). 이 프로젝트에는
--   현재 auth.users를 직접 참조하는 다른 FK가 없어(app_users만 참조) 새로 정하는
--   방향이라, "감사로그 완전성"을 우선해 RESTRICT를 선택했다.
-- target_user_id: SET NULL — 대상 사용자가 나중에 삭제되더라도(app_users는
--   auth.users에 ON DELETE CASCADE라 같이 지워짐) 그 사용자에게 있었던 관리자
--   작업 이력 자체(누가, 언제, 무엇을 했는지)는 감사 목적상 남아야 하므로 로그
--   행을 지우지 않고 대상만 NULL로 남긴다.
-- auth.users를 직접 참조한 이유: app_users.id가 auth.users(id)를 FK로 참조하고
--   ON DELETE CASCADE이므로, app_users(id)를 참조하면 사용자가 지워질 때 감사로그의
--   target_user_id도 함께 지워지는 문제가 생긴다(SET NULL을 원하는 의도와 충돌).
--   auth.users를 직접 참조하면 이 문제가 없다.

create index if not exists idx_admin_audit_logs_created_at
    on public.admin_audit_logs (created_at desc);
create index if not exists idx_admin_audit_logs_actor
    on public.admin_audit_logs (actor_user_id);
create index if not exists idx_admin_audit_logs_target
    on public.admin_audit_logs (target_user_id);
create index if not exists idx_admin_audit_logs_action
    on public.admin_audit_logs (action);
create index if not exists idx_admin_audit_logs_target_created
    on public.admin_audit_logs (target_user_id, created_at desc);

-- ============================================================
-- 2. admin_audit_logs RLS — admin만 조회, 쓰기는 RPC 경로만
-- ============================================================
alter table public.admin_audit_logs enable row level security;

revoke all on public.admin_audit_logs from anon, authenticated;
grant select on public.admin_audit_logs to authenticated;
-- INSERT/UPDATE/DELETE 권한은 authenticated/anon 어느 쪽에도 주지 않는다 — 설령 RLS
-- 정책이 있어도 테이블 권한(GRANT) 자체가 없으면 시도 자체가 거부된다(이중 방어).
-- admin에게도 직접 INSERT 권한을 주지 않는다 — 감사로그는 오직 아래 admin_* RPC
-- (SECURITY DEFINER, 테이블 소유자 권한으로 RLS를 우회)만 기록할 수 있다.

drop policy if exists admin_audit_logs_select_admin_only on public.admin_audit_logs;
create policy admin_audit_logs_select_admin_only
    on public.admin_audit_logs for select
    using (public.fn_is_admin());

-- INSERT/UPDATE/DELETE 정책을 하나도 만들지 않는다 = RLS가 켜진 상태에서 이 명령들은
-- (SECURITY DEFINER 함수를 통한 우회를 제외하면) 어떤 역할에게도 전면 거부된다
-- (message_history 테이블과 완전히 동일한 패턴 — phase2b_schema.sql 4절 참고).

-- ============================================================
-- 3. 공통 관리자 검증 — 기존 fn_is_admin() 재사용
-- ============================================================
-- fn_is_admin()은 fn_current_role()을 거치는데, fn_current_role()은 이미
--   select role from app_users where id = auth.uid() and status = 'approved'
-- 로 정의되어 있다(phase2b_schema.sql 5절) — status가 'approved'가 아니면 NULL을
-- 반환하고, fn_is_admin()의 COALESCE가 이를 false로 바꾼다. 즉 fn_is_admin() = true는
-- 이미 다음 세 조건을 전부 만족한다:
--   1) auth.uid() is not null (NULL이면 app_users 조회 결과가 없어 NULL → false)
--   2) 현재 사용자의 app_users.status = 'approved'
--   3) 현재 사용자의 app_users.role = 'admin'
-- 따라서 별도의 fn_is_approved_admin() 헬퍼는 불필요하다 — 기존 함수의 의미를
-- 바꾸지 않고 그대로 재사용한다(중복 함수를 만들지 않는다는 원칙).

-- 마지막 approved admin 보호를 위한 카운트 헬퍼. authenticated에게 직접 EXECUTE를
-- 주지 않는다(아래 admin_* RPC 내부에서만 호출 — SECURITY DEFINER 함수 간 호출은
-- 함수 소유자 권한으로 실행되므로 내부 호출 자체는 EXECUTE grant와 무관하게 항상 가능하다).
create or replace function public.fn_approved_admin_count()
returns integer
language sql stable security definer set search_path = public as $$
    select count(*)::integer from public.app_users
    where role = 'admin' and status = 'approved';
$$;

-- p_target_user_id를 차단/강등하면 "승인된 admin이 0명"이 되는지 확인하고,
-- 그렇다면 예외를 던진다. 호출부(admin_block_user/admin_update_user_role)가
-- 이 함수를 부르기 "전에" 반드시 pg_advisory_xact_lock으로 잠가야 한다 —
-- 그렇지 않으면 "count 조회 → 아직 안전하다고 판단 → UPDATE" 사이에 다른 트랜잭션이
-- 끼어들어 두 admin이 동시에 서로를(또는 마지막 admin을) 무력화하는 race condition이
-- 생길 수 있다(예: admin이 2명일 때 둘이 동시에 서로를 차단 시도 → 둘 다 "상대방을
-- 빼면 1명 남으니 안전하다"고 각자 판단해버리는 상황).
create or replace function public.fn_assert_not_last_admin(p_target_user_id uuid)
returns void
language plpgsql security definer set search_path = public as $$
declare
    v_target public.app_users%rowtype;
begin
    select * into v_target from public.app_users where id = p_target_user_id;
    if v_target.role = 'admin' and v_target.status = 'approved' then
        if public.fn_approved_admin_count() <= 1 then
            raise exception 'LAST_ADMIN_PROTECTED: 마지막 승인된 관리자는 차단/강등할 수 없습니다.';
        end if;
    end if;
end;
$$;

revoke all on function public.fn_approved_admin_count() from public, anon, authenticated;
revoke all on function public.fn_assert_not_last_admin(uuid) from public, anon, authenticated;
-- fn_assert_not_last_admin(uuid)는 임의의 target uuid를 받으므로, 이걸 그대로
-- authenticated에게 노출하면 "이 UUID가 지금 마지막 admin인가?"를 무단으로 조회하는
-- 용도로 악용될 수 있다(정보 노출) — 그래서 fn_is_admin() 같은 무인자 predicate와
-- 달리 명시적으로 EXECUTE를 회수한다.

-- ============================================================
-- 4. 관리자 사용자 목록 조회
-- ============================================================
create or replace function public.admin_list_users(
    p_status text default null,
    p_role text default null,
    p_search text default null,
    p_limit integer default 100,
    p_offset integer default 0
)
returns table (
    id uuid,
    email text,
    display_name text,
    role public.app_role,
    status public.app_user_status,
    approved_at timestamptz,
    created_at timestamptz,
    updated_at timestamptz,
    updated_by uuid
)
language plpgsql security definer set search_path = public as $$
declare
    v_status public.app_user_status;
    v_role public.app_role;
    v_limit integer;
    v_offset integer;
begin
    if not public.fn_is_admin() then
        raise exception 'ADMIN_REQUIRED: 관리자만 사용할 수 있습니다.';
    end if;

    if p_status is not null then
        begin
            v_status := p_status::public.app_user_status;
        exception when invalid_text_representation then
            raise exception 'INVALID_STATUS: 알 수 없는 status 값입니다: %', p_status;
        end;
    end if;

    if p_role is not null then
        begin
            v_role := p_role::public.app_role;
        exception when invalid_text_representation then
            raise exception 'INVALID_ROLE: 알 수 없는 role 값입니다: %', p_role;
        end;
    end if;

    -- limit/offset은 여기서 에러 대신 안전한 범위로 clamp한다(방어적 backstop) —
    -- 엄격한 거부는 Python AdminService가 RPC 호출 "전에" 이미 수행한다(이중 방어).
    v_limit := least(greatest(coalesce(p_limit, 100), 1), 200);
    v_offset := greatest(coalesce(p_offset, 0), 0);

    -- p_search는 텍스트 이어붙이기가 아니라 plpgsql 바인드 변수로 쿼리 플랜에
    -- 전달되는 정적 SQL이다(EXECUTE로 문자열을 조립해 실행하는 동적 SQL이 아님) —
    -- SQL 인젝션 경로가 없다.
    return query
        select u.id, u.email, u.display_name, u.role, u.status,
               u.approved_at, u.created_at, u.updated_at, u.updated_by
        from public.app_users u
        where (v_status is null or u.status = v_status)
          and (v_role is null or u.role = v_role)
          and (
                p_search is null or p_search = '' or
                u.email ilike '%' || p_search || '%' or
                u.display_name ilike '%' || p_search || '%'
              )
        order by u.created_at desc
        limit v_limit offset v_offset;
end;
$$;

revoke all on function public.admin_list_users(text, text, text, integer, integer) from public;
revoke execute on function public.admin_list_users(text, text, text, integer, integer) from anon;
grant execute on function public.admin_list_users(text, text, text, integer, integer) to authenticated;

-- ============================================================
-- 5. 사용자 승인
-- ============================================================
-- 정책: pending → approved는 허용한다. blocked → approved는 admin_unblock_user의
-- 의미와 겹치므로 이 함수에서는 명시적으로 금지한다(TARGET_BLOCKED) — 차단
-- 해제는 반드시 admin_unblock_user를 쓰도록 강제해, "차단 해제"라는 관리자
-- 행위가 감사로그에 정확한 action(user_unblocked)으로 남게 한다.
-- no-op 정책: 이미 status='approved'이고 role도 요청한 값과 같으면 아무 것도
-- 바꾸지 않고 현재 행을 그대로 반환한다 — 감사로그도 남기지 않는다(실제 변경이
-- 없는 호출까지 로그로 남기면 의미 있는 변경 이력을 찾기 어려워지기 때문).
create or replace function public.admin_approve_user(
    p_target_user_id uuid,
    p_role text default 'viewer',
    p_reason text default null
)
returns table (
    id uuid, email text, display_name text, role public.app_role, status public.app_user_status,
    approved_at timestamptz, created_at timestamptz, updated_at timestamptz, updated_by uuid
)
language plpgsql security definer set search_path = public as $$
declare
    v_role public.app_role;
    v_before public.app_users%rowtype;
    v_after public.app_users%rowtype;
    v_reason text;
    v_caller_id uuid;
begin
    v_caller_id := auth.uid();
    if not public.fn_is_admin() then
        raise exception 'ADMIN_REQUIRED: 관리자만 사용할 수 있습니다.';
    end if;

    begin
        v_role := coalesce(p_role, 'viewer')::public.app_role;
    exception when invalid_text_representation then
        raise exception 'INVALID_ROLE: 알 수 없는 role 값입니다: %', p_role;
    end;

    select * into v_before from public.app_users where id = p_target_user_id for update;
    if not found then
        raise exception 'TARGET_USER_NOT_FOUND: 대상 사용자를 찾을 수 없습니다.';
    end if;

    if v_before.status = 'blocked' then
        raise exception 'TARGET_BLOCKED: 차단된 사용자는 admin_unblock_user를 사용하세요.';
    end if;

    if v_before.status = 'approved' and v_before.role = v_role then
        return query select v_before.id, v_before.email, v_before.display_name, v_before.role,
                             v_before.status, v_before.approved_at, v_before.created_at,
                             v_before.updated_at, v_before.updated_by;
        return;
    end if;

    v_reason := nullif(trim(both from coalesce(p_reason, '')), '');
    if v_reason is not null and length(v_reason) > 500 then
        v_reason := left(v_reason, 500);
    end if;

    update public.app_users
    set status = 'approved',
        role = v_role,
        approved_at = coalesce(approved_at, now()),
        updated_at = now(),
        updated_by = v_caller_id
    where id = p_target_user_id
    returning * into v_after;

    insert into public.admin_audit_logs (
        actor_user_id, target_user_id, action,
        old_role, new_role, old_status, new_status, reason
    ) values (
        v_caller_id, p_target_user_id, 'user_approved',
        v_before.role, v_after.role, v_before.status, v_after.status, v_reason
    );

    return query select v_after.id, v_after.email, v_after.display_name, v_after.role,
                         v_after.status, v_after.approved_at, v_after.created_at,
                         v_after.updated_at, v_after.updated_by;
end;
$$;

revoke all on function public.admin_approve_user(uuid, text, text) from public;
revoke execute on function public.admin_approve_user(uuid, text, text) from anon;
grant execute on function public.admin_approve_user(uuid, text, text) to authenticated;

-- ============================================================
-- 6. 사용자 차단
-- ============================================================
create or replace function public.admin_block_user(
    p_target_user_id uuid,
    p_reason text default null
)
returns table (
    id uuid, email text, display_name text, role public.app_role, status public.app_user_status,
    approved_at timestamptz, created_at timestamptz, updated_at timestamptz, updated_by uuid
)
language plpgsql security definer set search_path = public as $$
declare
    v_before public.app_users%rowtype;
    v_after public.app_users%rowtype;
    v_reason text;
    v_caller_id uuid;
begin
    v_caller_id := auth.uid();
    if not public.fn_is_admin() then
        raise exception 'ADMIN_REQUIRED: 관리자만 사용할 수 있습니다.';
    end if;

    if p_target_user_id = v_caller_id then
        raise exception 'SELF_BLOCK_FORBIDDEN: 자기 자신을 차단할 수 없습니다.';
    end if;

    -- 동시성 보호(원칙 8 참고): count 조회와 UPDATE 사이의 race를 막기 위해
    -- 트랜잭션 범위 advisory lock을 먼저 잡는다. 같은 이름의 잠금을 쓰는 다른
    -- admin_* RPC(admin_update_user_role)와 서로 직렬화된다.
    perform pg_advisory_xact_lock(hashtext('app_users_admin_guard'));

    select * into v_before from public.app_users where id = p_target_user_id for update;
    if not found then
        raise exception 'TARGET_USER_NOT_FOUND: 대상 사용자를 찾을 수 없습니다.';
    end if;

    if v_before.status = 'blocked' then
        -- 이미 차단됨 — no-op, 감사로그 미기록
        return query select v_before.id, v_before.email, v_before.display_name, v_before.role,
                             v_before.status, v_before.approved_at, v_before.created_at,
                             v_before.updated_at, v_before.updated_by;
        return;
    end if;

    perform public.fn_assert_not_last_admin(p_target_user_id);

    v_reason := nullif(trim(both from coalesce(p_reason, '')), '');
    if v_reason is not null and length(v_reason) > 500 then
        v_reason := left(v_reason, 500);
    end if;

    update public.app_users
    set status = 'blocked', updated_at = now(), updated_by = v_caller_id
    where id = p_target_user_id
    returning * into v_after;

    insert into public.admin_audit_logs (
        actor_user_id, target_user_id, action,
        old_role, new_role, old_status, new_status, reason
    ) values (
        v_caller_id, p_target_user_id, 'user_blocked',
        v_before.role, v_after.role, v_before.status, v_after.status, v_reason
    );

    return query select v_after.id, v_after.email, v_after.display_name, v_after.role,
                         v_after.status, v_after.approved_at, v_after.created_at,
                         v_after.updated_at, v_after.updated_by;
end;
$$;

revoke all on function public.admin_block_user(uuid, text) from public;
revoke execute on function public.admin_block_user(uuid, text) from anon;
grant execute on function public.admin_block_user(uuid, text) to authenticated;

-- ============================================================
-- 7. 사용자 차단 해제
-- ============================================================
-- 정책: blocked 상태인 사용자만 대상이다. blocked가 아니면 조용한 no-op이
-- 아니라 명시적 오류(USER_NOT_BLOCKED)로 통일한다 — "차단 해제"라는 이름의
-- 작업을 차단되지 않은 사용자에게 호출하는 것은 호출부의 상태 추적 오류일
-- 가능성이 높으므로, 조용히 넘어가기보다 드러내는 쪽을 택했다.
create or replace function public.admin_unblock_user(
    p_target_user_id uuid,
    p_restore_status text default 'approved',
    p_reason text default null
)
returns table (
    id uuid, email text, display_name text, role public.app_role, status public.app_user_status,
    approved_at timestamptz, created_at timestamptz, updated_at timestamptz, updated_by uuid
)
language plpgsql security definer set search_path = public as $$
declare
    v_restore public.app_user_status;
    v_before public.app_users%rowtype;
    v_after public.app_users%rowtype;
    v_reason text;
    v_caller_id uuid;
begin
    v_caller_id := auth.uid();
    if not public.fn_is_admin() then
        raise exception 'ADMIN_REQUIRED: 관리자만 사용할 수 있습니다.';
    end if;

    begin
        v_restore := coalesce(p_restore_status, 'approved')::public.app_user_status;
    exception when invalid_text_representation then
        raise exception 'INVALID_STATUS: 알 수 없는 status 값입니다: %', p_restore_status;
    end;

    if v_restore not in ('approved', 'pending') then
        raise exception 'INVALID_STATUS: restore_status는 approved 또는 pending만 허용됩니다.';
    end if;

    select * into v_before from public.app_users where id = p_target_user_id for update;
    if not found then
        raise exception 'TARGET_USER_NOT_FOUND: 대상 사용자를 찾을 수 없습니다.';
    end if;

    if v_before.status <> 'blocked' then
        raise exception 'USER_NOT_BLOCKED: 차단 상태가 아닌 사용자는 차단 해제할 수 없습니다.';
    end if;

    v_reason := nullif(trim(both from coalesce(p_reason, '')), '');
    if v_reason is not null and length(v_reason) > 500 then
        v_reason := left(v_reason, 500);
    end if;

    -- approved_at 정책: approved로 복원할 때 기존 승인일이 있으면 그대로 유지하고,
    -- 없으면(한 번도 승인된 적 없이 pending에서 바로 blocked된 특이 케이스) now()로 채운다.
    -- pending으로 복원할 때는 approved_at을 건드리지 않는다(승인 이력 자체를 지우지 않음).
    update public.app_users
    set status = v_restore,
        approved_at = case when v_restore = 'approved' then coalesce(approved_at, now()) else approved_at end,
        updated_at = now(),
        updated_by = v_caller_id
    where id = p_target_user_id
    returning * into v_after;

    insert into public.admin_audit_logs (
        actor_user_id, target_user_id, action,
        old_role, new_role, old_status, new_status, reason
    ) values (
        v_caller_id, p_target_user_id, 'user_unblocked',
        v_before.role, v_after.role, v_before.status, v_after.status, v_reason
    );

    return query select v_after.id, v_after.email, v_after.display_name, v_after.role,
                         v_after.status, v_after.approved_at, v_after.created_at,
                         v_after.updated_at, v_after.updated_by;
end;
$$;

revoke all on function public.admin_unblock_user(uuid, text, text) from public;
revoke execute on function public.admin_unblock_user(uuid, text, text) from anon;
grant execute on function public.admin_unblock_user(uuid, text, text) to authenticated;

-- ============================================================
-- 8. 역할 변경
-- ============================================================
-- 정책 문서화:
--   1) 자기 자신의 admin 권한 제거 금지 — 호출자 본인이 대상이고 자신이 이미
--      admin(fn_is_admin() true 통과 = role='admin' 보장)인데 admin이 아닌
--      역할로 바꾸려 하면 무조건 거부한다. 이 규칙은 "마지막 admin인지"와
--      무관하게 항상 적용된다(다른 admin이 더 있어도 자기 자신을 강등할 수 없음).
--   2) 마지막 approved admin 강등 금지 — 대상이 role='admin' and status='approved'이고
--      새 role이 admin이 아니며, 승인된 admin이 1명뿐이면 거부한다.
--   3) blocked 사용자의 role 변경: 허용한다(status는 blocked로 유지). 예를 들어
--      차단된 사용자를 나중에 어떤 역할로 해제할지 미리 정해두는 용도로 쓸 수 있다
--      — status와 role은 서로 독립적인 필드로 취급한다.
--   4) pending 사용자를 admin으로 role만 먼저 바꾸는 것은 허용한다 — 단
--      fn_is_admin()/AppUserProfile.is_admin 둘 다 status='approved'를 함께
--      요구하므로, 실제 관리자 권한은 이후 admin_approve_user로 승인되기 전까지
--      전혀 활성화되지 않는다(role 변경과 권한 활성화는 서로 다른 사건).
create or replace function public.admin_update_user_role(
    p_target_user_id uuid,
    p_new_role text,
    p_reason text default null
)
returns table (
    id uuid, email text, display_name text, role public.app_role, status public.app_user_status,
    approved_at timestamptz, created_at timestamptz, updated_at timestamptz, updated_by uuid
)
language plpgsql security definer set search_path = public as $$
declare
    v_new_role public.app_role;
    v_before public.app_users%rowtype;
    v_after public.app_users%rowtype;
    v_reason text;
    v_caller_id uuid;
begin
    v_caller_id := auth.uid();
    if not public.fn_is_admin() then
        raise exception 'ADMIN_REQUIRED: 관리자만 사용할 수 있습니다.';
    end if;

    begin
        v_new_role := p_new_role::public.app_role;
    exception when invalid_text_representation then
        raise exception 'INVALID_ROLE: 알 수 없는 role 값입니다: %', p_new_role;
    end;

    if p_target_user_id = v_caller_id and v_new_role <> 'admin' then
        raise exception 'SELF_DEMOTION_FORBIDDEN: 자기 자신의 관리자 권한을 제거할 수 없습니다.';
    end if;

    perform pg_advisory_xact_lock(hashtext('app_users_admin_guard'));

    select * into v_before from public.app_users where id = p_target_user_id for update;
    if not found then
        raise exception 'TARGET_USER_NOT_FOUND: 대상 사용자를 찾을 수 없습니다.';
    end if;

    if v_before.role = v_new_role then
        return query select v_before.id, v_before.email, v_before.display_name, v_before.role,
                             v_before.status, v_before.approved_at, v_before.created_at,
                             v_before.updated_at, v_before.updated_by;
        return;
    end if;

    if v_before.role = 'admin' and v_new_role <> 'admin' then
        perform public.fn_assert_not_last_admin(p_target_user_id);
    end if;

    v_reason := nullif(trim(both from coalesce(p_reason, '')), '');
    if v_reason is not null and length(v_reason) > 500 then
        v_reason := left(v_reason, 500);
    end if;

    update public.app_users
    set role = v_new_role, updated_at = now(), updated_by = v_caller_id
    where id = p_target_user_id
    returning * into v_after;

    insert into public.admin_audit_logs (
        actor_user_id, target_user_id, action,
        old_role, new_role, old_status, new_status, reason
    ) values (
        v_caller_id, p_target_user_id, 'user_role_changed',
        v_before.role, v_after.role, v_before.status, v_after.status, v_reason
    );

    return query select v_after.id, v_after.email, v_after.display_name, v_after.role,
                         v_after.status, v_after.approved_at, v_after.created_at,
                         v_after.updated_at, v_after.updated_by;
end;
$$;

revoke all on function public.admin_update_user_role(uuid, text, text) from public;
revoke execute on function public.admin_update_user_role(uuid, text, text) from anon;
grant execute on function public.admin_update_user_role(uuid, text, text) to authenticated;

-- ============================================================
-- 9. 감사로그 조회
-- ============================================================
create or replace function public.admin_list_audit_logs(
    p_target_user_id uuid default null,
    p_action text default null,
    p_limit integer default 100,
    p_offset integer default 0
)
returns table (
    id uuid,
    actor_user_id uuid,
    actor_email text,
    target_user_id uuid,
    target_email text,
    action text,
    old_role public.app_role,
    new_role public.app_role,
    old_status public.app_user_status,
    new_status public.app_user_status,
    reason text,
    metadata jsonb,
    created_at timestamptz
)
language plpgsql security definer set search_path = public as $$
declare
    v_limit integer;
    v_offset integer;
begin
    if not public.fn_is_admin() then
        raise exception 'ADMIN_REQUIRED: 관리자만 사용할 수 있습니다.';
    end if;

    v_limit := least(greatest(coalesce(p_limit, 100), 1), 200);
    v_offset := greatest(coalesce(p_offset, 0), 0);

    -- app_users를 LEFT JOIN한다(auth.users를 직접 SELECT하지 않음) — 삭제된
    -- 사용자는 app_users도 함께 사라지므로(ON DELETE CASCADE) *_email이 NULL로
    -- 나올 수 있으며, 그래도 로그 자체(uuid/action/old·new 값)는 그대로 보존된다.
    return query
        select l.id, l.actor_user_id, actor.email as actor_email,
               l.target_user_id, target.email as target_email,
               l.action, l.old_role, l.new_role, l.old_status, l.new_status,
               l.reason, l.metadata, l.created_at
        from public.admin_audit_logs l
        left join public.app_users actor on actor.id = l.actor_user_id
        left join public.app_users target on target.id = l.target_user_id
        where (p_target_user_id is null or l.target_user_id = p_target_user_id)
          and (p_action is null or l.action = p_action)
        order by l.created_at desc
        limit v_limit offset v_offset;
end;
$$;

revoke all on function public.admin_list_audit_logs(uuid, text, integer, integer) from public;
revoke execute on function public.admin_list_audit_logs(uuid, text, integer, integer) from anon;
grant execute on function public.admin_list_audit_logs(uuid, text, integer, integer) to authenticated;

-- ============================================================
-- 10. app_users 직접 UPDATE 차단 — 관리자 변경은 반드시 admin_* RPC를 통과해야 한다
-- ============================================================
-- 기존 app_users_update_admin_only 정책(phase2b_schema.sql 7절)은 fn_is_admin()인
-- 클라이언트가 app_users를 "직접" UPDATE할 수 있게 허용하고 있었다 — 이 경로로
-- 수정하면 위 admin_* RPC의 감사로그 기록과 마지막 admin 보호(advisory lock 포함)를
-- 전부 우회할 수 있다.
--
-- services/, tests/ 전체를 대상으로 app_users에 대한 직접 UPDATE 호출을 검색했고
-- (grep -rn "app_users" services/), services/auth_service.py의
-- get_app_user_profile()이 SELECT만 수행할 뿐 UPDATE를 사용하는 코드는 어디에도
-- 없음을 확인했다 — 따라서 이 정책을 안전하게 제거할 수 있다.
drop policy if exists app_users_update_admin_only on public.app_users;
-- app_users에 UPDATE 정책이 하나도 남지 않으므로, RLS가 켜진 상태에서 UPDATE는
-- (SECURITY DEFINER RPC 경로를 제외하면) 어떤 역할에게도 전면 거부된다. admin_* RPC는
-- SECURITY DEFINER로 테이블 소유자 권한으로 실행되어 이 RLS 자체를 우회하므로
-- 계속 정상 동작한다(5절 헬퍼 함수들과 동일한 우회 원리 — phase2b_schema.sql 5절 참고).
-- app_users_select_self/app_users_select_all_if_admin(SELECT 정책)은 그대로 유지한다
-- — 이번 변경은 UPDATE 경로만 막는다.

-- ============================================================
-- 11. 초기 관리자 지정 (참고 — phase2b_schema.sql 8절과 동일한 패턴)
-- ============================================================
-- update public.app_users
--    set role = 'admin', status = 'approved', approved_at = now()
--  where email = '실제관리자@example.com';
