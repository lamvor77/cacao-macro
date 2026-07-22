# PC ↔ 모바일 실시간 동기화 구조 (Mobile 실시간 동기화 스프린트)

개발/운영 담당자를 위한 기술 문서. 사용자 관점의 안내는
[docs/mobile_message_editor.md](mobile_message_editor.md) 참고.

## 1. 전체 흐름

```
수정 클라이언트(PC 또는 모바일)
   │  update_shared_message RPC (base_revision 포함)
   ▼
Supabase (shared_messages 테이블) — Single Source of Truth
   │  저장 성공 → 새 revision 반환
   │  Postgres UPDATE 이벤트 발생
   ▼
Supabase Realtime (Postgres Changes)
   │
   ├──▶ PC (services/realtime_message_sync_service.py)
   └──▶ 다른 모바일 클라이언트들
```

PC의 로컬 JSON(`storage/cloud_sync/messages.json`)이나 기존 `messages`
테이블(Phase 2A~2E, 30초 폴링)은 이 흐름과 **완전히 별개**로 계속 동작한다
— 이번 스프린트는 그 위에 shared_messages라는 새 계층을 추가했을 뿐, 기존
동기화를 바꾸거나 대체하지 않는다.

## 2. 왜 PC 쪽 Realtime 구독이 "동기(Sync)" 클라이언트가 아니라 "비동기(Async)"
   클라이언트로 구현되었는가

설치된 `supabase-py==2.31.0`(및 번들된 `realtime==2.31.0`) 라이브러리 소스를
직접 확인한 결과:

- 동기 Realtime 채널(`realtime._sync.channel.SyncRealtimeChannel`)은 이
  버전에서 `__init__`만 있는 미구현 스텁이다 — `subscribe()`/
  `on_postgres_changes()`가 없다.
- Postgres Changes를 실제로 구독할 수 있는 것은 비동기 클라이언트뿐이며,
  지수 백오프 재연결(`AsyncRealtimeClient.connect()`/`_reconnect()`)도
  라이브러리가 이미 구현해 제공한다.

그래서 `services/realtime_message_sync_service.py`는 전용 백그라운드
스레드에서 asyncio 이벤트루프를 돌리고, 그 안에서
`supabase.create_async_client()`로 만든 별도 클라이언트로 구독한다. 이
스레드에서 발생한 콜백(변경 이벤트/연결 상태)은 절대 Tkinter 위젯을 직접
건드리지 않고, `gui/main_window.py`가 `self.after(0, ...)`로 메인 스레드에
넘겨 받는다(이 프로젝트의 기존 백그라운드 스레드 패턴과 동일).

**주의**: 이 구현은 라이브러리 소스 분석과 방어적 예외 처리로 작성되었지만,
실제 Supabase Realtime 서버에 연결해 검증한 적은 없다(이 프로젝트는 실제
Supabase를 자동으로 건드리지 않는다는 원칙을 지킨다). 최초 배포 시 실제
연결/재연결/이벤트 수신을 반드시 수동으로 확인해야 한다.

## 3. Revision 기반 낙관적 동시성 제어 (OCC)

- `shared_messages.revision`은 저장할 때마다 1씩 증가한다.
- 클라이언트는 편집을 시작한 시점의 revision을 `base_revision`으로 들고
  있다가 저장 RPC에 함께 보낸다.
- `update_shared_message` RPC는 `SELECT ... FOR UPDATE`로 행을 잠근 뒤
  `base_revision`이 현재 값과 같을 때만 갱신한다 — 다르면 `REVISION_CONFLICT`
  오류를 던진다(자동 덮어쓰기 없음).

## 4. Realtime 이벤트 반영 규칙("같거나 낮으면 무시")

`core/shared_message_coordinator.py`(PC)와 `mobile/src/syncLogic.ts`(모바일)는
**완전히 동일한 규칙**을 각자의 언어로 구현한다:

```
event.revision > local.revision  →  반영
event.revision <= local.revision →  무시(자신의 에코 포함)
```

저장에 성공하면 로컬 revision을 즉시 새 값으로 올려두기 때문에(서버 응답을
받는 시점에 바로 반영), 몇백 ms 뒤 도착하는 "내가 방금 저장한 변경"의 Realtime
에코는 이 규칙 하나로 자연스럽게 걸러진다 — 별도의 "이건 내가 보낸 이벤트다"
표시(클라이언트 ID 비교 등)를 두지 않았다.

## 5. 편집 중 원격 변경 처리

사용자가 특정 메시지를 편집 중일 때 그 메시지에 대한 원격 변경 이벤트가
오면, 텍스트를 즉시 덮어쓰지 않고 `pendingRemote`(모바일)/
`pending_remote`(PC)에 보류한 뒤 "다른 직원이 수정했습니다" 배너를 띄운다.
편집하지 않는 메시지는 즉시 반영한다.

## 6. 재연결과 정합성 복구

- PC/모바일 모두 Realtime 채널이 `SUBSCRIBED` 상태가 될 때(최초 연결 포함)
  전체 12개를 다시 조회(`list_messages`)해 놓친 이벤트를 복구한다 — 연결이
  끊긴 동안의 변경이 여러 번 있었어도 최종 상태로 한 번에 맞춰진다.
- 재연결 자체(지수 백오프)는 라이브러리가 담당한다(PC: `AsyncRealtimeClient`,
  모바일: `@supabase/supabase-js`의 Realtime 클라이언트).
- 모바일은 추가로 브라우저 `visibilitychange`/`online` 이벤트에서도 전체
  재조회를 수행한다(절전/백그라운드 복귀, 네트워크 전환 대응).
- 주기적(30초/60초 등) 전체 폴링은 어디에도 없다 — 시작 시/로그인 직후/
  재연결 직후/수동 새로고침/발송 직전에만 서버 상태를 확인한다.

## 7. 발송 직전 검증과 무인 자동 발송의 절충

`core/scheduler.py`의 `AutoScheduler`는 이제 그룹 발송 시작 직전
`verify_latest_fn` 훅(기본 None, 있으면 호출)으로 그 그룹이 쓸 message_no만
서버 최신 상태를 확인한다(`gui/main_window.py`의
`_verify_latest_before_send`가 실제 구현).

이 프로그램은 08:00~19:00 사이 15분마다 완전히 무인으로 자동 발송한다
(`CLAUDE.md`의 핵심 원칙). 반면 이번 스프린트 요구사항은 "서버 확인 실패
시 사용자 승인 없이 조용히 발송하지 말 것"을 요구한다 — 두 원칙이 정면으로
부딪히는 지점이 있어, 의도적으로 다음과 같이 절충했다:

| 시점 | 서버 확인 실패 시 동작 |
|---|---|
| "▶ 시작" 버튼을 누른 순간(`_verify_before_start_and_launch`) | 사용자가 화면 앞에 있다고 볼 수 있는 유일한 순간 — 명시적 확인 대화상자("서버에서 최신 메시지를 확인하지 못했습니다. 마지막 동기화된 내용으로 발송하시겠습니까?")를 띄우고, 확인해야만 진행한다. |
| 이후 매 그룹 자동 발송(`AutoScheduler._send_group`, 하루 최대 44회) | 대화상자를 띄우지 않는다 — 로그에 경고를 남기고 마지막으로 알려진(캐시) 내용으로 조용히 진행한다. 무인 운영 중 매번 팝업이 뜨면 사실상 자동 발송이 아니게 된다. |

이 절충안은 최초 시작 시점에 이미 한 번 사용자 승인을 받았다는 전제 위에
있다. 서버 확인이 계속 실패하는 상황(예: 장시간 네트워크 장애)에서는 로그와
PC 화면의 메시지별 상태 라벨("오프라인 변경" 등)로 계속 알 수 있다.

## 8. 로그에 남기지 않는 것

메시지 본문 전체는 어떤 로그에도 남기지 않는다 — `message_no`, `revision`,
성공/실패 여부, 오류 유형(클래스명)만 남긴다
(`services/realtime_message_sync_service.py`,
`gui/main_window.py`의 `_log_masked_save_result` 참고).
