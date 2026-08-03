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
    SharedMessageAuthError,
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
    def __init__(self, client: FakeClient, config_url: str = "https://testrefabc.supabase.co"):
        self._client = client
        self.config = SimpleNamespace(url=config_url)

    def get_client(self) -> ClientResult:
        return ClientResult(True, client=self._client)


class FakeFailingClientManager:
    def get_client(self) -> ClientResult:
        return ClientResult(False, error="연결 실패", error_code="connection_error")


def _make_service(user_id_fn=None, config_url="https://testrefabc.supabase.co", ensure_logged_in_fn=None):
    client = FakeClient()
    mgr = FakeClientManager(client, config_url=config_url)
    return (
        SharedMessageService(
            client_manager=mgr, user_id_fn=user_id_fn, ensure_logged_in_fn=ensure_logged_in_fn,
        ),
        client,
    )


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


# ============================================================
# 7. APIError 상세 로그 — code/message/details/hint + 호출 컨텍스트
# ============================================================
# 배경: 지금까지 실패 로그가 "SharedMessageService: 알 수 없는 RPC 오류 유형
# (APIError)"만 남아 shared_messages 저장/조회 실패 원인을 알 수 없었다.
# 아래 테스트는 5개 실패 지점(초기 조회/update/force_update/발송 직전 조회와
# 동일한 get_message/이력 조회) 각각에서 code/message/details/hint와
# operation/message_no/user_id(앞 8자)/project_ref가 실제로 로그에 남는지,
# 그리고 토큰·anon key·메시지 본문 전체는 남지 않는지 확인한다.

def _detailed_api_error(message: str) -> PostgrestAPIError:
    return PostgrestAPIError({
        "message": message, "code": "42501",
        "details": "일부 컬럼에 대한 권한이 없습니다.", "hint": "GRANT 문으로 권한을 부여하세요.",
    })


class TestApiErrorDetailedLogging(unittest.TestCase):
    _USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def _assert_detail_fields_logged(self, joined_logs: str, operation: str, message_no, rpc=None):
        self.assertIn("42501", joined_logs)
        self.assertIn("일부 컬럼에 대한 권한이 없습니다.", joined_logs)
        self.assertIn("GRANT 문으로 권한을 부여하세요.", joined_logs)
        self.assertIn(f"operation={operation}", joined_logs)
        self.assertIn(f"message_no={message_no}", joined_logs)
        self.assertIn("user=aaaaaaaa", joined_logs)  # UUID 앞 8자만
        self.assertIn("project=testrefabc", joined_logs)
        if rpc is not None:
            self.assertIn(f"rpc={rpc}", joined_logs)

    def test_1_list_messages_logs_full_detail(self):
        svc, client = _make_service(user_id_fn=lambda: self._USER_ID)
        client.next_table_error = _detailed_api_error("permission denied for table shared_messages")
        with self.assertLogs("services.shared_message_service", level="ERROR") as cm:
            with self.assertRaises(SharedMessageError):
                svc.list_messages()
        self._assert_detail_fields_logged("\n".join(cm.output), "list_messages", None)

    def test_2_get_message_logs_full_detail(self):
        svc, client = _make_service(user_id_fn=lambda: self._USER_ID)
        client.next_table_error = _detailed_api_error("permission denied for table shared_messages")
        with self.assertLogs("services.shared_message_service", level="ERROR") as cm:
            with self.assertRaises(SharedMessageError):
                svc.get_message(7)
        self._assert_detail_fields_logged("\n".join(cm.output), "get_message", 7)

    def test_3_list_history_logs_full_detail(self):
        svc, client = _make_service(user_id_fn=lambda: self._USER_ID)
        client.next_table_error = _detailed_api_error("permission denied for table shared_message_history")
        with self.assertLogs("services.shared_message_service", level="ERROR") as cm:
            with self.assertRaises(SharedMessageError):
                svc.list_history(message_no=3)
        self._assert_detail_fields_logged("\n".join(cm.output), "list_history", 3)

    def test_4_update_message_unknown_code_logs_full_detail(self):
        """update_shared_message — 발송 직전 흐름과 무관하게, "CODE: 메시지" 형식이지만
        매핑되지 않은 코드도 code/details/hint를 그대로 남겨야 한다."""
        svc, client = _make_service(user_id_fn=lambda: self._USER_ID)
        client.next_rpc_error = _detailed_api_error("SOME_UNMAPPED_CODE: 알 수 없는 오류")
        with self.assertLogs("services.shared_message_service", level="ERROR") as cm:
            with self.assertRaises(SharedMessageError):
                svc.update_message(2, None, "x", base_revision=1, update_source="desktop")
        joined = "\n".join(cm.output)
        self._assert_detail_fields_logged(joined, "update_message", 2, rpc="update_shared_message")
        self.assertIn("revision=1", joined)

    def test_5_force_update_message_unknown_code_logs_full_detail(self):
        svc, client = _make_service(user_id_fn=lambda: self._USER_ID)
        client.next_rpc_error = _detailed_api_error("SOME_UNMAPPED_CODE: 알 수 없는 오류")
        with self.assertLogs("services.shared_message_service", level="ERROR") as cm:
            with self.assertRaises(SharedMessageError):
                svc.force_update_message(4, None, "x", update_source="admin_force")
        self._assert_detail_fields_logged(
            "\n".join(cm.output), "force_update_message", 4, rpc="force_update_shared_message",
        )

    def test_non_apierror_exception_still_logs_context_without_crashing(self):
        """네트워크 오류 등 APIError가 아닌 예외는 code/details/hint 없이도
        operation/message_no/user/project 컨텍스트는 남아야 한다."""
        svc, client = _make_service(user_id_fn=lambda: self._USER_ID)
        client.next_table_error = TimeoutError("연결 시간 초과")
        with self.assertLogs("services.shared_message_service", level="ERROR") as cm:
            with self.assertRaises(SharedMessageError):
                svc.get_message(1)
        joined = "\n".join(cm.output)
        self.assertIn("operation=get_message", joined)
        self.assertIn("message_no=1", joined)
        self.assertIn("user=aaaaaaaa", joined)
        self.assertIn("project=testrefabc", joined)

    def test_missing_user_id_fn_logs_unknown_without_crashing(self):
        """user_id_fn을 주지 않아도(레거시 호출부 호환) 오류 로그 자체는 실패하지 않는다."""
        svc, client = _make_service(user_id_fn=None)
        client.next_table_error = _detailed_api_error("permission denied")
        with self.assertLogs("services.shared_message_service", level="ERROR") as cm:
            with self.assertRaises(SharedMessageError):
                svc.list_messages()
        self.assertIn("user=unknown", "\n".join(cm.output))

    def test_user_id_fn_exception_does_not_break_error_logging(self):
        """user_id_fn 콜백 자체가 예외를 던져도 원래 오류 처리/로그를 막지 않는다."""
        def _boom():
            raise RuntimeError("세션 조회 실패")

        svc, client = _make_service(user_id_fn=_boom)
        client.next_table_error = _detailed_api_error("permission denied")
        with self.assertLogs("services.shared_message_service", level="ERROR") as cm:
            with self.assertRaises(SharedMessageError):
                svc.list_messages()
        self.assertIn("user=unknown", "\n".join(cm.output))

    def test_message_content_and_title_never_logged(self):
        """message_no(슬롯 번호)는 로그에 남아도 되지만, 실제 저장하려던
        title/content(메시지 본문)는 절대 로그에 남으면 안 된다."""
        svc, client = _make_service(user_id_fn=lambda: self._USER_ID)
        client.next_rpc_error = _detailed_api_error("SOME_UNMAPPED_CODE: 알 수 없는 오류")
        secret_content = "이것은 매우 은밀한 실제 발송 메시지 본문입니다 12345"
        secret_title = "은밀한제목"
        with self.assertLogs("services.shared_message_service", level="ERROR") as cm:
            with self.assertRaises(SharedMessageError):
                svc.update_message(2, secret_title, secret_content, base_revision=1, update_source="desktop")
        joined = "\n".join(cm.output)
        self.assertNotIn(secret_content, joined)
        self.assertNotIn(secret_title, joined)

    def test_token_and_anon_key_never_logged(self):
        """SUPABASE_URL 전체나 anon key가 로그에 절대 남지 않는다 — project_ref만."""
        svc, client = _make_service(
            user_id_fn=lambda: self._USER_ID,
            config_url="https://realproject123.supabase.co",
        )
        client.next_table_error = _detailed_api_error("permission denied")
        with self.assertLogs("services.shared_message_service", level="ERROR") as cm:
            with self.assertRaises(SharedMessageError):
                svc.list_messages()
        joined = "\n".join(cm.output)
        self.assertIn("project=realproject123", joined)
        self.assertNotIn("https://", joined)
        self.assertNotIn(".supabase.co", joined)


# ============================================================
# 8. 인증 오류(SharedMessageAuthError) 분류 — 네트워크/RPC 오류와 구분
# ============================================================
# 배경: 공유 Supabase Client에 로그인 세션이 아직 반영되지 않은 상태로
# shared_messages 요청이 나가면(예: 앱 시작 직후 경쟁 상태), anon 권한으로
# 거부되어 PGRST301/42501류의 Postgres/PostgREST 오류가 난다 — 지금까지는
# 이것도 "SharedMessageService: 알 수 없는 RPC 오류 유형"으로만 뭉뚱그려져
# 순수 네트워크 오류와 구분할 수 없었다.

def _auth_like_api_error(code: str, message: str = "permission denied") -> PostgrestAPIError:
    return PostgrestAPIError({"message": message, "code": code})


class TestAuthErrorClassification(unittest.TestCase):
    def test_query_path_42501_classified_as_auth_error(self):
        svc, client = _make_service()
        client.next_table_error = _auth_like_api_error("42501")
        with self.assertRaises(SharedMessageAuthError):
            svc.list_messages()

    def test_query_path_pgrst301_classified_as_auth_error(self):
        svc, client = _make_service()
        client.next_table_error = _auth_like_api_error("PGRST301", "JWT expired")
        with self.assertRaises(SharedMessageAuthError):
            svc.get_message(1)

    def test_query_path_generic_timeout_not_classified_as_auth_error(self):
        """인증과 무관한(APIError조차 아닌) 순수 네트워크 오류는 여전히 일반
        SharedMessageError여야 한다 — 모든 실패를 인증 오류로 뭉뚱그리면 안 된다."""
        svc, client = _make_service()
        client.next_table_error = TimeoutError("연결 시간 초과")
        with self.assertRaises(SharedMessageError) as cm:
            svc.list_messages()
        self.assertNotIsInstance(cm.exception, SharedMessageAuthError)

    def test_rpc_path_unmapped_code_42501_classified_as_auth_error(self):
        svc, client = _make_service()
        client.next_rpc_error = _auth_like_api_error("42501", "permission denied for function update_shared_message")
        with self.assertRaises(SharedMessageAuthError):
            svc.update_message(1, None, "x", base_revision=1, update_source="desktop")

    def test_rpc_path_unmapped_code_other_not_classified_as_auth_error(self):
        """42501/PGRST301류가 아닌 다른 알 수 없는 코드는 여전히 일반
        SharedMessageError여야 한다(인증 오류로 과도하게 넓히지 않음)."""
        svc, client = _make_service()
        client.next_rpc_error = _api_error("SOME_UNMAPPED_CODE: 알 수 없음")
        with self.assertRaises(SharedMessageError) as cm:
            svc.update_message(1, None, "x", base_revision=1, update_source="desktop")
        self.assertNotIsInstance(cm.exception, SharedMessageAuthError)

    def test_known_permission_denied_rpc_code_is_not_auth_error(self):
        """서버가 명시적으로 'PERMISSION_DENIED: ...'를 던진 경우(승인된 사용자가
        맞지만 편집 권한이 없는 등)는 기존처럼 SharedMessagePermissionError이지,
        SharedMessageAuthError(세션 문제)로 바뀌면 안 된다 — 원인이 다르다."""
        svc, client = _make_service()
        client.next_rpc_error = _api_error("PERMISSION_DENIED: 메시지를 수정할 권한이 없습니다.")
        with self.assertRaises(SharedMessagePermissionError):
            svc.update_message(1, None, "x", base_revision=1, update_source="desktop")


# ============================================================
# 9. ensure_logged_in_fn — 로그인 안 된 상태면 네트워크 요청 자체를 막는다
# ============================================================
# 배경: 공유 Supabase Client에 아직 로그인 세션이 반영되지 않았을 수 있는
# 좁은 시간대(앱 시작 직후)에 shared_messages 요청이 나가는 것을 막기 위한
# 최소한의 방어선(요구사항 4절 "최소 범위로 반영").

class TestEnsureLoggedIn(unittest.TestCase):
    def test_false_blocks_list_messages_without_network_call(self):
        svc, client = _make_service(ensure_logged_in_fn=lambda: False)
        with self.assertRaises(SharedMessageAuthError):
            svc.list_messages()
        self.assertIsNone(client.last_table_query, "로그인 안 된 상태에서는 테이블 조회 자체를 시도하면 안 됨")

    def test_false_blocks_get_message_without_network_call(self):
        svc, client = _make_service(ensure_logged_in_fn=lambda: False)
        with self.assertRaises(SharedMessageAuthError):
            svc.get_message(1)
        self.assertIsNone(client.last_table_query)

    def test_false_blocks_update_message_without_rpc_call(self):
        svc, client = _make_service(ensure_logged_in_fn=lambda: False)
        with self.assertRaises(SharedMessageAuthError):
            svc.update_message(1, None, "x", base_revision=1, update_source="desktop")
        self.assertEqual(client.rpc_calls, [], "로그인 안 된 상태에서는 RPC 호출 자체를 시도하면 안 됨")

    def test_false_blocks_force_update_message_without_rpc_call(self):
        svc, client = _make_service(ensure_logged_in_fn=lambda: False)
        with self.assertRaises(SharedMessageAuthError):
            svc.force_update_message(1, None, "x", update_source="admin_force")
        self.assertEqual(client.rpc_calls, [])

    def test_false_blocks_list_history_without_network_call(self):
        svc, client = _make_service(ensure_logged_in_fn=lambda: False)
        with self.assertRaises(SharedMessageAuthError):
            svc.list_history()
        self.assertIsNone(client.last_table_query)

    def test_true_does_not_block_normal_operation(self):
        svc, client = _make_service(ensure_logged_in_fn=lambda: True)
        client.next_table_data = [_row(message_no=1)]
        records = svc.list_messages()
        self.assertEqual(len(records), 1)

    def test_callback_exception_treated_as_not_logged_in_no_crash(self):
        def _boom():
            raise RuntimeError("세션 조회 실패")

        svc, client = _make_service(ensure_logged_in_fn=_boom)
        with self.assertRaises(SharedMessageAuthError):
            svc.list_messages()
        self.assertIsNone(client.last_table_query)

    def test_none_means_no_gate_default_behavior_unchanged(self):
        svc, client = _make_service(ensure_logged_in_fn=None)
        client.next_table_data = [_row(message_no=1)]
        records = svc.list_messages()
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
