# 운영 관리자 UI 테스트 전용 fake — 실제 Supabase에는 절대 접속하지 않는다.
# 파일명이 test*.py 패턴이 아니므로 unittest discover 대상에 포함되지 않는다
# (tests/test_admin_ui_permissions.py, tests/test_operations_admin_panel.py가 공유한다).

import time
from typing import Optional

from services.admin_service import AdminAuditLogRecord, AdminUserRecord


class FakeAdminService:
    """AdminService의 공개 인터페이스(list_users/approve_user/block_user/
    unblock_user/update_user_role/list_audit_logs)만 흉내낸다."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.users_response: list[AdminUserRecord] = []
        self.audit_response: list[AdminAuditLogRecord] = []
        self.next_exception: Optional[Exception] = None
        self.delay_seconds: float = 0.0

    def _maybe_delay_and_raise(self) -> None:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.next_exception is not None:
            exc = self.next_exception
            self.next_exception = None
            raise exc

    def list_users(self, status=None, role=None, search=None, limit=100, offset=0):
        self.calls.append(("list_users", {
            "status": status, "role": role, "search": search, "limit": limit, "offset": offset,
        }))
        self._maybe_delay_and_raise()
        return list(self.users_response)

    def approve_user(self, target_user_id, role="viewer", reason=None):
        self.calls.append(("approve_user", {
            "target_user_id": target_user_id, "role": role, "reason": reason,
        }))
        self._maybe_delay_and_raise()
        return self.users_response[0] if self.users_response else None

    def block_user(self, target_user_id, reason=None):
        self.calls.append(("block_user", {"target_user_id": target_user_id, "reason": reason}))
        self._maybe_delay_and_raise()
        return self.users_response[0] if self.users_response else None

    def unblock_user(self, target_user_id, restore_status="approved", reason=None):
        self.calls.append(("unblock_user", {
            "target_user_id": target_user_id, "restore_status": restore_status, "reason": reason,
        }))
        self._maybe_delay_and_raise()
        return self.users_response[0] if self.users_response else None

    def update_user_role(self, target_user_id, new_role, reason=None):
        self.calls.append(("update_user_role", {
            "target_user_id": target_user_id, "new_role": new_role, "reason": reason,
        }))
        self._maybe_delay_and_raise()
        return self.users_response[0] if self.users_response else None

    def list_audit_logs(self, target_user_id=None, action=None, limit=100, offset=0):
        self.calls.append(("list_audit_logs", {
            "target_user_id": target_user_id, "action": action, "limit": limit, "offset": offset,
        }))
        self._maybe_delay_and_raise()
        return list(self.audit_response)


def make_user(
    id="11111111-1111-1111-1111-111111111111",
    email="user@example.com",
    display_name="테스트유저",
    role="viewer",
    status="approved",
    approved_at="2026-01-01T00:00:00Z",
    created_at="2025-12-01T00:00:00Z",
    updated_at="2026-01-01T00:00:00Z",
    updated_by=None,
) -> AdminUserRecord:
    return AdminUserRecord(
        id=id, email=email, display_name=display_name, role=role, status=status,
        approved_at=approved_at, created_at=created_at, updated_at=updated_at, updated_by=updated_by,
    )


def make_profile(status="approved", role="admin", user_id="admin-uuid", email="admin@example.com"):
    from services.auth_service import AppUserProfile
    return AppUserProfile(id=user_id, email=email, status=status, role=role)
