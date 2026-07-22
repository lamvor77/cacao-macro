# services/realtime_protocol_compat.py 테스트
#
# 실제 Supabase Realtime 서버에는 연결하지 않는다. CompatAsyncRealtimeClient는
# site-packages의 실제 AsyncRealtimeClient를 그대로 상속하되, _ws_connection과
# channels만 테스트용 가짜 값으로 채워 _listen()을 직접 실행한다(실제 사용
# 방식과 동일 — realtime 라이브러리가 내부적으로 이 메서드를 호출하는 구조
# 그대로 재현).

import json
import logging
import unittest
from unittest.mock import patch

from realtime import AsyncRealtimeClient

from services.realtime_protocol_compat import (
    CompatAsyncRealtimeClient,
    create_compat_realtime_client,
    normalize_realtime_message,
)

_TOPIC = "realtime:public:shared_messages"


def _postgres_changes_payload(message_no=3, revision=7):
    return {
        "data": {
            "schema": "public",
            "table": "shared_messages",
            "commit_timestamp": "2026-07-21T00:00:00Z",
            "type": "UPDATE",
            "errors": None,
            "columns": [],
            "record": {
                "message_no": message_no, "revision": revision, "content": "이것은 절대 로그에 남으면 안 되는 본문",
                "title": None, "updated_by": None, "updated_by_name": None,
                "update_source": "mobile", "updated_at": "2026-07-21T00:00:00Z",
            },
        },
        "ids": [1],
    }


def _array_message(topic=_TOPIC, event="postgres_changes", payload=None, join_ref=None, ref=None):
    return json.dumps([join_ref, ref, topic, event, payload if payload is not None else _postgres_changes_payload()])


class _FakeChannel:
    def __init__(self):
        self.received = []

    def _handle_message(self, message):
        self.received.append(message)


class _FakeWSConnection:
    """async for msg in connection 형태만 흉내낸다(정상 종료 시 예외 없이 끝남)."""

    def __init__(self, messages):
        self._messages = messages

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m


def _make_client() -> CompatAsyncRealtimeClient:
    return CompatAsyncRealtimeClient("wss://example.supabase.co/realtime/v1", token="anon-key")


class TestNormalizeRealtimeMessage(unittest.TestCase):
    def test_object_message_passes_through_unchanged(self):
        raw = json.dumps({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": "1"})
        self.assertEqual(normalize_realtime_message(raw), raw)

    def test_array_of_five_is_normalized_to_object(self):
        raw = _array_message(join_ref="1", ref="2")
        normalized = normalize_realtime_message(raw)
        obj = json.loads(normalized)
        self.assertEqual(obj["join_ref"], "1")
        self.assertEqual(obj["ref"], "2")
        self.assertEqual(obj["topic"], _TOPIC)
        self.assertEqual(obj["event"], "postgres_changes")
        self.assertIn("data", obj["payload"])

    def test_null_join_ref_and_ref_allowed(self):
        raw = _array_message(join_ref=None, ref=None)
        normalized = normalize_realtime_message(raw)
        obj = json.loads(normalized)
        self.assertIsNone(obj["join_ref"])
        self.assertIsNone(obj["ref"])

    def test_postgres_changes_payload_preserved_intact(self):
        payload = _postgres_changes_payload(message_no=9, revision=42)
        raw = _array_message(payload=payload)
        normalized = normalize_realtime_message(raw)
        obj = json.loads(normalized)
        self.assertEqual(obj["payload"], payload)

    def test_wrong_length_array_returned_unchanged(self):
        raw = json.dumps(["only", "four", "items", "here"])
        self.assertEqual(normalize_realtime_message(raw), raw)

    def test_non_string_topic_returned_unchanged(self):
        raw = json.dumps([None, None, 123, "postgres_changes", {}])
        self.assertEqual(normalize_realtime_message(raw), raw)

    def test_non_dict_payload_returned_unchanged(self):
        raw = json.dumps([None, None, _TOPIC, "postgres_changes", "not-a-dict"])
        self.assertEqual(normalize_realtime_message(raw), raw)

    def test_malformed_json_returned_unchanged(self):
        raw = "{not valid json"
        self.assertEqual(normalize_realtime_message(raw), raw)


class TestCompatAsyncRealtimeClientListen(unittest.IsolatedAsyncioTestCase):
    async def test_array_update_message_reaches_channel_callback(self):
        client = _make_client()
        channel = _FakeChannel()
        client.channels[_TOPIC] = channel
        client._ws_connection = _FakeWSConnection([_array_message()])

        await client._listen()

        self.assertEqual(len(channel.received), 1)
        self.assertEqual(channel.received[0].topic, _TOPIC)
        self.assertEqual(channel.received[0].event, "postgres_changes")

    async def test_object_message_still_works_as_before(self):
        client = _make_client()
        channel = _FakeChannel()
        client.channels[_TOPIC] = channel
        obj_msg = json.dumps({
            "topic": _TOPIC, "event": "postgres_changes", "ref": None,
            "payload": _postgres_changes_payload(),
        })
        client._ws_connection = _FakeWSConnection([obj_msg])

        await client._listen()

        self.assertEqual(len(channel.received), 1)

    async def test_malformed_array_is_skipped_but_next_message_still_processed(self):
        client = _make_client()
        channel = _FakeChannel()
        client.channels[_TOPIC] = channel
        bad = json.dumps(["too", "few", "items"])
        good = _array_message()
        client._ws_connection = _FakeWSConnection([bad, good])

        await client._listen()

        self.assertEqual(len(channel.received), 1, "잘못된 메시지 하나가 이후 정상 메시지 처리를 막으면 안 됨")

    async def test_unmatched_topic_is_silently_ignored(self):
        client = _make_client()
        client._ws_connection = _FakeWSConnection([_array_message(topic="realtime:public:other_table")])
        try:
            await client._listen()
        except Exception as e:
            self.fail(f"등록되지 않은 topic 메시지가 예외를 일으키면 안 됨: {e}")


class TestDiagnosticLogGating(unittest.IsolatedAsyncioTestCase):
    async def test_no_log_when_not_test_environment(self):
        client = _make_client()
        client.channels[_TOPIC] = _FakeChannel()
        client._ws_connection = _FakeWSConnection([_array_message()])

        with patch("services.realtime_protocol_compat.IS_TEST_ENVIRONMENT", False):
            with self.assertRaises(AssertionError):
                with self.assertLogs("services.realtime_protocol_compat", level="INFO"):
                    await client._listen()

    async def test_log_emitted_when_test_environment_and_never_contains_message_body(self):
        client = _make_client()
        client.channels[_TOPIC] = _FakeChannel()
        client._ws_connection = _FakeWSConnection([_array_message()])

        with patch("services.realtime_protocol_compat.IS_TEST_ENVIRONMENT", True):
            with self.assertLogs("services.realtime_protocol_compat", level="INFO") as cm:
                await client._listen()

        joined = " ".join(cm.output)
        self.assertIn("message_no=3", joined)
        self.assertIn("revision=7", joined)
        self.assertNotIn("절대 로그에 남으면 안 되는", joined)
        self.assertNotIn("anon-key", joined)


class TestCreateCompatRealtimeClient(unittest.TestCase):
    def test_compat_enabled_returns_subclass(self):
        client = create_compat_realtime_client("wss://example.supabase.co/realtime/v1", token="k", compat_enabled=True)
        self.assertIsInstance(client, CompatAsyncRealtimeClient)

    def test_compat_disabled_returns_original_class_exactly(self):
        client = create_compat_realtime_client("wss://example.supabase.co/realtime/v1", token="k", compat_enabled=False)
        self.assertIs(type(client), AsyncRealtimeClient, "REALTIME_PROTOCOL_COMPAT=false면 원본 클래스를 그대로 써야 함(서브클래스 아님)")

    def test_version_mismatch_logs_warning(self):
        with patch("services.realtime_protocol_compat._installed_realtime_version", return_value="9.9.9"):
            with self.assertLogs("services.realtime_protocol_compat", level="WARNING") as cm:
                create_compat_realtime_client("wss://example.supabase.co/realtime/v1", token="k")
        self.assertTrue(any("9.9.9" in line for line in cm.output))

    def test_matching_version_does_not_warn(self):
        with patch("services.realtime_protocol_compat._installed_realtime_version", return_value="2.31.0"):
            try:
                with self.assertLogs("services.realtime_protocol_compat", level="WARNING"):
                    create_compat_realtime_client("wss://example.supabase.co/realtime/v1", token="k")
            except AssertionError:
                pass  # 로그가 없는 것이 정상(assertLogs가 "로그 없음"이면 AssertionError를 던짐)
            else:
                self.fail("버전이 일치하는데 경고 로그가 남으면 안 됨")


class TestInheritanceScope(unittest.TestCase):
    def test_only_listen_is_overridden(self):
        """요구사항 9절 — connect/reconnect/heartbeat/channel 관리 로직을 다시
        구현하지 않고 전부 상속으로 재사용했는지 고정한다."""
        overridden = [name for name, value in vars(CompatAsyncRealtimeClient).items() if not name.startswith("__")]
        self.assertEqual(overridden, ["_listen"])


if __name__ == "__main__":
    unittest.main()
