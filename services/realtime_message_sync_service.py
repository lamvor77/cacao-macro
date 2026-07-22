# shared_messages 실시간(Realtime) 동기화 — Production Stabilization Sprint에서
# 명시적 상태 머신 + 종료 후 콜백 차단(disposed 가드)을 추가해 안정화함.
#
# 배경 조사(설치된 supabase==2.31.0 / realtime==2.31.0 소스를 직접 확인):
#   - supabase-py의 "동기(Sync)" Realtime 채널(realtime._sync.channel.SyncRealtimeChannel)은
#     이 버전에서 __init__만 있는 미구현 스텁이다(subscribe/on_postgres_changes 없음).
#   - Postgres Changes를 실제로 구독할 수 있는 것은 "비동기(Async)" 클라이언트뿐이며,
#     지수 백오프 재연결(realtime._async.client.AsyncRealtimeClient.connect/_reconnect)도
#     라이브러리가 이미 구현해 제공한다 — 이 클래스는 그 재연결에 전적으로
#     의존하지 않고, 우리 쪽 상태 머신(State 클래스 참고)으로 현재 상태를 명확히
#     추적한다(요구사항 7절 "라이브러리의 내부 자동 재연결에만 전적으로
#     의존하지 말고, 서비스 상태 머신을 명확히 만드세요").
#   - 이 프로젝트의 GUI는 Tkinter(동기, 단일 이벤트루프)이므로, asyncio 이벤트루프를
#     전용 백그라운드 스레드에서 돌리고 콜백을 이 클래스가 대신 받아, 호출부가
#     기존 self.after(0, ...) 패턴으로 안전하게 메인 스레드에 넘길 수 있도록
#     "평범한 콜백 함수"로 재노출한다 — 이 클래스 자체는 Tkinter를 전혀 참조하지
#     않는다(원칙: Realtime 콜백에서 직접 위젯을 만지지 않는다).
#
# ===== GUI 종료 후 콜백 발생 방지(Production Stabilization Sprint에서 발견/수정) =====
# stop()이 호출된 즉시(비동기 정리 작업이 끝나기 "전"부터) self._disposed = True로
# 표시한다. 이후 asyncio 스레드에서 이미 진행 중이던 콜백이 도착해도
# _handle_subscribe_state/_handle_postgres_change/_set_state가 가장 먼저
# _disposed를 확인해 그대로 버린다 — on_change_fn/on_state_fn/
# on_reconcile_needed_fn(결국 gui/main_window.py의 self.after(0, ...))이 파괴된
# Tkinter 위젯을 건드릴 방법이 없어진다. 이 플래그는 1회성이다 — 한 번 stop()된
# 인스턴스는 재사용하지 않는다(재시작은 항상 새 인스턴스를 만든다 —
# gui/main_window.py의 _restart_shared_message_realtime 참고).
#
# 주의: 이 모듈은 실제 Supabase Realtime 서버에 연결해 본 적이 없다(이 프로젝트
# 전체 원칙 — 실제 Supabase를 자동으로 건드리지 않는다). 라이브러리 소스 분석과
# 방어적 예외 처리로 최대한 안전하게 작성했지만, 실제 연결/재연결/이벤트 수신은
# 사용자의 실제 Supabase 프로젝트에서 최종 확인이 필요하다(docs/e2e_realtime_test_plan.md).

import asyncio
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_CHANNEL_TOPIC = "shared_messages_changes"
_STOP_TIMEOUT_SECONDS = 5.0


class RealtimeConnectionState(Enum):
    """서비스 내부 상태 머신(요구사항 7절 권장 상태와 동일)."""

    STOPPED = "stopped"
    STARTING = "starting"
    CONNECTING = "connecting"
    SUBSCRIBED = "subscribed"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    STOPPING = "stopping"


@dataclass
class MessageChangeEvent:
    """shared_messages UPDATE 이벤트에서 뽑아낸, 이 앱이 실제로 쓰는 값만 담은 DTO."""

    message_no: int
    revision: int
    content: str
    title: Optional[str]
    updated_by: Optional[str]
    updated_by_name: Optional[str]
    update_source: str
    updated_at: str


class RealtimeMessageSyncService:
    """shared_messages 테이블의 UPDATE를 구독하는 백그라운드 asyncio 스레드 래퍼.

    한 인스턴스는 start() → (여러 상태 전이) → stop()의 1회성 수명주기를 가진다.
    stop() 이후에는 재사용하지 않는다 — 재시작이 필요하면 항상 새 인스턴스를
    만든다(이 클래스가 스스로 재시작하지 않는 것은 의도된 설계다 — "재시작"과
    "재연결"을 구분한다: 재연결은 이 서비스가 살아있는 동안 자동으로 일어나고,
    재시작은 호출부가 명시적으로 새 인스턴스를 만드는 것이다).

    모든 콜백(on_change_fn/on_state_fn/on_reconcile_needed_fn/log_fn)은 asyncio
    스레드에서 호출된다 — 호출부가 Tkinter 위젯을 직접 건드리려면 self.after(0, ...)로
    다시 메인 스레드에 넘겨야 한다(이 클래스는 강제하지 않는다 — 원칙: Realtime
    콜백에서 직접 위젯을 수정하지 않는다는 책임은 호출부에 있다). stop() 이후에는
    이 콜백들이 절대 호출되지 않는다(_disposed 가드, 위 모듈 설명 참고).
    """

    def __init__(
        self,
        supabase_url: str,
        supabase_anon_key: str,
        get_session_tokens_fn: Callable[[], tuple],
        on_change_fn: Callable[[MessageChangeEvent], None],
        on_state_fn: Callable[[RealtimeConnectionState], None],
        on_reconcile_needed_fn: Callable[[], None],
        log_fn: Optional[Callable[[str], None]] = None,
        debug_enabled: bool = False,
        protocol_compat_enabled: bool = True,
    ):
        """
        Args:
            get_session_tokens_fn: (access_token, refresh_token) 튜플을 반환하는
                콜백 — AuthService.load_session()처럼 로컬 파일만 읽는(네트워크 없는)
                함수여야 한다. 로그인하지 않았으면 (None, None)을 반환해도 된다
                (익명으로 연결을 시도하며, RLS의 fn_is_approved()에 의해 실제
                이벤트는 승인된 사용자에게만 전달된다).
            debug_enabled: MESSAGE_SYNC_DEBUG(요구사항 13절). False면(기본값)
                연결/재연결/오류처럼 운영상 항상 필요한 로그만 남긴다. True면
                수신한 각 이벤트의 message_no/revision 등 세부 로그를 추가로
                남긴다 — 어느 쪽이든 메시지 본문/토큰/키는 절대 남기지 않는다.
            protocol_compat_enabled: REALTIME_PROTOCOL_COMPAT(기본 true). false면
                services/realtime_protocol_compat.py의 배열 메시지 정규화 계층을
                쓰지 않고 원본 AsyncRealtimeClient를 그대로 사용한다(즉시
                원상복구용 안전장치 — 문제가 생기면 코드 변경 없이 .env만 바꿔
                끌 수 있다).
        """
        self._url = supabase_url
        self._anon_key = supabase_anon_key
        self._get_session_tokens = get_session_tokens_fn
        self._on_change = on_change_fn
        self._on_state = on_state_fn
        self._on_reconcile_needed = on_reconcile_needed_fn
        self._log = log_fn or (lambda msg: None)
        self._debug_enabled = debug_enabled
        self._protocol_compat_enabled = protocol_compat_enabled

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._channel = None
        self._state = RealtimeConnectionState.STOPPED
        # 종료 후 콜백 차단 가드(위 모듈 설명 참고) — stop() 진입 즉시 True.
        self._disposed = False
        self._reconnect_count = 0

    @property
    def state(self) -> RealtimeConnectionState:
        return self._state

    @property
    def reconnect_count(self) -> int:
        """진단정보/디버그 로그용 — 이번 인스턴스 수명 동안 재연결이 발생한 횟수."""
        return self._reconnect_count

    # ===== 공개 메서드 (동기, 호출부 스레드에서 안전) =====

    def start(self) -> None:
        """중복 호출을 방지한다 — 이미 시작했거나 이미 폐기된 인스턴스는 무시한다."""
        if self._disposed:
            logger.warning("이미 종료된 RealtimeMessageSyncService는 재시작할 수 없습니다 — 새 인스턴스를 만드세요.")
            return
        if self._thread is not None:
            return
        self._set_state(RealtimeConnectionState.STARTING)
        self._thread = threading.Thread(target=self._run_loop, name="realtime-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """구독을 해제하고 스레드를 정리한다(완료 기준 — 종료 시 구독 정상 해제).

        가장 먼저 _disposed를 True로 표시해 이후 어떤 콜백도 사용자 코드에
        도달하지 못하게 막는다. 완료를 기다리되(_STOP_TIMEOUT_SECONDS), 네트워크가
        이미 끊겨 있어도 프로그램 종료 자체가 멈추지 않도록 최대 대기시간을 둔다.
        여러 번 호출해도 안전하다(두 번째 호출부터는 아무 것도 하지 않음).
        """
        if self._disposed:
            return  # 중복 stop() 호출 — 안전하게 무시
        self._disposed = True

        loop = self._loop
        if loop is None or not loop.is_running():
            self._thread = None
            self._loop = None
            self._state = RealtimeConnectionState.STOPPED
            return

        self._state = RealtimeConnectionState.STOPPING
        future = asyncio.run_coroutine_threadsafe(self._async_stop(), loop)
        try:
            future.result(timeout=_STOP_TIMEOUT_SECONDS)
        except Exception as e:
            logger.warning(f"Realtime 종료 대기 중 오류(무시하고 계속 진행): {type(e).__name__}")

        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=_STOP_TIMEOUT_SECONDS)
            if self._thread.is_alive():
                logger.warning("Realtime 백그라운드 스레드가 제한 시간 내에 종료되지 않았습니다(계속 진행).")
        self._thread = None
        self._loop = None
        self._state = RealtimeConnectionState.STOPPED
        # _set_state()가 아니라 위에서 직접 self._state를 대입한다 — _disposed=True
        # 상태에서는 _set_state()가 on_state_fn을 호출하지 않도록 설계되어 있으므로
        # (아래 _set_state 참고), STOPPED 전이 자체는 기록하되 콜백은 정말로
        # 발생시키지 않는다(요구사항: 종료 후 콜백 금지는 STOPPED 전이 자체에도 적용).

    # ===== asyncio 스레드 내부 =====

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.create_task(self._async_start())
            loop.run_forever()
        finally:
            loop.close()

    async def _async_start(self) -> None:
        if self._disposed:
            return
        self._set_state(RealtimeConnectionState.CONNECTING)
        try:
            from supabase import create_async_client
        except ImportError:
            logger.error("supabase 패키지를 사용할 수 없어 Realtime을 시작할 수 없습니다.")
            self._set_state(RealtimeConnectionState.FAILED)
            return

        try:
            self._client = await create_async_client(self._url, self._anon_key)

            # Realtime protocol 2.0(배열 메시지) 호환 계층 — create_async_client()가
            # 내부적으로 만든 원본 AsyncRealtimeClient(아직 connect()되지 않아
            # 부작용 없음)를 CompatAsyncRealtimeClient로 교체한다. AsyncClient가
            # 이미 계산해 둔 realtime_url/supabase_key/options.realtime을 그대로
            # 재사용한다 — supabase-py 내부 전역 이름을 바꾸지 않는다.
            from services.realtime_protocol_compat import create_compat_realtime_client

            self._client.realtime = create_compat_realtime_client(
                str(self._client.realtime_url),
                token=self._client.supabase_key,
                compat_enabled=self._protocol_compat_enabled,
                **(self._client.options.realtime or {}),
            )

            access_token, refresh_token = self._get_session_tokens()
            if access_token and refresh_token:
                try:
                    await self._client.auth.set_session(access_token, refresh_token)
                except Exception as e:
                    # 인증 세션 적용에 실패해도 연결 자체는 시도한다 — RLS가
                    # fn_is_approved()로 막아줄 뿐, 익명 연결 시도 자체는 안전하다.
                    logger.warning(f"Realtime 인증 세션 적용 실패(계속 진행): {type(e).__name__}")
                    self._log("[경고] Realtime 인증 세션 적용 실패 — 재로그인이 필요할 수 있습니다.")

            if self._disposed:
                return

            if self._channel is not None:
                # 정상 흐름에서는 발생하지 않는다(start()가 중복 호출을 막으므로) —
                # 방어적으로만 남겨둔다(요구사항: 재연결 시 이전 채널 제거).
                logger.warning("이전 Realtime 채널이 남아있어 재생성합니다.")
                self._channel = None

            self._channel = self._client.channel(_CHANNEL_TOPIC)
            self._channel.on_postgres_changes(
                "UPDATE", schema="public", table="shared_messages", callback=self._handle_postgres_change,
            )
            await self._channel.subscribe(self._handle_subscribe_state)
        except Exception as e:
            logger.error(f"Realtime 연결 시작 실패: {type(e).__name__}: {e}")
            self._log(f"[오류] Realtime 연결 실패: {type(e).__name__}")
            self._set_state(RealtimeConnectionState.FAILED)

    async def _async_stop(self) -> None:
        try:
            if self._channel is not None:
                await self._channel.unsubscribe()
        except Exception as e:
            logger.debug(f"Realtime 구독 해제 중 오류(무시): {type(e).__name__}")
        try:
            realtime_client = getattr(self._client, "realtime", None)
            if realtime_client is not None:
                await realtime_client.close()
        except Exception as e:
            logger.debug(f"Realtime 연결 종료 중 오류(무시): {type(e).__name__}")
        # log_fn 호출 자체는 허용한다(로그는 위젯이 아니라 로그 패널/파일에 남을 뿐이고,
        # 호출부의 log_callback도 self.after(0, ...)로 넘기므로 파괴된 위젯을 직접
        # 건드리지 않는다) — 다만 on_change_fn/on_state_fn/on_reconcile_needed_fn은
        # 여기서 절대 호출하지 않는다.
        self._log("[INFO] Realtime 구독 해제 완료")

    # ===== 라이브러리 콜백 (asyncio 스레드에서 호출됨 — Tk 위젯 직접 접근 금지) =====

    def _handle_subscribe_state(self, state, error) -> None:
        if self._disposed:
            return
        from realtime import RealtimeSubscribeStates

        if state == RealtimeSubscribeStates.SUBSCRIBED:
            was_reconnecting = self._state in (
                RealtimeConnectionState.RECONNECTING, RealtimeConnectionState.FAILED,
            )
            if was_reconnecting:
                self._reconnect_count += 1
            self._set_state(RealtimeConnectionState.SUBSCRIBED)
            self._log("[INFO] Realtime 재연결 성공" if was_reconnecting else "[INFO] Realtime 연결 성공")
            # 요구사항 7절: 최초 연결/재연결 모두 이 시점에 전체 재조회로 누락 이벤트를 복구한다.
            try:
                self._on_reconcile_needed()
            except Exception:
                logger.exception("Realtime 재연결 정합성 복구 콜백 오류")
        elif state == RealtimeSubscribeStates.CHANNEL_ERROR:
            self._set_state(RealtimeConnectionState.RECONNECTING)
            self._log(f"[경고] Realtime 채널 오류 — 재연결 시도 중: {error}")
        elif state == RealtimeSubscribeStates.TIMED_OUT:
            self._set_state(RealtimeConnectionState.RECONNECTING)
            self._log("[경고] Realtime 구독 시간 초과 — 재연결 시도 중")
        elif state == RealtimeSubscribeStates.CLOSED:
            # stop()이 이미 _disposed=True로 표시했다면 이 콜백 자체가 맨 위에서
            # 걸러진다 — 여기 도달했다는 것은 "예상치 못한" 종료(서버 측 종료 등)라는
            # 뜻이므로 FAILED로 취급한다(정상 종료와 구분).
            self._set_state(RealtimeConnectionState.FAILED)
            self._log("[경고] Realtime 연결이 예기치 않게 종료되었습니다.")

    def _handle_postgres_change(self, payload: dict) -> None:
        if self._disposed:
            return
        try:
            data = payload.get("data") or {}
            record = data.get("record") or {}
            event = MessageChangeEvent(
                message_no=int(record["message_no"]),
                revision=int(record.get("revision", 0)),
                content=record.get("content", "") or "",
                title=record.get("title"),
                updated_by=record.get("updated_by"),
                updated_by_name=record.get("updated_by_name"),
                update_source=record.get("update_source", ""),
                updated_at=record.get("updated_at", "") or "",
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Realtime 이벤트 파싱 오류(무시): {type(e).__name__}")
            return

        # 본문 전체를 로그로 남기지 않는다(요구사항 16절) — 번호/revision만.
        # MESSAGE_SYNC_DEBUG가 꺼져 있으면(기본값) 매 이벤트마다 로그를 남기지
        # 않는다 — 정상 운영 중에는 매우 빈번할 수 있어 "세부정보"로 분류한다
        # (요구사항 13절). 연결/재연결/오류 로그는 debug 여부와 무관하게 항상 남는다.
        if self._debug_enabled:
            self._log(f"[DEBUG] 메시지 변경 이벤트 수신 — message_no={event.message_no}, revision={event.revision}")
        if self._disposed:
            return
        try:
            self._on_change(event)
        except Exception:
            logger.exception("Realtime 변경 이벤트 처리 콜백 오류")

    def _set_state(self, state: RealtimeConnectionState) -> None:
        self._state = state
        if self._disposed:
            return
        try:
            self._on_state(state)
        except Exception:
            logger.exception("Realtime 상태 콜백 오류")
