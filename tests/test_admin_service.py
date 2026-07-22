# Phase 4-1: AdminService 단위 테스트
#
# 실제 Supabase에는 절대 연결하지 않는다 — client.rpc(fn, params).execute()를
# 흉내내는 fake만 사용한다. 실제 storage도 건드리지 않는다(AdminService는 애초에
# 파일 I/O를 하지 않으므로 해당 사항 없음 — 회귀는 tests/ 전체 실행으로 확인).
#
# 실행: python -m unittest tests.test_admin_service -v

import logging
import os
import sys
import unittest
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from postgrest.exceptions import APIError as PostgrestAPIError

from services.admin_service import (
    AdminConflictError,
    AdminPermissionError,
    AdminService,
    AdminServiceError,
    AdminValidationError,
    _translate_rpc_error,
)
from services.supabase_client import ClientResult


# ============================================================
# fake — 실제 Client.rpc(fn, params).execute() 모양만 흉내낸다
# ============================================================

class FakeRPCQuery:
    def __init__(self, data=None, error: Exception = None):
        self._data = data
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(data=self._data)


class FakeAdminClient:
    def __init__(self):
        self.rpc_calls: list[tuple[str, dict]] = []
        self.next_data = []
        self.next_error: Exception | None = None

    def rpc(self, fn_name: str, params: dict):
        self.rpc_calls.append((fn_name, dict(params)))
        return FakeRPCQuery(self.next_data, self.next_error)


class FakeAdminClientManager:
    def __init__(self, client: FakeAdminClient):
        self._client = client

    def get_client(self) -> ClientResult:
        return ClientResult(True, client=self._client)


def _make_service():
    client = FakeAdminClient()
    mgr = FakeAdminClientManager(client)
    return AdminService(client_manager=mgr), client, mgr


def _api_error(message: str) -> PostgrestAPIError:
    return PostgrestAPIError({"message": message, "code": "P0001"})


_UUID_A = "11111111-1111-1111-1111-111111111111"
_UUID_B = "22222222-2222-2222-2222-222222222222"


# ============================================================
# 1~2: list_users 파싱
# ============================================================

class TestListUsers(unittest.TestCase):
    def test_1_list_users_parses_rows(self):
        svc, client, _ = _make_service()
        client.next_data = [
            {"id": _UUID_A, "email": "a@example.com", "display_name": None, "role": "editor",
             "status": "approved", "approved_at": "t", "created_at": "t", "updated_at": "t", "updated_by": None},
        ]
        result = svc.list_users()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].email, "a@example.com")
        self.assertEqual(result[0].role, "editor")

    def test_2_list_users_empty_result(self):
        svc, client, _ = _make_service()
        client.next_data = []
        result = svc.list_users()
        self.assertEqual(result, [])


# ============================================================
# 3~8: 입력 검증(RPC 호출 전 차단)
# ============================================================

class TestValidation(unittest.TestCase):
    def test_3_invalid_role_filter_rejected(self):
        svc, client, _ = _make_service()
        with self.assertRaises(AdminValidationError):
            svc.list_users(role="not_a_real_role")
        self.assertEqual(client.rpc_calls, [], "검증 실패 시 RPC를 호출하면 안 됨")

    def test_4_invalid_status_filter_rejected(self):
        svc, client, _ = _make_service()
        with self.assertRaises(AdminValidationError):
            svc.list_users(status="active")  # 실제로 존재하지 않는 값(정답은 approved)
        self.assertEqual(client.rpc_calls, [])

    def test_5_invalid_uuid_rejected(self):
        svc, client, _ = _make_service()
        with self.assertRaises(AdminValidationError):
            svc.approve_user("not-a-uuid", role="viewer")
        self.assertEqual(client.rpc_calls, [])

    def test_6_limit_zero_rejected(self):
        svc, client, _ = _make_service()
        with self.assertRaises(AdminValidationError):
            svc.list_users(limit=0)
        self.assertEqual(client.rpc_calls, [])

    def test_7_limit_201_rejected(self):
        svc, client, _ = _make_service()
        with self.assertRaises(AdminValidationError):
            svc.list_users(limit=201)
        self.assertEqual(client.rpc_calls, [])

    def test_8_negative_offset_rejected(self):
        svc, client, _ = _make_service()
        with self.assertRaises(AdminValidationError):
            svc.list_users(offset=-1)
        self.assertEqual(client.rpc_calls, [])


# ============================================================
# 9~12: 변경 RPC payload 정확성
# ============================================================

class TestPayloads(unittest.TestCase):
    def _row(self, **overrides):
        row = {"id": _UUID_A, "email": "a@example.com", "display_name": None, "role": "editor",
               "status": "approved", "approved_at": "t", "created_at": "t", "updated_at": "t", "updated_by": _UUID_B}
        row.update(overrides)
        return row

    def test_9_approve_user_payload(self):
        svc, client, _ = _make_service()
        client.next_data = [self._row()]
        svc.approve_user(_UUID_A, role="editor", reason="  승격  ")
        fn_name, params = client.rpc_calls[-1]
        self.assertEqual(fn_name, "admin_approve_user")
        self.assertEqual(params, {"p_target_user_id": _UUID_A, "p_role": "editor", "p_reason": "승격"})

    def test_10_block_user_payload(self):
        svc, client, _ = _make_service()
        client.next_data = [self._row(status="blocked")]
        svc.block_user(_UUID_A, reason="정책 위반")
        fn_name, params = client.rpc_calls[-1]
        self.assertEqual(fn_name, "admin_block_user")
        self.assertEqual(params, {"p_target_user_id": _UUID_A, "p_reason": "정책 위반"})

    def test_11_unblock_user_payload(self):
        svc, client, _ = _make_service()
        client.next_data = [self._row(status="pending")]
        svc.unblock_user(_UUID_A, restore_status="pending", reason=None)
        fn_name, params = client.rpc_calls[-1]
        self.assertEqual(fn_name, "admin_unblock_user")
        self.assertEqual(params, {"p_target_user_id": _UUID_A, "p_restore_status": "pending", "p_reason": None})

    def test_12_update_user_role_payload(self):
        svc, client, _ = _make_service()
        client.next_data = [self._row(role="admin")]
        svc.update_user_role(_UUID_A, new_role="admin")
        fn_name, params = client.rpc_calls[-1]
        self.assertEqual(fn_name, "admin_update_user_role")
        self.assertEqual(params, {"p_target_user_id": _UUID_A, "p_new_role": "admin", "p_reason": None})


# ============================================================
# 13: 감사로그 파싱
# ============================================================

class TestAuditLogParsing(unittest.TestCase):
    def test_13_list_audit_logs_parses_rows(self):
        svc, client, _ = _make_service()
        client.next_data = [{
            "id": "33333333-3333-3333-3333-333333333333", "actor_user_id": _UUID_A, "actor_email": "admin@x.com",
            "target_user_id": _UUID_B, "target_email": "b@x.com", "action": "user_blocked",
            "old_role": "editor", "new_role": "editor", "old_status": "approved", "new_status": "blocked",
            "reason": "정책 위반", "metadata": {}, "created_at": "t",
        }]
        result = svc.list_audit_logs()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].action, "user_blocked")
        self.assertEqual(result[0].actor_user_id, _UUID_A)


# ============================================================
# 14~17: RPC 오류 → 예외 매핑
# ============================================================

class TestErrorMapping(unittest.TestCase):
    def test_14_admin_required_maps_to_permission_error(self):
        exc = _translate_rpc_error(_api_error("ADMIN_REQUIRED: 관리자만 사용할 수 있습니다."))
        self.assertIsInstance(exc, AdminPermissionError)

    def test_15_last_admin_protected_maps_to_conflict_error(self):
        exc = _translate_rpc_error(_api_error("LAST_ADMIN_PROTECTED: 마지막 승인된 관리자는 차단/강등할 수 없습니다."))
        self.assertIsInstance(exc, AdminConflictError)

    def test_16_invalid_role_maps_to_validation_error(self):
        exc = _translate_rpc_error(_api_error("INVALID_ROLE: 알 수 없는 role 값입니다: foo"))
        self.assertIsInstance(exc, AdminValidationError)

    def test_17_unknown_rpc_error_maps_to_generic_service_error(self):
        exc = _translate_rpc_error(_api_error("relation \"public.app_users\" does not exist"))
        self.assertIsInstance(exc, AdminServiceError)
        self.assertNotIsInstance(exc, AdminPermissionError)
        self.assertNotIsInstance(exc, AdminValidationError)
        self.assertNotIsInstance(exc, AdminConflictError)

    def test_end_to_end_error_raised_through_service_call(self):
        """_translate_rpc_error()뿐 아니라 실제 서비스 메서드 호출 경로에서도 매핑이 적용되는지."""
        svc, client, _ = _make_service()
        client.next_error = _api_error("SELF_BLOCK_FORBIDDEN: 자기 자신을 차단할 수 없습니다.")
        with self.assertRaises(AdminConflictError):
            svc.block_user(_UUID_A)


# ============================================================
# 18: 로그에 민감정보 미출력
# ============================================================

class TestLoggingSafety(unittest.TestCase):
    def test_18_unknown_exception_message_not_logged(self):
        """알 수 없는(비-APIError) 예외 발생 시, 예외 메시지 원문(토큰류 포함 가능)을
        로그에 그대로 남기지 않고 예외 타입 이름만 남기는지 확인한다."""
        svc, client, _ = _make_service()
        fake_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.super-secret-access-token"
        client.next_error = RuntimeError(f"connection failed, Authorization: Bearer {fake_token}")

        with self.assertLogs("services.admin_service", level="ERROR") as cm:
            with self.assertRaises(AdminServiceError):
                svc.block_user(_UUID_A)

        joined_logs = "\n".join(cm.output)
        self.assertNotIn(fake_token, joined_logs, "토큰 유사 문자열이 로그에 노출되면 안 됨")
        self.assertIn("RuntimeError", joined_logs, "예외 타입 이름은 로그에 남아야 진단 가능")


# ============================================================
# 19: 별도 Supabase client 생성 금지(공유 client_manager 사용)
# ============================================================

class TestClientSharing(unittest.TestCase):
    def test_19_uses_injected_client_manager_not_a_new_one(self):
        client = FakeAdminClient()
        mgr = FakeAdminClientManager(client)
        svc = AdminService(client_manager=mgr)

        self.assertIs(svc.client_manager, mgr)
        self.assertIs(svc.client_manager.get_client().client, client)


if __name__ == "__main__":
    unittest.main()
