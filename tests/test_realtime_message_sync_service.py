# services/realtime_message_sync_service.py 테스트
#
# 실제 Supabase Realtime 서버에는 절대 연결하지 않는다. asyncio 이벤트루프/스레드를
# 시작하지 않고, 라이브러리 콜백(_handle_subscribe_state/_handle_postgres_change)을
# 직접 동기 호출해 파싱/상태 전이 로직만 검증한다 — 이 두 메서드는 원래도 코루틴이
# 아니라 realtime 라이브러리가 동기적으로 호출하는 평범한 콜백이므로 직접 호출이
# 실제 사용 방식과 동일하다.
#
# 실행: python -m unittest tests.test_realtime_message_sync_service -v

import unittest
from unittest.mock import MagicMock

from realtime import RealtimeSubscribeStates

from services.realtime_message_sync_service import (
    MessageChangeEvent,
    RealtimeConnectionState,
    RealtimeMessageSyncService,
)


def _make_service(on_change=None, on_state=None, on_reconcile=None, log=None, debug_enabled=False):
    return RealtimeMessageSyncService(
        supabase_url="https://example.supabase.co",
        supabase_anon_key="fake-anon-key",
        get_session_tokens_fn=lambda: (None, None),
        on_change_fn=on_change or (lambda e: None),
        on_state_fn=on_state or (lambda s: None),
        on_reconcile_needed_fn=on_reconcile or (lambda: None),
        log_fn=log or (lambda msg: None),
        debug_enabled=debug_enabled,
    )


class TestSubscribeStateTransitions(unittest.TestCase):
    def test_subscribed_sets_connected_and_triggers_reconcile(self):
        states = []
        reconcile_calls = []
        svc = _make_service(on_state=states.append, on_reconcile=lambda: reconcile_calls.append(1))

        svc._handle_subscribe_state(RealtimeSubscribeStates.SUBSCRIBED, None)

        self.assertEqual(states, [RealtimeConnectionState.SUBSCRIBED])
        self.assertEqual(len(reconcile_calls), 1, "SUBSCRIBED 시 재조회(정합성 복구)가 호출되어야 함")

    def test_reconnect_after_error_also_triggers_reconcile(self):
        """요구사항 7절 — 최초 연결뿐 아니라 재연결 성공 후에도 누락 이벤트 복구가 필요하다."""
        states = []
        reconcile_calls = []
        svc = _make_service(on_state=states.append, on_reconcile=lambda: reconcile_calls.append(1))

        svc._handle_subscribe_state(RealtimeSubscribeStates.CHANNEL_ERROR, Exception("boom"))
        svc._handle_subscribe_state(RealtimeSubscribeStates.SUBSCRIBED, None)

        self.assertEqual(states, [RealtimeConnectionState.RECONNECTING, RealtimeConnectionState.SUBSCRIBED])
        self.assertEqual(len(reconcile_calls), 1)

    def test_channel_error_sets_reconnecting(self):
        states = []
        svc = _make_service(on_state=states.append)
        svc._handle_subscribe_state(RealtimeSubscribeStates.CHANNEL_ERROR, Exception("boom"))
        self.assertEqual(states, [RealtimeConnectionState.RECONNECTING])

    def test_timed_out_sets_reconnecting(self):
        states = []
        svc = _make_service(on_state=states.append)
        svc._handle_subscribe_state(RealtimeSubscribeStates.TIMED_OUT, None)
        self.assertEqual(states, [RealtimeConnectionState.RECONNECTING])

    def test_closed_sets_failed_when_unexpected(self):
        """_disposed가 아닌 상태에서 CLOSED가 오면 "예기치 않은 종료"로 취급해
        FAILED로 전이한다(정상 stop()에 의한 종료와 구분 — 아래 disposed 테스트 참고)."""
        states = []
        svc = _make_service(on_state=states.append)
        svc._handle_subscribe_state(RealtimeSubscribeStates.CLOSED, None)
        self.assertEqual(states, [RealtimeConnectionState.FAILED])

    def test_reconnect_count_increments_only_on_actual_reconnect(self):
        svc = _make_service()
        self.assertEqual(svc.reconnect_count, 0)
        svc._handle_subscribe_state(RealtimeSubscribeStates.SUBSCRIBED, None)  # 최초 연결
        self.assertEqual(svc.reconnect_count, 0, "최초 연결은 재연결이 아니다")
        svc._handle_subscribe_state(RealtimeSubscribeStates.CHANNEL_ERROR, Exception("x"))
        svc._handle_subscribe_state(RealtimeSubscribeStates.SUBSCRIBED, None)  # 재연결
        self.assertEqual(svc.reconnect_count, 1)

    def test_state_callback_exception_does_not_propagate(self):
        """상태 콜백이 예외를 던져도 realtime 처리 자체가 죽으면 안 된다(요구사항 16절
        "이벤트 수신 실패가 프로그램 전체 오류로 이어지지 않음")."""
        def bad_callback(_state):
            raise RuntimeError("UI가 죽었다고 가정")

        svc = _make_service(on_state=bad_callback)
        try:
            svc._handle_subscribe_state(RealtimeSubscribeStates.SUBSCRIBED, None)
        except RuntimeError:
            self.fail("_handle_subscribe_state가 콜백 예외를 밖으로 전파하면 안 됨")


class TestPostgresChangeParsing(unittest.TestCase):
    def _payload(self, **record_overrides):
        record = {
            "message_no": 3, "revision": 7, "content": "새 내용", "title": None,
            "updated_by": "uid-123", "updated_by_name": "홍길동", "update_source": "mobile",
            "updated_at": "2026-07-19T00:00:00Z",
        }
        record.update(record_overrides)
        return {"data": {"schema": "public", "table": "shared_messages", "type": "UPDATE", "record": record}}

    def test_valid_payload_parsed_and_forwarded(self):
        received = []
        svc = _make_service(on_change=received.append)
        svc._handle_postgres_change(self._payload())

        self.assertEqual(len(received), 1)
        event = received[0]
        self.assertIsInstance(event, MessageChangeEvent)
        self.assertEqual(event.message_no, 3)
        self.assertEqual(event.revision, 7)
        self.assertEqual(event.content, "새 내용")
        self.assertEqual(event.updated_by_name, "홍길동")

    def test_missing_record_ignored_gracefully(self):
        received = []
        svc = _make_service(on_change=received.append)
        svc._handle_postgres_change({"data": {}})
        self.assertEqual(received, [], "record가 없으면 콜백을 호출하지 않아야 함")

    def test_missing_message_no_key_ignored_gracefully(self):
        received = []
        svc = _make_service(on_change=received.append)
        payload = self._payload()
        del payload["data"]["record"]["message_no"]
        svc._handle_postgres_change(payload)
        self.assertEqual(received, [])

    def test_malformed_payload_does_not_raise(self):
        svc = _make_service()
        try:
            svc._handle_postgres_change({"unexpected": "shape"})
        except Exception as e:
            self.fail(f"잘못된 payload로 예외가 전파됨: {e}")

    def test_on_change_callback_exception_does_not_propagate(self):
        def bad_callback(_event):
            raise RuntimeError("boom")

        svc = _make_service(on_change=bad_callback)
        try:
            svc._handle_postgres_change(self._payload())
        except RuntimeError:
            self.fail("_handle_postgres_change가 콜백 예외를 밖으로 전파하면 안 됨")

    def test_log_does_not_include_full_message_content(self):
        """요구사항 16절 — 로그에는 message_no/revision만, 메시지 본문 전체는 남기지 않는다.
        Production Stabilization Sprint(요구사항 13절) — 이 세부 로그는 이제
        MESSAGE_SYNC_DEBUG(debug_enabled)가 켜져 있을 때만 남는다."""
        logged = []
        svc = _make_service(log=logged.append, debug_enabled=True)
        svc._handle_postgres_change(self._payload(content="이것은 절대 로그에 남으면 안 되는 민감한 발송 문구입니다"))

        joined = " ".join(logged)
        self.assertIn("message_no=3", joined)
        self.assertIn("revision=7", joined)
        self.assertNotIn("절대 로그에 남으면 안 되는", joined)

    def test_event_log_suppressed_when_debug_disabled(self):
        logged = []
        svc = _make_service(log=logged.append, debug_enabled=False)
        svc._handle_postgres_change(self._payload())
        self.assertEqual(logged, [], "MESSAGE_SYNC_DEBUG가 꺼져 있으면 이벤트별 로그를 남기지 않아야 함")


class TestConnectionStateProperty(unittest.TestCase):
    def test_initial_state_is_stopped(self):
        svc = _make_service()
        self.assertEqual(svc.state, RealtimeConnectionState.STOPPED)

    def test_stop_without_start_is_safe_noop(self):
        svc = _make_service()
        try:
            svc.stop()
        except Exception as e:
            self.fail(f"시작하지 않은 서비스의 stop()이 예외를 던짐: {e}")

    def test_stop_called_twice_is_safe(self):
        svc = _make_service()
        svc.stop()
        try:
            svc.stop()
        except Exception as e:
            self.fail(f"stop()을 두 번 호출하면 예외가 발생함: {e}")

    def test_start_does_not_spawn_second_thread_when_already_running(self):
        svc = _make_service()
        svc._thread = MagicMock()  # 이미 실행 중인 것처럼 흉내낸다
        svc._thread.is_alive.return_value = True
        original_thread = svc._thread
        svc.start()
        self.assertIs(svc._thread, original_thread, "이미 스레드가 있으면 start()가 새 스레드를 만들면 안 됨")

    def test_start_after_stop_refuses_to_restart_same_instance(self):
        svc = _make_service()
        svc.stop()  # start() 없이 stop()만 호출해도 _disposed가 표시됨
        svc.start()
        self.assertIsNone(svc._thread, "폐기된 인스턴스는 start()해도 스레드를 만들면 안 됨")


class TestDisposedGuardBlocksAllCallbacksAfterStop(unittest.TestCase):
    """Production Stabilization Sprint에서 발견/수정한 핵심 위험 — GUI 종료(stop())
    이후에는 asyncio 스레드에서 이미 진행 중이던 콜백이 도착해도 on_change_fn/
    on_state_fn/on_reconcile_needed_fn이 절대 호출되지 않아야 한다(파괴된 Tkinter
    위젯을 건드리는 것을 코드 구조로 원천 차단)."""

    def test_state_callback_blocked_after_dispose(self):
        states = []
        svc = _make_service(on_state=states.append)
        svc.stop()  # _disposed = True
        svc._handle_subscribe_state(RealtimeSubscribeStates.SUBSCRIBED, None)
        self.assertEqual(states, [], "stop() 이후에는 on_state_fn이 호출되면 안 됨")

    def test_change_callback_blocked_after_dispose(self):
        received = []
        svc = _make_service(on_change=received.append)
        svc.stop()
        payload = {"data": {"record": {
            "message_no": 1, "revision": 2, "content": "x", "title": None,
            "updated_by": None, "updated_by_name": None, "update_source": "mobile", "updated_at": "",
        }}}
        svc._handle_postgres_change(payload)
        self.assertEqual(received, [], "stop() 이후에는 on_change_fn이 호출되면 안 됨")

    def test_reconcile_callback_blocked_after_dispose(self):
        reconcile_calls = []
        svc = _make_service(on_reconcile=lambda: reconcile_calls.append(1))
        svc.stop()
        svc._handle_subscribe_state(RealtimeSubscribeStates.SUBSCRIBED, None)
        self.assertEqual(reconcile_calls, [], "stop() 이후에는 on_reconcile_needed_fn이 호출되면 안 됨")

    def test_log_fn_after_dispose_does_not_raise(self):
        """log_fn 자체는 호출부에서 self.after(0, ...)로 넘기므로 파괴된 위젯을
        직접 건드리지 않는다 — dispose 후에도 예외 없이 동작해야 한다(부가 확인)."""
        svc = _make_service()
        svc.stop()
        try:
            svc._handle_postgres_change({"data": {"record": {
                "message_no": 1, "revision": 1, "content": "x", "title": None,
                "updated_by": None, "updated_by_name": None, "update_source": "mobile", "updated_at": "",
            }}})
        except Exception as e:
            self.fail(f"dispose 후 호출이 예외를 던짐: {e}")


if __name__ == "__main__":
    unittest.main()
