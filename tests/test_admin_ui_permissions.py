# Phase 4-2: 운영 관리자 UI 판정/변환 로직(gui/panels/admin_ui_state.py) 단위 테스트
#
# 전부 Tkinter와 무관한 순수 함수/클래스만 검증한다 — 실제 위젯을 만들지 않는다.
# 실제 Supabase/storage에는 접근하지 않는다.
#
# 실행: python -m unittest tests.test_admin_ui_permissions -v

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from gui.panels.admin_ui_state import (
    APPROVE_DIALOG_ROLES,
    ROLE_CHANGE_DIALOG_ROLES,
    AdminActionState,
    build_approve_payload,
    build_audit_log_params,
    build_block_payload,
    build_list_users_params,
    build_role_change_payload,
    build_unblock_payload,
    can_change_role_to,
    describe_admin_error,
    get_admin_action_state,
    is_operations_admin_menu_visible,
)
from services.admin_service import (
    AdminConflictError,
    AdminPermissionError,
    AdminServiceError,
    AdminValidationError,
)

from tests._admin_service_fakes import make_profile, make_user

_ADMIN_ID = "admin-uuid"
_OTHER_ID = "22222222-2222-2222-2222-222222222222"


# ============================================================
# 1~3: 운영 관리자 메뉴 노출
# ============================================================

class TestMenuVisibility(unittest.TestCase):
    def test_1_admin_profile_shows_menu(self):
        profile = make_profile(status="approved", role="admin")
        self.assertTrue(is_operations_admin_menu_visible(profile))

    def test_2_non_admin_profiles_hide_menu(self):
        for status, role in [("approved", "viewer"), ("approved", "editor"),
                              ("pending", "admin"), ("blocked", "admin")]:
            with self.subTest(status=status, role=role):
                profile = make_profile(status=status, role=role)
                self.assertFalse(is_operations_admin_menu_visible(profile))

    def test_3_logged_out_hides_menu(self):
        self.assertFalse(is_operations_admin_menu_visible(None))


# ============================================================
# 4~8: 사용자별 작업 버튼 활성화
# ============================================================

class TestActionState(unittest.TestCase):
    def test_4_no_selection_disables_everything(self):
        state = get_admin_action_state(_ADMIN_ID, None)
        self.assertEqual(state, AdminActionState(False, False, False, False, False))

    def test_5_pending_user_can_approve(self):
        user = make_user(id=_OTHER_ID, status="pending")
        state = get_admin_action_state(_ADMIN_ID, user)
        self.assertTrue(state.can_approve)

    def test_6_blocked_user_can_unblock(self):
        user = make_user(id=_OTHER_ID, status="blocked")
        state = get_admin_action_state(_ADMIN_ID, user)
        self.assertTrue(state.can_unblock)
        self.assertFalse(state.can_approve)

    def test_7_approved_user_can_block(self):
        user = make_user(id=_OTHER_ID, status="approved")
        state = get_admin_action_state(_ADMIN_ID, user)
        self.assertTrue(state.can_block)

    def test_8_self_selection_disables_block(self):
        user = make_user(id=_ADMIN_ID, status="approved")
        state = get_admin_action_state(_ADMIN_ID, user)
        self.assertFalse(state.can_block)
        self.assertTrue(state.is_self)


# ============================================================
# 9~11: 역할 변경 사전 차단 + 다이얼로그 역할 목록
# ============================================================

class TestRoleChangeGuards(unittest.TestCase):
    def test_9_self_admin_demotion_blocked_in_ui(self):
        user = make_user(id=_ADMIN_ID, role="admin", status="approved")
        self.assertFalse(can_change_role_to(_ADMIN_ID, user, "editor"))
        self.assertFalse(can_change_role_to(_ADMIN_ID, user, "viewer"))
        # 자기 자신을 admin → admin(동일 값)으로 "바꾸는" 것은 no-op이라 어차피 막힘
        self.assertFalse(can_change_role_to(_ADMIN_ID, user, "admin"))

    def test_self_promotion_still_blocked_by_noop_rule_but_other_target_admin_allowed(self):
        other = make_user(id=_OTHER_ID, role="editor", status="approved")
        self.assertTrue(can_change_role_to(_ADMIN_ID, other, "admin"))

    def test_10_approve_dialog_never_offers_admin(self):
        self.assertNotIn("admin", APPROVE_DIALOG_ROLES)
        self.assertEqual(set(APPROVE_DIALOG_ROLES), {"viewer", "editor"})

    def test_11_role_change_dialog_offers_admin(self):
        self.assertIn("admin", ROLE_CHANGE_DIALOG_ROLES)
        self.assertEqual(set(ROLE_CHANGE_DIALOG_ROLES), {"viewer", "editor", "admin"})


# ============================================================
# 12~15, 20: 조회 payload 생성
# ============================================================

class TestQueryPayloads(unittest.TestCase):
    def test_12_search_text_is_trimmed_and_empty_becomes_none(self):
        params = build_list_users_params("  홍길동  ", "전체", "전체", 100, 0)
        self.assertEqual(params["search"], "홍길동")

        params_empty = build_list_users_params("   ", "전체", "전체", 100, 0)
        self.assertIsNone(params_empty["search"])

    def test_13_status_filter_payload(self):
        params = build_list_users_params("", "pending", "전체", 100, 0)
        self.assertEqual(params["status"], "pending")
        params_all = build_list_users_params("", "전체", "전체", 100, 0)
        self.assertIsNone(params_all["status"])

    def test_14_role_filter_payload(self):
        params = build_list_users_params("", "전체", "editor", 100, 0)
        self.assertEqual(params["role"], "editor")
        params_all = build_list_users_params("", "전체", "전체", 100, 0)
        self.assertIsNone(params_all["role"])

    def test_15_pagination_offset_payload(self):
        params = build_list_users_params("", "전체", "전체", 100, 200)
        self.assertEqual(params["limit"], 100)
        self.assertEqual(params["offset"], 200)

    def test_20_audit_log_params(self):
        params = build_audit_log_params(_OTHER_ID, "전체", 100, 0)
        self.assertEqual(params["target_user_id"], _OTHER_ID)
        self.assertIsNone(params["action"])

        params2 = build_audit_log_params(None, "user_blocked", 50, 100)
        self.assertIsNone(params2["target_user_id"])
        self.assertEqual(params2["action"], "user_blocked")
        self.assertEqual(params2["offset"], 100)


# ============================================================
# 16~19: 변경 payload 생성
# ============================================================

class TestMutationPayloads(unittest.TestCase):
    def test_16_approve_payload(self):
        payload = build_approve_payload(_OTHER_ID, "editor", "  승격 요청  ")
        self.assertEqual(payload, {"target_user_id": _OTHER_ID, "role": "editor", "reason": "승격 요청"})

    def test_17_role_change_payload(self):
        payload = build_role_change_payload(_OTHER_ID, "admin", "")
        self.assertEqual(payload, {"target_user_id": _OTHER_ID, "new_role": "admin", "reason": None})

    def test_18_block_payload(self):
        payload = build_block_payload(_OTHER_ID, "정책 위반")
        self.assertEqual(payload, {"target_user_id": _OTHER_ID, "reason": "정책 위반"})

    def test_19_unblock_payload(self):
        payload = build_unblock_payload(_OTHER_ID, "pending", None or "")
        self.assertEqual(payload, {"target_user_id": _OTHER_ID, "restore_status": "pending", "reason": None})


# ============================================================
# 21~22: 오류 메시지 한국어 매핑
# ============================================================

class TestErrorMessages(unittest.TestCase):
    def test_21_admin_permission_error_message(self):
        msg = describe_admin_error(AdminPermissionError("ADMIN_REQUIRED"))
        self.assertIn("다시 로그인", msg)

    def test_22_conflict_error_translated_to_korean(self):
        msg = describe_admin_error(AdminConflictError("마지막 승인된 관리자는 차단/강등할 수 없습니다."))
        self.assertIn("마지막 승인된 관리자", msg)

    def test_validation_error_shows_detail(self):
        msg = describe_admin_error(AdminValidationError("role 값이 올바르지 않습니다: foo"))
        self.assertIn("role 값이 올바르지 않습니다", msg)

    def test_unknown_service_error_generic_message(self):
        msg = describe_admin_error(AdminServiceError("아무 사유"))
        self.assertIn("잠시 후 다시 시도", msg)

    def test_non_admin_exception_still_returns_safe_generic_message(self):
        msg = describe_admin_error(RuntimeError("connection reset, token=eyJabc123"))
        self.assertNotIn("eyJabc123", msg)


if __name__ == "__main__":
    unittest.main()
