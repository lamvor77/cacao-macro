# core/send_verification.py 테스트 — 발송 직전 검증 로직(Tk/네트워크 비의존, fake fetch_fn만 사용)

import unittest
from dataclasses import dataclass

from core.send_verification import (
    OfflineSendPolicy,
    VerificationErrorCode,
    VerificationSource,
    verify_message_before_send,
)


@dataclass
class _FakeRecord:
    content: str
    revision: int


class _FailingFetch:
    def __init__(self, exc: Exception, fail_times: int = 999):
        self._exc = exc
        self._fail_times = fail_times
        self.call_count = 0

    def __call__(self, message_no: int):
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise self._exc
        return _FakeRecord(content="recovered", revision=99)


class _SlowFetch:
    """timeout_seconds보다 오래 걸리는 fetch를 흉내낸다."""

    def __init__(self):
        self.call_count = 0

    def __call__(self, message_no: int):
        import time
        self.call_count += 1
        time.sleep(2)
        return _FakeRecord(content="too-late", revision=1)


class TestServiceDisabled(unittest.TestCase):
    def test_service_disabled_returns_allowed_with_local_cache(self):
        result = verify_message_before_send(
            message_no=1, local_content="local", local_revision=3,
            fetch_fn=lambda n: (_ for _ in ()).throw(AssertionError("호출되면 안 됨")),
            policy=OfflineSendPolicy.BLOCK, timeout_seconds=5, retry_count=0,
            service_enabled=False,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.source, VerificationSource.LOCAL_CACHE)
        self.assertTrue(result.used_cached_content)
        self.assertEqual(result.error_code, VerificationErrorCode.SERVICE_DISABLED)
        self.assertEqual(result.content, "local")


class TestSuccessfulVerification(unittest.TestCase):
    def test_server_newer_revision_uses_server_content(self):
        fetch = lambda n: _FakeRecord(content="server-latest", revision=5)
        result = verify_message_before_send(
            message_no=2, local_content="local-old", local_revision=3,
            fetch_fn=fetch, policy=OfflineSendPolicy.BLOCK, timeout_seconds=5,
            retry_count=0, service_enabled=True,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.content, "server-latest")
        self.assertEqual(result.server_revision, 5)
        self.assertEqual(result.source, VerificationSource.SERVER)
        self.assertFalse(result.used_cached_content)

    def test_equal_revision_still_uses_server_content_source_server(self):
        """revision이 같아도(사실상 캐시와 동일) 서버 확인에는 성공했으므로 source=server."""
        fetch = lambda n: _FakeRecord(content="same", revision=3)
        result = verify_message_before_send(
            message_no=2, local_content="same", local_revision=3,
            fetch_fn=fetch, policy=OfflineSendPolicy.BLOCK, timeout_seconds=5,
            retry_count=0, service_enabled=True,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.source, VerificationSource.SERVER)
        self.assertFalse(result.used_cached_content)

    def test_verified_at_is_populated(self):
        result = verify_message_before_send(
            message_no=1, local_content="x", local_revision=1,
            fetch_fn=lambda n: _FakeRecord(content="x", revision=1),
            policy=OfflineSendPolicy.BLOCK, timeout_seconds=5, retry_count=0,
            service_enabled=True,
        )
        self.assertTrue(result.verified_at)


class TestMessageNotFound(unittest.TestCase):
    def test_none_record_treated_as_not_found_then_policy_applies(self):
        result = verify_message_before_send(
            message_no=1, local_content="cache", local_revision=1,
            fetch_fn=lambda n: None, policy=OfflineSendPolicy.BLOCK,
            timeout_seconds=5, retry_count=0, service_enabled=True,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.error_code, VerificationErrorCode.MESSAGE_NOT_FOUND)


class TestBlockPolicy(unittest.TestCase):
    def test_network_error_blocks_send(self):
        fetch = _FailingFetch(ConnectionError("net down"))
        result = verify_message_before_send(
            message_no=1, local_content="cache", local_revision=1,
            fetch_fn=fetch, policy=OfflineSendPolicy.BLOCK, timeout_seconds=5,
            retry_count=1, service_enabled=True,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.source, VerificationSource.UNAVAILABLE)
        self.assertFalse(result.used_cached_content)
        self.assertEqual(result.error_code, VerificationErrorCode.NETWORK_ERROR)

    def test_retry_count_controls_attempt_count(self):
        fetch = _FailingFetch(ConnectionError("boom"))
        verify_message_before_send(
            message_no=1, local_content="x", local_revision=1,
            fetch_fn=fetch, policy=OfflineSendPolicy.BLOCK, timeout_seconds=5,
            retry_count=2, service_enabled=True,
        )
        self.assertEqual(fetch.call_count, 3)  # 최초 시도 + 재시도 2회

    def test_retry_succeeds_on_second_attempt(self):
        fetch = _FailingFetch(ConnectionError("temporary"), fail_times=1)
        result = verify_message_before_send(
            message_no=1, local_content="x", local_revision=1,
            fetch_fn=fetch, policy=OfflineSendPolicy.BLOCK, timeout_seconds=5,
            retry_count=2, service_enabled=True,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.content, "recovered")

    def test_timeout_blocks_send(self):
        fetch = _SlowFetch()
        result = verify_message_before_send(
            message_no=1, local_content="cache", local_revision=1,
            fetch_fn=fetch, policy=OfflineSendPolicy.BLOCK, timeout_seconds=0.2,
            retry_count=0, service_enabled=True,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.error_code, VerificationErrorCode.TIMEOUT)


class TestCachedPolicy(unittest.TestCase):
    def test_network_error_uses_cached_content(self):
        fetch = _FailingFetch(ConnectionError("net down"))
        result = verify_message_before_send(
            message_no=1, local_content="cache-content", local_revision=1,
            fetch_fn=fetch, policy=OfflineSendPolicy.CACHED, timeout_seconds=5,
            retry_count=1, service_enabled=True,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.content, "cache-content")
        self.assertTrue(result.used_cached_content)
        self.assertEqual(result.source, VerificationSource.LOCAL_CACHE)
        self.assertEqual(result.error_code, VerificationErrorCode.NETWORK_ERROR)

    def test_timeout_uses_cached_content(self):
        fetch = _SlowFetch()
        result = verify_message_before_send(
            message_no=1, local_content="cache-content", local_revision=1,
            fetch_fn=fetch, policy=OfflineSendPolicy.CACHED, timeout_seconds=0.2,
            retry_count=0, service_enabled=True,
        )
        self.assertTrue(result.allowed)
        self.assertTrue(result.used_cached_content)
        self.assertEqual(result.error_code, VerificationErrorCode.TIMEOUT)


class TestOfflineSendPolicyParsing(unittest.TestCase):
    def test_parses_block(self):
        self.assertEqual(OfflineSendPolicy.from_str("block"), OfflineSendPolicy.BLOCK)

    def test_parses_cached_case_insensitive(self):
        self.assertEqual(OfflineSendPolicy.from_str("CACHED"), OfflineSendPolicy.CACHED)

    def test_unknown_value_defaults_to_block(self):
        self.assertEqual(OfflineSendPolicy.from_str("garbage"), OfflineSendPolicy.BLOCK)


if __name__ == "__main__":
    unittest.main()
