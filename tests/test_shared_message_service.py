# services/shared_message_service.py 단위 테스트
#
# 실제 Supabase에는 절대 연결하지 않는다 — client.table(...)/.rpc(...).execute()를
# 흉내내는 fake만 사용한다(tests/test_admin_service.py와 동일한 패턴).
#
# 실행: python -m unittest tests.test_shared_message_service -v

import unittest
from types import SimpleNamespace

from postgrest.exceptions import APIError as PostgrestAPIError

from services.shared_message_service import (
    SharedMessageConflictError,
    SharedMessageError,
    SharedMessageNotFoundError,
    SharedMessagePermissionError,
    SharedMessageRecord,
    SharedMessageService,
    SharedMessageValidationError,
    _translate_rpc_error,
    is_untouched_seed,
    validate_message_no,
)
from services.supabase_client import ClientResult


def _api_error(message: str) -> PostgrestAPIError:
    return PostgrestAPIError({"message": message, "code": "P0001"})


def _row(message_no=1, content="hello", revision=1, update_source="system", **kwargs):
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "message_no": message_no,
        "title": None,
        "content": content,
        "revision": revision,
        "is_active": True,
        "updated_at": "2026-07-19T00:00:00Z",
        "updated_by": None,
        "updated_by_name": None,
        "update_source": update_source,
        "created_at": "2026-07-19T00:00:00Z",
    }
    row.update(kwargs)
    return row


# ============================================================
# fake — .table(...).select(...).eq(...).order(...).range(...).limit(...).execute()
# ============================================================

class FakeQuery:
    def __init__(self, data=None, error: Exception = None):
        self._data = data
        self._error = error
        self.calls: list = []

    def select(self, *a, **kw):
        self.calls.append(("select", a, kw))
        return self

    def eq(self, *a, **kw):
        self.calls.append(("eq", a, kw))
        return self

    def order(self, *a, **kw):
        self.calls.append(("order", a, kw))
        return self

    def range(self, *a, **kw):
        self.calls.append(("range", a, kw))
        return self

    def limit(self, *a, **kw):
        self.calls.append(("limit", a, kw))
        return self

    def execute(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(data=self._data)


class FakeRPCQuery:
    def __init__(self, data=None, error: Exception = None):
        self._data = data
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(data=self._data)


class FakeClient:
    def __init__(self):
        self.rpc_calls: list[tuple[str, dict]] = []
        self.next_table_data = []
        self.next_table_error: Exception = None
        self.next_rpc_data = None
        self.next_rpc_error: Exception = None
        self.last_table_query: FakeQuery = None

    def table(self, name: str):
        q = FakeQuery(self.next_table_data, self.next_table_error)
        self.last_table_query = q
        return q

    def rpc(self, fn_name: str, params: dict):
        self.rpc_calls.append((fn_name, dict(params)))
        return FakeRPCQuery(self.next_rpc_data, self.next_rpc_error)


class FakeClientManager:
    def __init__(self, client: FakeClient):
        self._client = client

    def get_client(self) -> ClientResult:
        return ClientResult(True, client=self._client)


class FakeFailingClientManager:
    def get_client(self) -> ClientResult:
        return ClientResult(False, error="연결 실패", error_code="connection_error")


def _make_service():
    client = FakeClient()
    mgr = FakeClientManager(client)
    return SharedMessageService(client_manager=mgr), client


# ============================================================
# 1. message_no 검증
# ============================================================

class TestValidateMessageNo(unittest.TestCase):
    def test_valid_range_accepted(self):
        for n in range(1, 13):
            validate_message_no(n)  # 예외 없이 통과해야 함

    def test_zero_rejected(self):
        with self.assertRaises(SharedMessageValidationError):
            validate_message_no(0)

    def test_thirteen_rejected(self):
        with self.assertRaises(SharedMessageValidationError):
            validate_message_no(13)

    def test_negative_rejected(self):
        with self.assertRaises(SharedMessageValidationError):
            validate_message_no(-1)

    def test_bool_rejected_even_though_bool_is_int_subclass(self):
        with self.assertRaises(SharedMessageValidationError):
            validate_message_no(True)


# ============================================================
# 2. 조회
# ============================================================

class TestListAndGetMessages(unittest.TestCase):
    def test_list_messages_parses_rows(self):
        svc, client = _make_service()
        client.next_table_data = [_row(message_no=1), _row(message_no=2, content="world")]
        records = svc.list_messages()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].message_no, 1)
        self.assertEqual(records[1].content, "world")

    def test_get_message_validates_message_no_first(self):
        svc, _ = _make_service()
        with self.assertRaises(SharedMessageValidationError):
            svc.get_message(99)

    def test_get_message_returns_none_when_not_found(self):
        svc, client = _make_service()
        client.next_table_data = []
        self.assertIsNone(svc.get_message(1))

    def test_get_message_returns_record(self):
        svc, client = _make_service()
        client.next_table_data = [_row(message_no=5, content="five")]
        record = svc.get_message(5)
        self.assertEqual(record.content, "five")

    def test_disconnected_client_raises_shared_message_error(self):
        svc = SharedMessageService(client_manager=FakeFailingClientManager())
        with self.assertRaises(SharedMessageError):
            svc.list_messages()


# ============================================================
# 3. 저장(update_message) — RPC 파라미터/오류 변환
# ============================================================

class TestUpdateMessage(unittest.TestCase):
    def test_update_message_calls_rpc_with_correct_params(self):
        svc, client = _make_service()
        client.next_rpc_data = _row(message_no=3, content="new", revision=2, update_source="desktop")
        record = svc.update_message(3, None, "new", base_revision=1, update_source="desktop")

        self.assertEqual(len(client.rpc_calls), 1)
        fn_name, params = client.rpc_calls[0]
        self.assertEqual(fn_name, "update_shared_message")
        self.assertEqual(params, {
            "p_message_no": 3, "p_title": None, "p_content": "new",
            "p_base_revision": 1, "p_update_source": "desktop",
        })
        self.assertEqual(record.revision, 2)

    def test_invalid_message_no_rejected_locally_without_rpc_call(self):
        svc, client = _make_service()
        with self.assertRaises(SharedMessageValidationError):
            svc.update_message(0, None, "x", base_revision=1, update_source="desktop")
        self.assertEqual(client.rpc_calls, [], "검증 실패 시 RPC를 호출하면 안 됨")

    def test_invalid_update_source_rejected_locally(self):
        svc, client = _make_service()
        with self.assertRaises(SharedMessageValidationError):
            svc.update_message(1, None, "x", base_revision=1, update_source="not-a-real-source")
        self.assertEqual(client.rpc_calls, [])

    def test_revision_conflict_error_translated(self):
        svc, client = _make_service()
        client.next_rpc_error = _api_error("REVISION_CONFLICT: 다른 사용자가 먼저 저장했습니다(현재 revision=5).")
        with self.assertRaises(SharedMessageConflictError):
            svc.update_message(1, None, "x", base_revision=1, update_source="desktop")

    def test_permission_denied_error_translated(self):
        svc, client = _make_service()
        client.next_rpc_error = _api_error("PERMISSION_DENIED: 메시지를 수정할 권한이 없습니다.")
        with self.assertRaises(SharedMessagePermissionError):
            svc.update_message(1, None, "x", base_revision=1, update_source="desktop")

    def test_message_not_found_error_translated(self):
        svc, client = _make_service()
        client.next_rpc_error = _api_error("MESSAGE_NOT_FOUND: message_no=1를 찾을 수 없습니다.")
        with self.assertRaises(SharedMessageNotFoundError):
            svc.update_message(1, None, "x", base_revision=1, update_source="desktop")

    def test_unknown_rpc_error_wrapped_generically(self):
        svc, client = _make_service()
        client.next_rpc_error = _api_error("SOME_UNMAPPED_CODE: 알 수 없음")
        with self.assertRaises(SharedMessageError):
            svc.update_message(1, None, "x", base_revision=1, update_source="desktop")

    def test_migration_source_rejected_locally_in_normal_update(self):
        """Production Stabilization Sprint — SQL이 update_shared_message에서
        migration/admin_force를 더 이상 허용하지 않도록 강화됨에 맞춰, 서비스
        계층도 두 값을 일반 저장에서 미리 걸러낸다(불필요한 RPC 왕복 방지)."""
        svc, client = _make_service()
        with self.assertRaises(SharedMessageValidationError):
            svc.update_message(1, None, "x", base_revision=1, update_source="migration")
        self.assertEqual(client.rpc_calls, [])

    def test_admin_force_source_rejected_locally_in_normal_update(self):
        svc, client = _make_service()
        with self.assertRaises(SharedMessageValidationError):
            svc.update_message(1, None, "x", base_revision=1, update_source="admin_force")
        self.assertEqual(client.rpc_calls, [])


# ============================================================
# 4. 강제 저장(force_update_message)
# ============================================================

class TestForceUpdateMessage(unittest.TestCase):
    def test_force_update_calls_correct_rpc_without_base_revision(self):
        svc, client = _make_service()
        client.next_rpc_data = _row(message_no=1, revision=9, update_source="admin_force")
        record = svc.force_update_message(1, "제목", "강제 내용", update_source="admin_force")

        fn_name, params = client.rpc_calls[0]
        self.assertEqual(fn_name, "force_update_shared_message")
        self.assertNotIn("p_base_revision", params)
        self.assertEqual(record.revision, 9)

    def test_force_update_permission_denied_translated(self):
        svc, client = _make_service()
        client.next_rpc_error = _api_error("PERMISSION_DENIED: 강제 저장은 관리자만 수행할 수 있습니다.")
        with self.assertRaises(SharedMessagePermissionError):
            svc.force_update_message(1, None, "x", update_source="admin_force")

    def test_force_update_migration_source_accepted(self):
        svc, client = _make_service()
        client.next_rpc_data = _row(message_no=1, revision=2, update_source="migration")
        svc.force_update_message(1, None, "x", update_source="migration")
        self.assertEqual(client.rpc_calls[0][1]["p_update_source"], "migration")

    def test_force_update_desktop_source_rejected_locally(self):
        """force_update는 이제 migration/admin_force만 허용 — desktop/mobile은
        일반 update_message로만 저장해야 한다(OCC 우회 방지)."""
        svc, client = _make_service()
        with self.assertRaises(SharedMessageValidationError):
            svc.force_update_message(1, None, "x", update_source="desktop")
        self.assertEqual(client.rpc_calls, [])


# ============================================================
# 5. is_untouched_seed — 초기 마이그레이션 판단
# ============================================================

class TestIsUntouchedSeed(unittest.TestCase):
    def test_fresh_seed_is_untouched(self):
        record = SharedMessageRecord.from_row(_row(revision=1, update_source="system"))
        self.assertTrue(is_untouched_seed(record))

    def test_edited_record_is_not_untouched(self):
        record = SharedMessageRecord.from_row(_row(revision=2, update_source="desktop"))
        self.assertFalse(is_untouched_seed(record))

    def test_revision_1_but_non_system_source_is_not_untouched(self):
        # revision=1이라도 이미 한 번 실제로 수정되었다면(예: 강제 저장으로 revision을
        # 유지한 채 update_source만 바뀌는 경우는 실제로 없지만) system이 아니면 안전하게 "수정됨"으로 취급.
        record = SharedMessageRecord.from_row(_row(revision=1, update_source="mobile"))
        self.assertFalse(is_untouched_seed(record))


# ============================================================
# 6. _translate_rpc_error 직접 테스트
# ============================================================

class TestTranslateRpcError(unittest.TestCase):
    def test_unmapped_code_returns_generic_error(self):
        exc = _translate_rpc_error(_api_error("NOT_A_KNOWN_CODE: 뭔가"))
        self.assertIsInstance(exc, SharedMessageError)
        self.assertNotIsInstance(exc, SharedMessageConflictError)

    def test_message_without_colon_prefix_returns_generic_error(self):
        exc = _translate_rpc_error(_api_error("이상한 형식의 오류"))
        self.assertIsInstance(exc, SharedMessageError)


if __name__ == "__main__":
    unittest.main()
