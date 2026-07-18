# Phase 2 클라우드 동기화 — 설계 문서

Phase 2A 구현 범위: 클라우드 동기화의 "기반 계층"만 구현한다.
GUI(main_window.py)와의 연결, 모바일 웹, 인증/권한 관리는 포함하지 않는다.

> **진행 현황**: 이 문서는 Phase 2A(로컬 서비스 계층) 시점에 작성되었다.
> 역할/RLS/이력은 [PHASE2B_ROLES_AUTH_SPEC.md](PHASE2B_ROLES_AUTH_SPEC.md)에서,
> `MainWindow`/`DataManager`와의 실제 연결(로컬 우선 로드, 폴링, 충돌 UI 상태
> 등)은 [PHASE2C_DESKTOP_INTEGRATION.md](PHASE2C_DESKTOP_INTEGRATION.md)에서
> 다룬다. 아래 "Phase 2B에서 진행 예정"이라고 적힌 항목은 대부분 Phase 2C에서
> 실제로 구현되었다 — 표시는 기록 보존을 위해 원문 그대로 남겨둔다.

---

## 1. 핵심 원칙

1. 기존 PC 카카오톡 매크로 기능(카톡방 열기/발송/자동발송)은 절대 깨지지 않는다.
2. `storage/data_manager.py`(DataManager)의 로컬 JSON 저장 기능과 메서드
   시그니처는 그대로 유지한다. 클라우드는 그 위에 얹는 "추가 동기화 계층"이다.
3. `SUPABASE_ENABLED=False`(기본값)이면 프로그램은 기존과 100% 동일하게 동작한다.
4. 인터넷/Supabase 장애가 발생해도 로컬 JSON 저장과 PC 자동발송은 정상 동작해야 한다.
5. Supabase 네트워크 요청은 GUI 메인 스레드와 자동발송 스레드를 절대 막지 않는다
   (호출부가 별도 스레드에서 호출해야 한다 — 기존 `MainWindow._run_in_thread` 패턴 재사용 예정).
6. `service_role` key는 PC 프로그램/모바일 웹 어디에도 포함하지 않는다. `anon` key만 사용하고,
   Supabase 프로젝트에는 Row Level Security(RLS)를 반드시 적용한다.

---

## 2. 동기화 정책 — "클라우드 최신 우선", 단방향 push 아님

- Supabase의 메시지 데이터를 **클라우드 기준 최신 데이터**로 취급한다.
- PC 로컬 JSON은 **오프라인 캐시이자 장애 대비 백업**으로 계속 유지된다.
- 동작 순서(설계 목표, Phase 2B에서 GUI에 연결 예정):
  1. PC 프로그램 시작 시 로컬 JSON으로 기존 기능을 즉시 사용할 수 있어야 한다.
  2. 클라우드 동기화가 활성화되어 있고 인터넷 연결이 가능하면 Supabase의 최신
     메시지를 가져온다 (`pull_messages()`).
  3. Supabase 데이터가 로컬보다 최신이면 PC 메시지 입력창에 반영한다.
  4. PC에서 메시지를 수정하고 저장하면 **로컬 JSON에 먼저 저장한 뒤** Supabase에도
     업로드한다 (`push_messages()`). 로컬 저장 성공 여부는 클라우드 업로드 성공
     여부와 무관하게 보장된다.
  5. 모바일에서 향후 수정된 데이터는 Supabase를 통해 PC로 내려받는다 (Phase 2B 이후).

### 충돌 처리

- 각 메시지(1~12)에 `updated_at`, `updated_by`, `version`을 둔다.
- `push_messages()`는 마지막으로 `pull_messages()`한 시점의 `version`을 "예상 버전"으로
  기억해두고, 업로드 시 `WHERE version = 예상버전` 조건으로 갱신한다(낙관적 잠금).
- 조건이 일치하지 않으면(=그 사이 다른 기기가 먼저 수정) **덮어쓰지 않고** 해당 메시지
  번호를 `conflicts` 목록에 담아 반환한다.
- 충돌은 자동으로 어느 한쪽을 덮어쓰지 않는다. 로그와 사용자 알림이 우선이며,
  충돌 해결 UI는 Phase 2B 이후 과제다.

---

## 3. 발송 중 메시지 변경 처리

`core/scheduler.py`의 `AutoScheduler._send_group()`은 그룹(A/B/C/D) 발송을 시작할 때
`get_messages_fn()`을 호출해 해당 그룹의 메시지 3개를 **불변 튜플 스냅샷**으로 고정한다.
이 스냅샷은 그 그룹의 모든 체크된 단톡방에 대한 발송이 끝날 때까지 그대로 사용되며,
그룹 도중에는 `get_messages_fn()`을 다시 호출하지 않는다.

따라서:

- 그룹 발송 도중 UI(또는 향후 클라우드 pull)로 메시지가 바뀌어도 **현재 진행 중인
  그룹에는 영향이 없다.**
- 다음 그룹(또는 다음 회차의 동일 그룹) 발송이 시작될 때 새 스냅샷을 뜨므로
  **그 다음 발송부터 자동으로 최신 메시지가 적용된다.**
- 기존 단톡방 기준 순차 발송 순서, 메시지 간 2초 대기, 단톡방 이동 랜덤 딜레이(0.5~1.5초),
  운영시간(08:00~19:00)과 매시 0/15/30/45분 스케줄, 중복 실행 방지(`_last_sent_id`)는
  전혀 변경하지 않았다.

클라우드 변경 알림이 도착했을 때(Phase 2B에서 실제로 호출될 지점) 스케줄러에
`AutoScheduler.notify_cloud_update()`를 호출하면, 그룹 발송 중일 경우:

```
[INFO] 클라우드 수정사항 수신 — 현재 그룹 종료 후 다음 발송부터 적용
```

그룹 발송 중이 아닐 경우:

```
[INFO] 클라우드 수정사항 수신 — 다음 발송부터 적용
```

이 로그를 남긴다. 실제 반영은 위 스냅샷 메커니즘으로 이미 보장되므로, 이 메서드는
"왜 즉시 반영되지 않는지"를 사용자에게 알리는 역할만 한다.

---

## 4. 아키텍처 — Phase 2A에서 만든 것

```
config/cloud_settings.py       환경변수(.env) → CloudConfig 로딩
        │
        ▼
services/supabase_client.py    SupabaseClientManager
                                 - get_client() / check_connection()
                                 - 실패 시 ClientResult(success=False, ...)로 반환
        │
        ▼
services/cloud_sync_service.py CloudSyncService
                                 - is_enabled() / check_connection()
                                 - pull_messages() → SyncResult(messages=...)
                                 - push_messages(dict) → SyncResult(updated=, conflicts=)
                                 - get_sync_status() → SyncStatus (네트워크 요청 없음)
                                 - 충돌 감지용 로컬 캐시: storage/cloud_sync/message_cache.json
```

- `CloudSyncService`는 **UI 컴포넌트를 import하지 않는다.** 입출력은 모두
  `dict[int, str]` / dataclass이며, `ControlPanel.get_all_messages()`가 반환하는
  형태와 그대로 호환된다.
- 모든 메서드는 예외를 던지지 않고 `SyncResult`(또는 `SyncStatus`)를 반환한다.
- `SupabaseClientManager`는 `supabase` 패키지가 설치되어 있지 않아도 import 시점에
  죽지 않는다 (`ImportError`를 잡아 `_SDK_AVAILABLE = False`로 처리).

### Phase 2A에서 하지 않은 것 (의도적으로 남겨둠)

- `MainWindow`(GUI)와의 실제 연결 — 저장/불러오기 버튼에서 `push_messages()` /
  `pull_messages()`를 호출하는 배선은 **Phase 2B**에서 진행한다.
- Supabase Realtime 구독 (모바일에서 수정 시 PC에 실시간 반영) — 현재는 `CloudSyncService`가
  polling(수동 pull) 구조로만 설계되어 있고, Realtime을 나중에 추가할 수 있도록 클래스
  경계만 분리해두었다.
- 클라우드 동기화 on/off UI, 동기화 상태 표시 UI.
- 모바일 관리 웹, Google OAuth, 관리자 승인, 사용자 권한 관리.

---

## 5. Supabase 스키마 (제안 — 아직 실제 프로젝트에 적용하지 않음)

```sql
create table if not exists messages (
    message_number integer primary key check (message_number between 1 and 12),
    text text not null default '',
    updated_at timestamptz not null default now(),
    updated_by text not null default '',
    version integer not null default 1
);

alter table messages enable row level security;

-- 최소 정책 예시 (실제 배포 전 요구사항에 맞게 재검토 필요):
-- anon key로 읽기/쓰기를 허용하되, service_role key는 절대 클라이언트에 배포하지 않는다.
create policy "messages_select" on messages for select using (true);
create policy "messages_insert" on messages for insert with check (true);
create policy "messages_update" on messages for update using (true);
```

> 이 스키마는 설계 제안이며 Phase 2A에서 실제로 Supabase 프로젝트에 적용하지 않았다.
> 실제 적용은 사용자가 Supabase 프로젝트를 준비한 뒤 별도로 진행한다.

---

## 6. 환경변수

`.env.example` 참고. `.env` 파일은 `.gitignore`에 등록되어 있으며 git에 커밋되지 않는다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SUPABASE_ENABLED` | `false` | 클라우드 동기화 사용 여부 |
| `SUPABASE_URL` | (없음) | Supabase 프로젝트 URL |
| `SUPABASE_ANON_KEY` | (없음) | anon(public) key. service_role 금지 |
| `SUPABASE_SYNC_INTERVAL_SECONDS` | `30` | `CloudSyncCoordinator`의 폴링 주기(초) — Phase 2C에서 실제 사용 시작, 기본값을 60→30으로 조정 |
| `SUPABASE_DEVICE_ID` | PC 호스트 이름 기반 자동 생성 | `updated_by` 기록에 사용할 기기 식별자 |

`SUPABASE_ENABLED=true`인데 `SUPABASE_URL`/`SUPABASE_ANON_KEY`가 비어있으면,
`config/cloud_settings.py`가 경고 로그만 남기고 자동으로 `enabled=False`로 전환한다
(잘못된 설정이 앱 실행을 막지 않도록).

---

## 7. Phase 2B 예정 작업이었던 항목 — 실제 진행 현황

- `MainWindow`의 저장/불러오기 콜백에 `CloudSyncService` 연결 → **완료 (Phase 2C)**,
  `services/cloud_sync_coordinator.py` 참고.
- 프로그램 시작 시 `pull_messages()`로 최신 메시지 반영 → **완료 (Phase 2C)**, 단
  실제 로그인 세션이 있을 때만 동작 — 로그인 자체는 아직 미구현이라 현재는 항상
  "로그인 필요" 상태로 건너뛴다 (아래 PHASE2C 문서의 "미구현 항목" 참고).
- 클라우드 동기화 on/off 토글, 연결 상태 표시 → **연결 상태 표시는 완료**(상단
  작은 상태 라벨). on/off 토글 UI는 아직 없음(환경변수로만 제어).
- `AutoScheduler.notify_cloud_update()`를 실제 클라우드 폴링/이벤트 발생 지점에서 호출
  → **완료 (Phase 2C)**.
- 충돌(`conflicts`) 발생 시 사용자에게 보여줄 UI/로그 형식 → 상태 라벨의
  "충돌 확인 필요" 표시 + 로그까지 완료, 충돌을 직접 해결하는 UI는 아직 없음.

자세한 내용은 [PHASE2C_DESKTOP_INTEGRATION.md](PHASE2C_DESKTOP_INTEGRATION.md) 참고.
