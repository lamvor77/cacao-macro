# Phase 4-1: AdminUserRecord/AdminAuditLogRecord.from_row() 파싱 테스트
#
# 실제 Supabase/storage에는 전혀 접근하지 않는다 — RPC가 반환할 법한 dict를
# 직접 구성해 .from_row()가 정확히 매핑하는지만 검증한다.
#
# 실행: python -m unittest tests.test_admin_models -v

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.admin_service import AdminAuditLogRecord, AdminUserRecord


class TestAdminUserRecord(unittest.TestCase):
    def test_from_row_full(self):
        row = {
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "editor@example.com",
            "display_name": "홍길동",
            "role": "editor",
            "status": "approved",
            "approved_at": "2026-01-01T00:00:00Z",
            "created_at": "2025-12-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "updated_by": "22222222-2222-2222-2222-222222222222",
        }
        rec = AdminUserRecord.from_row(row)
        self.assertEqual(rec.id, row["id"])
        self.assertEqual(rec.email, "editor@example.com")
        self.assertEqual(rec.display_name, "홍길동")
        self.assertEqual(rec.role, "editor")
        self.assertEqual(rec.status, "approved")
        self.assertEqual(rec.approved_at, "2026-01-01T00:00:00Z")
        self.assertEqual(rec.updated_by, row["updated_by"])

    def test_from_row_missing_optional_fields(self):
        """display_name/approved_at/updated_by 등은 NULL(None)일 수 있다(예: 미승인 사용자)."""
        row = {
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "pending@example.com",
            "display_name": None,
            "role": "viewer",
            "status": "pending",
            "approved_at": None,
            "created_at": "2025-12-01T00:00:00Z",
            "updated_at": "2025-12-01T00:00:00Z",
            "updated_by": None,
        }
        rec = AdminUserRecord.from_row(row)
        self.assertIsNone(rec.display_name)
        self.assertIsNone(rec.approved_at)
        self.assertIsNone(rec.updated_by)


class TestAdminAuditLogRecord(unittest.TestCase):
    def test_from_row_full(self):
        row = {
            "id": "33333333-3333-3333-3333-333333333333",
            "actor_user_id": "11111111-1111-1111-1111-111111111111",
            "actor_email": "admin@example.com",
            "target_user_id": "22222222-2222-2222-2222-222222222222",
            "target_email": "editor@example.com",
            "action": "user_role_changed",
            "old_role": "viewer",
            "new_role": "editor",
            "old_status": "approved",
            "new_status": "approved",
            "reason": "승격 요청",
            "metadata": {"source": "test"},
            "created_at": "2026-01-01T00:00:00Z",
        }
        rec = AdminAuditLogRecord.from_row(row)
        self.assertEqual(rec.action, "user_role_changed")
        self.assertEqual(rec.old_role, "viewer")
        self.assertEqual(rec.new_role, "editor")
        self.assertEqual(rec.reason, "승격 요청")
        self.assertEqual(rec.metadata, {"source": "test"})

    def test_from_row_target_deleted_leaves_target_fields_none(self):
        """대상 사용자가 삭제된 경우(target_user_id는 남되 target_email은 NULL) — LEFT JOIN 결과."""
        row = {
            "id": "33333333-3333-3333-3333-333333333333",
            "actor_user_id": "11111111-1111-1111-1111-111111111111",
            "actor_email": "admin@example.com",
            "target_user_id": None,
            "target_email": None,
            "action": "user_blocked",
            "old_role": "editor",
            "new_role": "editor",
            "old_status": "approved",
            "new_status": "blocked",
            "reason": None,
            "metadata": None,
            "created_at": "2026-01-01T00:00:00Z",
        }
        rec = AdminAuditLogRecord.from_row(row)
        self.assertIsNone(rec.target_user_id)
        self.assertIsNone(rec.target_email)
        self.assertIsNone(rec.reason)
        self.assertEqual(rec.metadata, {}, "metadata가 NULL이면 빈 dict로 폴백해야 함")


if __name__ == "__main__":
    unittest.main()
