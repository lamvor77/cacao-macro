# core/scheduler.py — 발송 직전 검증 훅(요구사항 4/5절, Production Stabilization
# Sprint에서 배치 조회 → message_no 단위 개별 조회로 전면 개편) 테스트
#
# 실제 카카오톡/psutil 프로세스 검사는 패치로 대체한다. 그룹 발송 시간 대기
# 로직(_loop/_tick)은 거치지 않고 _send_group()을 직접 호출해 훅 연동만 검증한다.
#
# 실행: python -m unittest tests.test_scheduler_verify_latest -v

import unittest
from unittest.mock import patch

from core.scheduler import AutoScheduler
from core.send_verification import (
    SendMessageVerificationResult,
    VerificationErrorCode,
    VerificationSource,
)


def _make_scheduler(verify_message_fn=None, get_local_revision_fn=None, messages=None, rooms=None):
    scheduler = AutoScheduler(
        get_rooms_fn=lambda: rooms or {"room1": True},
        get_messages_fn=lambda: dict(messages or {1: "m1", 2: "m2", 3: "m3"}),
        log_fn=lambda msg: None,
        verify_message_fn=verify_message_fn,
        get_local_revision_fn=get_local_revision_fn,
    )
    # _send_group()은 그룹 루프 안에서 self._running을 확인한다(중지 요청 처리) —
    # 이 테스트들은 스케줄 루프(start())를 거치지 않고 _send_group()을 직접
    # 호출하므로, 실행 중 상태를 직접 표시해야 첫 방에서 곧바로 반환되지 않는다.
    scheduler._running = True
    return scheduler


def _allowed(message_no, content, source=VerificationSource.SERVER, used_cached=False):
    return SendMessageVerificationResult(
        allowed=True, message_no=message_no, content=content, local_revision=1,
        server_revision=2, source=source, used_cached_content=used_cached,
    )


def _blocked(message_no, error_code=VerificationErrorCode.NETWORK_ERROR):
    return SendMessageVerificationResult(
        allowed=False, message_no=message_no, content="", local_revision=1,
        server_revision=None, source=VerificationSource.UNAVAILABLE,
        used_cached_content=False, error_code=error_code, error_message="네트워크 오류",
    )


class TestVerifyMessageHookWiring(unittest.TestCase):
    @patch("core.scheduler._is_kakao_running", return_value=True)
    def test_verify_message_fn_called_per_message_not_batch(self, _mock_running):
        """전체 12건이 아니라 그룹의 각 message_no만, 한 건씩 호출되어야 한다."""
        calls = []

        def verify(message_no, local_content, local_revision):
            calls.append((message_no, local_content, local_revision))
            return _allowed(message_no, local_content)

        scheduler = _make_scheduler(verify_message_fn=verify)
        with patch.object(scheduler._sender, "send_message", return_value=True):
            scheduler._send_group("A", {"label": "Group A", "messages": [1, 2, 3], "minute": 0})

        self.assertEqual(len(calls), 3)
        self.assertEqual([c[0] for c in calls], [1, 2, 3])

    @patch("core.scheduler._is_kakao_running", return_value=True)
    def test_server_content_used_when_allowed(self, _mock_running):
        scheduler = _make_scheduler(verify_message_fn=lambda n, c, r: _allowed(n, f"server-{n}"))
        sent_texts = []
        with patch.object(scheduler._sender, "send_message", side_effect=lambda room, text: sent_texts.append(text) or True):
            scheduler._send_group("A", {"label": "Group A", "messages": [1, 2, 3], "minute": 0})

        self.assertEqual(sent_texts, ["server-1", "server-2", "server-3"])

    @patch("core.scheduler._is_kakao_running", return_value=True)
    def test_hook_exception_excludes_only_that_message(self, _mock_running):
        """검증 훅 자체가 예외를 던져도 그 메시지 하나만 제외되고 나머지는 발송된다
        (완료 기준 7 — 한 발송 실패가 전체 scheduler를 중단하지 않는다)."""
        def verify(message_no, local_content, local_revision):
            if message_no == 2:
                raise ConnectionError("boom")
            return _allowed(message_no, local_content)

        scheduler = _make_scheduler(verify_message_fn=verify)
        sent_texts = []
        with patch.object(scheduler._sender, "send_message", side_effect=lambda room, text: sent_texts.append(text) or True):
            scheduler._send_group("A", {"label": "Group A", "messages": [1, 2, 3], "minute": 0})

        self.assertEqual(sent_texts, ["m1", "m3"], "message_no=2만 제외되고 나머지는 발송되어야 함")

    @patch("core.scheduler._is_kakao_running", return_value=True)
    def test_block_result_excludes_message_but_others_still_send(self, _mock_running):
        def verify(message_no, local_content, local_revision):
            if message_no == 1:
                return _blocked(message_no)
            return _allowed(message_no, local_content)

        scheduler = _make_scheduler(verify_message_fn=verify)
        sent_texts = []
        with patch.object(scheduler._sender, "send_message", side_effect=lambda room, text: sent_texts.append(text) or True):
            scheduler._send_group("A", {"label": "Group A", "messages": [1, 2, 3], "minute": 0})

        self.assertEqual(sent_texts, ["m2", "m3"])

    @patch("core.scheduler._is_kakao_running", return_value=True)
    def test_all_messages_blocked_skips_whole_group_but_not_scheduler(self, _mock_running):
        scheduler = _make_scheduler(verify_message_fn=lambda n, c, r: _blocked(n))
        with patch.object(scheduler._sender, "send_message") as mock_send:
            scheduler._send_group("A", {"label": "Group A", "messages": [1, 2, 3], "minute": 0})
        mock_send.assert_not_called()
        # 그룹 하나가 통째로 비어도 스케줄러 자체는 계속 실행 상태를 유지해야 한다.
        self.assertTrue(scheduler._running)

    @patch("core.scheduler._is_kakao_running", return_value=True)
    def test_cached_result_used_and_logged(self, _mock_running):
        logs = []
        scheduler = AutoScheduler(
            get_rooms_fn=lambda: {"room1": True},
            get_messages_fn=lambda: {1: "m1", 2: "m2", 3: "m3"},
            log_fn=logs.append,
            verify_message_fn=lambda n, c, r: _allowed(n, c, source=VerificationSource.LOCAL_CACHE, used_cached=True),
        )
        scheduler._running = True
        with patch.object(scheduler._sender, "send_message", return_value=True):
            scheduler._send_group("A", {"label": "Group A", "messages": [1, 2, 3], "minute": 0})

        self.assertTrue(any("cached" in log or "캐시" in log for log in logs))

    @patch("core.scheduler._is_kakao_running", return_value=True)
    def test_get_local_revision_fn_passed_to_verify(self, _mock_running):
        received_revisions = []

        def verify(message_no, local_content, local_revision):
            received_revisions.append(local_revision)
            return _allowed(message_no, local_content)

        scheduler = _make_scheduler(
            verify_message_fn=verify,
            get_local_revision_fn=lambda n: 100 + n,
        )
        with patch.object(scheduler._sender, "send_message", return_value=True):
            scheduler._send_group("A", {"label": "Group A", "messages": [1, 2, 3], "minute": 0})

        self.assertEqual(received_revisions, [101, 102, 103])

    @patch("core.scheduler._is_kakao_running", return_value=True)
    def test_none_verify_message_fn_preserves_legacy_behavior(self, _mock_running):
        """verify_message_fn을 생략하면(None) 기존과 완전히 동일하게 동작해야 한다
        (요구사항 12 — 기존 기능 유지)."""
        scheduler = _make_scheduler(verify_message_fn=None)
        sent_texts = []
        with patch.object(scheduler._sender, "send_message", side_effect=lambda room, text: sent_texts.append(text) or True):
            scheduler._send_group("A", {"label": "Group A", "messages": [1, 2, 3], "minute": 0})

        self.assertEqual(sent_texts, ["m1", "m2", "m3"])


if __name__ == "__main__":
    unittest.main()
