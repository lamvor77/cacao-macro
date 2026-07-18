# Phase 2B — 역할 기반 접근제어 · Google 로그인 · 이력/복원 설계

**상태: 설계 전용 (코드 미작성, 실제 Supabase 프로젝트에 미적용).**
이 문서는 [PHASE2_CLOUD_SPEC.md](PHASE2_CLOUD_SPEC.md)(Phase 2A — 로컬 서비스 계층)에서
정의한 `messages` 테이블 개념을 역할/인증/이력을 포함하도록 확장한다. Phase 2A에서
구현한 `CloudSyncService`(version 기반 낙관적 잠금, 그룹 발송 스냅샷)는 그대로 재사용되며
변경하지 않는다.

## 정책 요약 (사용자 확정 사항)

- 메시지 수정에 대한 승인 절차, 재확인 팝업, 금액/전화번호/중요항목 변경 경고는 **만들지 않는다.**
- 승인된 editor는 메시지를 직접 수정하고, 저장 즉시 Supabase 현재 값에 반영된다.
- 관리자 개입은 다음 4가지로 한정한다: ① 신규 사용자 최초 승인, ② 역할(viewer/editor/admin) 지정·변경,
  ③ 사용자 차단/재활성화, ④ 필요 시 수정 이력 확인 및 이전 버전 복원.
- 모든 수정은 자동으로 이력에 남는다 (수정자, 시각, 변경 전/후 내용).
- 동시 수정 충돌 감지, 그룹 발송 스냅샷 고정, "발송 중 변경분은 다음 발송부터 적용"은 유지한다
  (Phase 2A에서 이미 구현 완료 — `services/cloud_sync_service.py`, `core/scheduler.py`).
- 기존 로컬 JSON(`storage/*.json`, `DataManager`)은 오프라인 백업으로 유지, PC 매크로 기능은 깨지지 않는다.

---

## 1. Supabase 데이터베이스 스키마 설계

### 1.1 열거형(enum)

```sql
create type app_role as enum ('viewer', 'editor', 'admin');
create type app_user_status as enum ('pending', 'approved', 'blocked');
create type message_source as enum ('mobile', 'pc', 'restore');
```

### 1.2 `app_users` — 사용자 프로필/역할/승인 상태

Supabase Auth의 `auth.users`(Google 로그인 시 자동 생성)와 1:1로 연결되는 프로필 테이블.
"승인"과 "역할"을 분리한다 — 승인 전에는 role 값과 무관하게 접근이 차단된다.

```sql
create table public.app_users (
    id            uuid primary key references auth.users(id) on delete cascade,
    email         text not null,
    display_name  text,
    status        app_user_status not null default 'pending',
    role          app_role not null default 'viewer',
    approved_by   uuid references public.app_users(id),
    approved_at   timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
```

**신규 가입 자동 생성 트리거** — Google 로그인으로 `auth.users`에 행이 생기면 자동으로
`app_users`에 `status='pending'` 행을 만든다 (관리자가 승인하기 전까지 아무 권한 없음).

```sql
create or replace function public.fn_handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.app_users (id, email, display_name)
    values (new.id, new.email, coalesce(new.raw_user_meta_data->>'full_name', new.email));
    return new;
end;
$$;

create trigger trg_on_auth_user_created
after insert on auth.users
for each row execute function public.fn_handle_new_auth_user();
```

### 1.3 `messages` — 메시지 1~12의 현재 상태 (Phase 2A 설계를 확장)

Phase 2A 문서에서 제안했던 `messages` 테이블에 `source`를 추가하고, `updated_by`를
자유 텍스트가 아닌 `app_users.id`(uuid) FK로 바꾼다 — 이력 조회 시 실제 계정과
연결하기 위함이다 (아래 5번 "PC 연동 지점"에서 이유 설명).

```sql
create table public.messages (
    message_number integer primary key check (message_number between 1 and 12),
    text           text not null default '',
    version        integer not null default 1,
    updated_by     uuid not null references public.app_users(id),
    updated_at     timestamptz not null default now(),
    source         message_source not null default 'mobile'
);
```

### 1.4 `message_history` — 자동 이력 (트리거로 기록, 애플리케이션 코드에 의존하지 않음)

"모든 메시지 수정 이력 자동 기록"을 애플리케이션 로직이 아니라 **DB 트리거**로 강제한다.
PC/모바일 어느 경로로 쓰든, 나중에 새 클라이언트가 추가되든 이력 누락이 불가능하다.

```sql
create table public.message_history (
    id               bigint generated always as identity primary key,
    message_number   integer not null check (message_number between 1 and 12),
    text_before      text,              -- 최초 삽입이면 NULL
    text_after       text not null,
    version_before   integer,
    version_after    integer not null,
    updated_by       uuid not null references public.app_users(id),
    updated_by_email text,              -- 계정이 나중에 삭제돼도 이력에서 누가 했는지 보이도록 스냅샷 저장
    updated_at       timestamptz not null default now(),
    source           message_source not null default 'mobile'
);

create index idx_message_history_number on public.message_history(message_number, updated_at desc);
```

```sql
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
        updated_by, updated_by_email, updated_at, source
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
        new.source
    );
    return new;
end;
$$;

create trigger trg_messages_history
after insert or update on public.messages
for each row execute function public.fn_log_message_history();
```

### 1.5 이전 버전 복원

별도의 "복원" 테이블/상태는 두지 않는다. 복원은 **일반 수정과 동일한 경로**로 처리한다 —
`message_history`에서 과거 `text_after`를 골라 `messages.text`에 다시 UPDATE하는 것 뿐이며,
그 UPDATE 자체가 트리거를 통해 새 이력 행(`source='restore'`)으로 남는다. 별도 승인/특수
플로우가 필요 없다.

```sql
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
    select text_after into v_text
    from public.message_history
    where id = p_history_id and message_number = p_message_number;

    if v_text is null then
        raise exception '복원할 이력을 찾을 수 없습니다 (history_id=%, message_number=%)', p_history_id, p_message_number;
    end if;

    select * into v_current from public.messages where message_number = p_message_number for update;

    update public.messages
    set text = v_text,
        version = v_current.version + 1,
        updated_by = auth.uid(),
        updated_at = now(),
        source = 'restore'
    where message_number = p_message_number
    returning * into v_result;

    return v_result;
end;
$$;
```

이 함수는 `security definer`이지만 함수 내부에서 `auth.uid()`를 그대로 `updated_by`로
기록하므로 "누가 복원했는지"가 정확히 남는다.

**최종 결정(2026-07-17): 복원은 admin 전용이다.** `EXECUTE` 권한 자체는 `authenticated`
전체에 열려 있지만, 함수 본문 맨 앞에서 `fn_is_admin()`을 검사해 admin이 아니면 즉시
예외를 던진다 — pending/viewer/editor/blocked는 전부 거부된다. 일반 메시지 수정(messages
테이블 UPDATE)은 여전히 editor도 가능하지만, "이력에서 골라 되돌리는" 이 RPC는 명시적인
관리자 행위로 admin에게만 열어둔다. (초안 검토 단계에서는 "editor도 어차피 같은 내용을
다시 입력해서 저장할 수 있으니 복원도 열어도 된다"는 논리로 editor 이상 허용을 고려했으나,
"관리자 개입 범위 ④ 이전 버전 복원"이라는 정책 문구와 맞추어 admin 전용으로 확정했다.)

---

## 2. Roles 및 RLS 정책 설계

### 2.1 헬퍼 함수 (RLS 재귀 방지를 위해 `security definer`로 분리)

```sql
create or replace function public.fn_is_approved()
returns boolean
language sql stable security definer set search_path = public as $$
    select exists (
        select 1 from public.app_users
        where id = auth.uid() and status = 'approved'
    );
$$;

create or replace function public.fn_current_role()
returns app_role
language sql stable security definer set search_path = public as $$
    select role from public.app_users
    where id = auth.uid() and status = 'approved';
$$;

create or replace function public.fn_is_admin()
returns boolean
language sql stable security definer set search_path = public as $$
    select fn_current_role() = 'admin';
$$;

create or replace function public.fn_can_edit()
returns boolean
language sql stable security definer set search_path = public as $$
    select fn_current_role() in ('editor', 'admin');
$$;
```

### 2.2 `app_users` RLS

```sql
alter table public.app_users enable row level security;

-- 본인 행은 항상 조회 가능 (승인 대기/차단 화면을 보여주기 위해 필요)
create policy app_users_select_self
    on public.app_users for select
    using (id = auth.uid());

-- 승인된 사용자는 전체 사용자 목록을 볼 수 있다 (역할 표시 등 UI 편의). 필요 시 admin 전용으로 좁힐 수 있음.
create policy app_users_select_all_if_admin
    on public.app_users for select
    using (fn_is_admin());

-- 신규 가입 행 자체는 트리거(security definer)가 만들므로 클라이언트발 INSERT 정책은 없음.

-- 승인/역할변경/차단/재활성화는 전부 admin만
create policy app_users_update_admin_only
    on public.app_users for update
    using (fn_is_admin())
    with check (fn_is_admin());
```

> 주의: 자기 자신의 role/status를 스스로 못 바꾸게 하려면 `with check`에
> `id <> auth.uid() or fn_is_admin()` 같은 보강이 필요할 수 있다. 실제 적용 전 검토 필요
> (아래 "미해결 질문" 참고).

### 2.3 `messages` RLS

```sql
alter table public.messages enable row level security;

create policy messages_select_approved
    on public.messages for select
    using (fn_is_approved());

create policy messages_insert_editor
    on public.messages for insert
    with check (fn_can_edit());

create policy messages_update_editor
    on public.messages for update
    using (fn_can_edit())
    with check (fn_can_edit());

-- delete 정책 없음 = 기본적으로 전면 차단 (메시지 삭제는 지원하지 않음, 복원은 UPDATE로 처리)
```

### 2.4 `message_history` RLS

```sql
alter table public.message_history enable row level security;

-- 이력은 승인된 사용자(viewer 포함) 누구나 조회 가능 — 투명성 목적
create policy message_history_select_approved
    on public.message_history for select
    using (fn_is_approved());

-- INSERT는 트리거(security definer)만 수행 — 클라이언트발 직접 INSERT 정책 없음(전면 차단)
```

`fn_restore_message`는 `security definer` + `grant execute on function fn_restore_message to authenticated`
로 노출하되, 함수 맨 앞에 `if not fn_can_edit() then raise exception ... end if;` 가드를
추가해 editor 미만은 호출 자체가 거부되도록 한다 (설계상 추가할 지점, 위 SQL 초안에는
가드 라인을 별도 표시하지 않았으니 실제 작성 시 반영).

---

## 3. Google OAuth 및 최초 사용자 승인 구조

```
[모바일/PC] Google 로그인 버튼
        │  (Supabase Auth, Google OAuth Provider)
        ▼
Supabase Auth: auth.users 행 생성/조회 + 세션(JWT) 발급
        │  trg_on_auth_user_created 트리거
        ▼
public.app_users 행 자동 생성 (status='pending', role='viewer')
        │
        ▼
클라이언트: app_users.status 조회
   ├─ pending  → "관리자 승인 대기 중입니다" 화면만 표시 (그 외 아무 데이터도 못 봄, RLS가 차단)
   ├─ blocked  → "이용이 제한되었습니다" 화면
   └─ approved → role에 따라 메시지 편집(editor/admin) 또는 읽기 전용(viewer) 화면
```

- Supabase 프로젝트 설정에서 Google Provider를 활성화하고, 승인된 redirect URL을 등록해야
  한다 (Supabase 콘솔 설정 — 코드 작업 아님).
- "최초 사용자 승인"은 관리자 화면에서 `status='pending'`인 사용자 목록을 보여주고,
  승인 시 `status='approved'`, `role=관리자가 선택한 값(기본 viewer)`, `approved_by`,
  `approved_at`을 한 번의 UPDATE로 설정한다. 별도의 "승인 요청" 상태 테이블은 만들지 않는다
  (`app_users.status`만으로 충분).
- 거절은 별도 상태로 두지 않고 `blocked`로 통일한다 (승인 후 차단과 로직을 공유).

---

## 4. 메시지 현재값과 수정 이력 구조 (요약 다이어그램)

```
편집자가 메시지 저장
    │
    ▼
messages 테이블 UPDATE (text, version+1, updated_by=auth.uid(), updated_at=now(), source)
    │  RLS: fn_can_edit() 통과해야 함 (editor/admin만)
    │  트리거: trg_messages_history 자동 발동
    ▼
message_history에 새 행 자동 삽입 (변경 전/후, 수정자, 시각)
    │
    ▼
PC/다른 모바일 세션이 pull_messages() 호출 시 최신 text/version 수신
```

- **충돌 감지**: Phase 2A `CloudSyncService.push_messages()`가 이미 구현한 `WHERE version = 예상버전`
  낙관적 잠금을 그대로 사용한다. 이 설계 문서에서 변경할 부분 없음.
- **복원**: `fn_restore_message()` 호출 → 내부적으로 동일한 UPDATE 경로를 타므로 이력에
  자동으로 남고, 다른 클라이언트 입장에서는 "누군가 메시지를 이전 값으로 다시 수정한 것"과
  동일하게 보인다 (재확인 팝업/승인 절차는 여전히 없음). 다만 이 RPC를 호출할 수 있는 주체는
  admin으로 제한된다 — 함수 내부에서 `fn_is_admin()`을 검사해 admin이 아니면 예외를 던진다.

---

## 5. PC 프로그램 연동 지점 설계

### 5.1 핵심 쟁점 — PC 프로그램도 "인증된 사용자"가 되어야 하는가?

Phase 2A의 `SupabaseClientManager`/`CloudSyncService`는 **anon key + RLS 없음(사실상 전체
허용)** 전제로 설계되어 있었다. 이번 역할 기반 접근 제어를 도입하면 `messages` 테이블
쓰기는 `fn_can_edit()`(=로그인된 editor/admin)만 가능하도록 RLS로 막힌다.

**결론(제안): PC 프로그램도 Google 로그인을 통해 인증된 세션으로 Supabase에 접근해야 한다.**
anon key만으로 익명 쓰기를 허용하는 예외를 두면, exe에서 추출 가능한 anon key로 누구나
메시지를 조작할 수 있게 되어 역할 기반 접근 제어 자체가 무력화된다. 이력 테이블의
`updated_by`가 실제 계정을 가리켜야 한다는 요구사항("수정자 계정과 수정 시간 기록")과도
맞지 않는다.

### 5.2 PC 로그인 흐름 (설계, 미구현)

```
관리자/editor가 PC 프로그램에서 "클라우드 로그인" 버튼 클릭
    │
    ▼
로컬에 임시 HTTP 서버(loopback, 예: 127.0.0.1:PORT) 기동
    │
    ▼
시스템 기본 브라우저로 Supabase Auth Google OAuth URL 오픈
    │  (사용자가 브라우저에서 Google 계정으로 로그인)
    ▼
Supabase가 loopback redirect로 authorization code 전달
    │
    ▼
PC 프로그램이 code를 세션(access/refresh token)으로 교환
    │
    ▼
refresh token을 로컬에 암호화 저장 (Windows DPAPI 등 — 평문 저장 금지)
    storage/cloud_sync/ 하위가 아니라 사용자별 보호 경로 고려 (예: %LOCALAPPDATA%)
    │
    ▼
이후 실행 시 저장된 refresh token으로 자동 재로그인 (세션 만료 시에만 재로그인 요구)
```

- 로그인하지 않은 상태(또는 세션 만료 + 갱신 실패)에서는 **경고 팝업이 아니라** 로그
  한 줄과 상태 표시("클라우드 로그인 필요")만 남기고, Phase 2A 원칙대로 **로컬 전용
  모드로 계속 정상 동작**한다 (자동발송/로컬 저장 불가 없음).
- `CloudSyncService.push_messages()`의 `updated_by` 파라미터는 더 이상 자유 텍스트
  device_id가 아니라, 로그인된 세션의 `auth.uid()`(실제 app_users.id)가 된다.
  `SUPABASE_DEVICE_ID`는 계정 식별자가 아니라 `messages.source='pc'`와 함께 남기는
  보조 진단 정보(어느 PC에서 보냈는지) 정도로 역할이 축소된다.
- Phase 2A에서 만든 `SupabaseClientManager`는 "인증된 세션을 가진 client" 개념이
  추가되어야 한다 (`get_client()`가 세션 유무에 따라 다른 상태를 반환하도록 확장) —
  기존 메서드 시그니처를 크게 바꾸지 않고 확장 가능하도록 설계할 것.
- **변경 없음**: `core/scheduler.py`의 그룹 스냅샷/2초 대기/랜덤 딜레이/운영시간, 그리고
  `storage/data_manager.py`(로컬 JSON)는 이번 설계로 인해 전혀 영향받지 않는다.

### 5.3 미해결 질문 (설계 승인 시 확정 필요)

1. **PC 로그인 주체**: PC 프로그램 자체를 하나의 고정 editor/admin 계정으로 로그인시킬지,
   아니면 실행하는 사람마다 개별 Google 계정으로 로그인하게 할지. 전자는 운영이 간단하지만
   "누가 PC에서 발송했는지"가 이력에 구분되지 않는다. 후자는 더 정확하지만 PC를 여러 명이
   함께 쓰는 사무실 환경에서는 매번 로그인/로그아웃이 번거로울 수 있다.
2. **PC 로그인 없이도 로컬 저장은 계속 가능**해야 하는가(=클라우드 push만 스킵)? — 현재
   설계는 "로그인 안 되어 있으면 로컬 JSON 저장/자동발송은 그대로, 클라우드 push만 조용히
   생략"으로 가정했다.

---

## 6. 모바일 관리자 화면 구조 설계

이번 Phase 2B 설계에도 실제 모바일 웹 코드는 작성하지 않는다 — 화면/역할 구조만 정의한다.
기술 스택과 리포지토리 위치는 별도 결정 필요(아래 참고).

### 6.1 화면 목록

| 화면 | 접근 가능 역할 | 설명 |
|---|---|---|
| 로그인 | 전체 | Google 로그인 버튼만 |
| 승인 대기 | `status=pending` | "관리자 승인을 기다리고 있습니다" 안내만, 그 외 기능 없음 |
| 이용 제한 | `status=blocked` | "이용이 제한되었습니다" 안내만 |
| 메시지 편집 | viewer(읽기 전용) / editor·admin(수정 가능) | 그룹 A/B/C/D별 메시지 1~12, editor 이상은 저장 버튼 클릭 시 **재확인 팝업 없이 즉시 저장** |
| 이력 조회 | viewer 이상 (조회만) | 메시지별 수정 이력(수정자/시각/변경 전후), **admin에게만** "이 버전으로 복원" 버튼 노출 (editor/viewer는 조회만 가능) |
| 사용자 관리 (관리자) | admin 전용 | 승인 대기 목록(승인/거절=차단), 전체 사용자 역할 변경, 차단/재활성화 |

### 6.2 편집 화면에 의도적으로 넣지 않는 것

- 저장 전 재확인 팝업, "정말 저장하시겠습니까?" 류 확인창
- 금액/전화번호 패턴 감지 및 강조/경고
- "관리자 승인 대기 중" 같은 수정 상태 배지 (수정은 즉시 반영이므로 그런 상태 자체가 없음)
- 수정 요청/승인 큐, 변경 사항 리뷰 화면

### 6.3 저장소/기술 스택 관련 열린 질문

`cacao_macro`는 Python/CustomTkinter 데스크톱 앱 저장소이며, CLAUDE.md의 프로젝트 구조에는
웹 프론트엔드가 포함되어 있지 않다. 모바일 관리 화면은 보통 React/Next.js 등 별도 스택으로
만들어지므로, **이 저장소 안에 넣기보다 별도 디렉터리(또는 별도 리포지토리)로 분리하는 것을
제안**한다 — CLAUDE.md의 "임의로 프로젝트 구조를 변경하지 않는다" 원칙에 따라, 실제 착수 전
사용자 확인을 받는다.

---

## 7. Phase 2A 대비 변경/영향 요약

| 항목 | Phase 2A 상태 | 이 설계에서의 변경 |
|---|---|---|
| `messages.updated_by` | 자유 텍스트(device_id) | `app_users.id`(uuid) FK — PC도 로그인 필요 |
| RLS | 없음(설계 문서 상 제안만, 미적용) | editor/admin만 쓰기, 승인된 사용자만 읽기 |
| 이력 | 없음 | `message_history` + DB 트리거로 자동 기록 |
| 복원 | 없음 | `fn_restore_message()` — 일반 UPDATE와 동일 경로(승인 절차 없음)지만 **admin 전용**, 내부에서 `fn_is_admin()` 검사 |
| 충돌 감지(version) | 구현 완료 (`CloudSyncService`) | 변경 없음, 그대로 재사용 |
| 그룹 발송 스냅샷 | 구현 완료 (`core/scheduler.py`) | 변경 없음 |
| 로컬 JSON(DataManager) | 유지 | 변경 없음 |

---

## 8. 다음 단계 (이 설계 승인 후)

승인 시 다음 순서로 "작은 단위" 구현을 제안한다 (한 번에 전체 구현하지 않음):

1. 위 SQL을 실제 Supabase 프로젝트에 마이그레이션으로 적용 + RLS 정책 수동 검증
   (다른 역할 계정으로 select/update 시도해보고 차단되는지 확인)
2. `services/supabase_client.py`를 인증 세션을 다루도록 확장 (anon-only → 세션 지원),
   기존 메서드 시그니처는 유지하며 확장
3. PC 데스크톱 로그인 플로우(로컬 loopback OAuth) 구현 — 가장 리스크가 큰 부분이라 별도 검증 필요
4. `CloudSyncService`의 `updated_by`를 device_id 문자열에서 인증된 uuid로 교체
5. 모바일 관리 화면(별도 스택/리포지토리 여부 확정 후) 최소 기능(로그인 → 승인 대기 → 편집)부터 구현
6. 관리자 화면(승인/역할/차단) 구현
7. 이력 조회 + 복원 UI 구현

각 단계마다 결과를 보고하고 승인을 받은 뒤 다음 단계로 진행한다.
