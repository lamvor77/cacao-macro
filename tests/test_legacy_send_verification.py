# core/legacy_send_verification.py 테스트
#
# 네트워크를 전혀 쓰지 않는다 — fetch_fn/push_fn 전부 fake로 주입한다.

import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock

from core.legacy_send_verification import (
    MessageClassification,
    classify_message,
    verify_legacy_message_before_send,
)
from core.send_verification import OfflineSendPolicy, VerificationErrorCode, VerificationSource


@dataclass
class _FakeRecord:
    version: int
    text: str


class TestClassifyMessage(unittest.TestCase):
    """CloudSyncCoordinator._reconcile()의 기존 6가지 규칙과 동일해야 한다."""

    def test_no_cloud_record_with_local_text_is_push(self):
        self.assertEqual(
            classify_message(dirty=False, local_version=0, last_synced_text="", local_text="hello",
                              cloud_version=None, cloud_text=None),
            MessageClassification.PUSH,
        )

    def test_no_cloud_record_with_empty_local_text_is_noop(self):
        self.assertEqual(
            classify_message(dirty=False, local_version=0, last_synced_text="", local_text="",
                              cloud_version=None, cloud_text=None),
            MessageClassification.NOOP,
        )

    def test_not_dirty_and_cloud_newer_is_remote_apply(self):
        self.assertEqual(
            classify_message(dirty=False, local_version=1, last_synced_text="a", local_text="a",
                              cloud_version=2, cloud_text="b"),
            MessageClassification.REMOTE_APPLY,
        )

    def test_not_dirty_and_cloud_not_newer_is_noop(self):
        self.assertEqual(
            classify_message(dirty=False, local_version=2, last_synced_text="a", local_text="a",
                              cloud_version=2, cloud_text="a"),
            MessageClassification.NOOP,
        )

    def test_dirty_and_texts_match_is_identical(self):
        self.assertEqual(
            classify_message(dirty=True, local_version=1, last_synced_text="old", local_text="same",
                              cloud_version=2, cloud_text="same"),
            MessageClassification.IDENTICAL,
        )

    def test_dirty_and_cloud_version_not_ahead_is_local_pending(self):
        self.assertEqual(
            classify_message(dirty=True, local_version=3, last_synced_text="old", local_text="new",
                              cloud_version=2, cloud_text="remote-old"),
            MessageClassification.LOCAL_PENDING,
        )

    def test_dirty_and_cloud_text_equals_last_synced_is_local_pending(self):
        # 원격 버전만 앞섰지 실제 내용은 내가 마지막으로 확인한 그대로 — 내 자신의
        # 이전 push가 아직 로컬에 반영되지 않았을 뿐인 정상 대기.
        self.assertEqual(
            classify_message(dirty=True, local_version=1, last_synced_text="synced-value", local_text="new",
                              cloud_version=5, cloud_text="synced-value"),
            MessageClassification.LOCAL_PENDING,
        )

    def test_dirty_and_cloud_actually_diverged_is_conflict(self):
        self.assertEqual(
            classify_message(dirty=True, local_version=1, last_synced_text="synced-value", local_text="my-new",
                              cloud_version=5, cloud_text="someone-elses-new"),
            MessageClassification.CONFLICT,
        )


def _verify(
    message_no=1, local_content="local", is_dirty=False, local_version=1, last_synced_text="local",
    is_editing=False, fetch_fn=None, push_fn=None, policy=OfflineSendPolicy.BLOCK,
    timeout_seconds=1.0, retry_count=0, service_enabled=True,
):
    return verify_legacy_message_before_send(
        message_no=message_no, local_content=local_content, is_dirty=is_dirty,
        local_version=local_version, last_synced_text=last_synced_text, is_editing=is_editing,
        fetch_fn=fetch_fn or (lambda n: _FakeRecord(version=1, text="local")),
        push_fn=push_fn or (lambda n, c: True),
        policy=policy, timeout_seconds=timeout_seconds, retry_count=retry_count,
        service_enabled=service_enabled,
    )


class TestVerifyLegacyMessageBeforeSend(unittest.TestCase):
    def test_service_disabled_short_circuits(self):
        result = _verify(service_enabled=False)
        self.assertTrue(result.allowed)
        self.assertEqual(result.error_code, VerificationErrorCode.SERVICE_DISABLED)
        self.assertEqual(result.source, VerificationSource.LOCAL_CACHE)

    def test_remote_apply_not_editing_uses_server_content(self):
        result = _verify(
            local_content="stale", is_dirty=False, local_version=1,
            fetch_fn=lambda n: _FakeRecord(version=2, text="fresh-from-server"),
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.content, "fresh-from-server")
        self.assertEqual(result.source, VerificationSource.SERVER)
        self.assertIsNone(result.error_code)

    def test_remote_apply_while_editing_blocks_send(self):
        result = _verify(
            local_content="stale", is_dirty=False, local_version=1, is_editing=True,
            fetch_fn=lambda n: _FakeRecord(version=2, text="fresh-from-server"),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.error_code, VerificationErrorCode.EDIT_IN_PROGRESS)
        self.assertIn("편집을 종료하고 새로고침", result.error_message)

    def test_local_pending_pushes_then_sends_local_content(self):
        push_calls = []

        def push_fn(n, c):
            push_calls.append((n, c))
            return True

        result = _verify(
            local_content="my-edit", is_dirty=True, local_version=1, last_synced_text="synced",
            fetch_fn=lambda n: _FakeRecord(version=5, text="synced"),
            push_fn=push_fn,
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.content, "my-edit")
        self.assertEqual(push_calls, [(1, "my-edit")])

    def test_local_pending_push_failure_blocks_send(self):
        result = _verify(
            local_content="my-edit", is_dirty=True, local_version=1, last_synced_text="synced",
            fetch_fn=lambda n: _FakeRecord(version=5, text="synced"),
            push_fn=lambda n, c: False,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.error_code, VerificationErrorCode.NETWORK_ERROR)

    def test_local_pending_push_exception_blocks_send(self):
        def bad_push(n, c):
            raise RuntimeError("boom")

        result = _verify(
            local_content="my-edit", is_dirty=True, local_version=1, last_synced_text="synced",
            fetch_fn=lambda n: _FakeRecord(version=5, text="synced"),
            push_fn=bad_push,
        )
        self.assertFalse(result.allowed)

    def test_conflict_blocks_send(self):
        result = _verify(
            local_content="my-new", is_dirty=True, local_version=1, last_synced_text="synced",
            fetch_fn=lambda n: _FakeRecord(version=5, text="someone-elses-new"),
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.error_code, VerificationErrorCode.CONFLICT)
        self.assertIn("발송을 중단", result.error_message)

    def test_identical_allows_send_without_push(self):
        push_fn = MagicMock(return_value=True)
        result = _verify(
            local_content="same", is_dirty=True, local_version=1, last_synced_text="old",
            fetch_fn=lambda n: _FakeRecord(version=5, text="same"),
            push_fn=push_fn,
        )
        self.assertTrue(result.allowed)
        push_fn.assert_not_called()

    def test_noop_allows_send_with_local_content(self):
        result = _verify(
            local_content="a", is_dirty=False, local_version=2,
            fetch_fn=lambda n: _FakeRecord(version=2, text="a"),
        )
        self.assertTrue(result.allowed)
        self.assertEqual(result.content, "a")

    def test_fetch_failure_block_policy_blocks_send(self):
        def bad_fetch(n):
            raise ConnectionError("network down")

        result = _verify(fetch_fn=bad_fetch, policy=OfflineSendPolicy.BLOCK, retry_count=0)
        self.assertFalse(result.allowed)
        self.assertEqual(result.error_code, VerificationErrorCode.NETWORK_ERROR)

    def test_fetch_failure_cached_policy_sends_local_content(self):
        def bad_fetch(n):
            raise ConnectionError("network down")

        result = _verify(local_content="cached-value", fetch_fn=bad_fetch, policy=OfflineSendPolicy.CACHED, retry_count=0)
        self.assertTrue(result.allowed)
        self.assertEqual(result.content, "cached-value")
        self.assertTrue(result.used_cached_content)

    def test_fetch_timeout_reports_timeout_error_code(self):
        import time

        def slow_fetch(n):
            time.sleep(0.5)
            return _FakeRecord(version=1, text="x")

        result = _verify(fetch_fn=slow_fetch, timeout_seconds=0.05, retry_count=0, policy=OfflineSendPolicy.BLOCK)
        self.assertFalse(result.allowed)
        self.assertEqual(result.error_code, VerificationErrorCode.TIMEOUT)

    def test_message_not_found_treated_as_offline_policy(self):
        result = _verify(fetch_fn=lambda n: None, policy=OfflineSendPolicy.BLOCK, retry_count=0)
        self.assertFalse(result.allowed)
        self.assertEqual(result.error_code, VerificationErrorCode.MESSAGE_NOT_FOUND)

    def test_retry_then_succeed(self):
        attempts = {"n": 0}

        def flaky_fetch(n):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ConnectionError("temporary")
            return _FakeRecord(version=1, text="ok")

        result = _verify(local_content="ok", is_dirty=False, local_version=1, fetch_fn=flaky_fetch, retry_count=2)
        self.assertTrue(result.allowed)
        self.assertEqual(attempts["n"], 2)


if __name__ == "__main__":
    unittest.main()
