# Phase 3-2: 라이선스 관리자 환경변수화 + fail-closed 동작 테스트
#
# LICENSE_ADMIN_PASSWORD/LICENSE_SECRET_KEY는 config/settings.py에서 os.getenv()로
# 읽힌 뒤 Final[str]로 고정되므로, core.license_manager 모듈 네임스페이스에 이미
# 바인딩된 이름을 unittest.mock.patch로 직접 치환해 다양한 설정 상태를 시뮬레이션한다
# (실제 .env는 절대 건드리지 않는다 — 이 테스트는 실제 파일시스템의 .env를 읽지도
# 쓰지도 않는다).
#
# 운영 관리자(app_users.role) 쪽은 AppUserProfile.is_admin만 검증한다 — 실제
# Supabase에는 연결하지 않는다.
#
# 실행: python -m unittest tests.test_license_admin_config -v

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import core.license_manager as lm
from services.auth_service import AppUserProfile


# ============================================================
# 1~4: 관리자 비밀번호 인증
# ============================================================

class TestAdminPasswordAuth(unittest.TestCase):
    def test_1_empty_configured_password_always_fails(self):
        """1. LICENSE_ADMIN_PASSWORD가 비어 있음 → 관리자 인증 항상 실패."""
        with patch.object(lm, "LICENSE_ADMIN_PASSWORD", ""):
            mgr = lm.LicenseManager()
            self.assertFalse(mgr.is_admin_password_configured())
            self.assertFalse(mgr.verify_admin_password("아무거나"))
            self.assertFalse(mgr.verify_admin_password(""))

    def test_2_placeholder_password_fails(self):
        """2. LICENSE_ADMIN_PASSWORD가 CHANGE_ME_ 값 → 관리자 인증 실패."""
        with patch.object(lm, "LICENSE_ADMIN_PASSWORD", "CHANGE_ME_ADMIN_PASSWORD"):
            mgr = lm.LicenseManager()
            self.assertFalse(mgr.is_admin_password_configured())
            self.assertFalse(mgr.verify_admin_password("CHANGE_ME_ADMIN_PASSWORD"))

    def test_3_correct_password_succeeds(self):
        """3. 올바른 비밀번호 → 인증 성공."""
        with patch.object(lm, "LICENSE_ADMIN_PASSWORD", "테스트용-강한-비밀번호-123!"):
            mgr = lm.LicenseManager()
            self.assertTrue(mgr.is_admin_password_configured())
            self.assertTrue(mgr.verify_admin_password("테스트용-강한-비밀번호-123!"))

    def test_4_wrong_password_fails(self):
        """4. 잘못된 비밀번호 → 인증 실패."""
        with patch.object(lm, "LICENSE_ADMIN_PASSWORD", "테스트용-강한-비밀번호-123!"):
            mgr = lm.LicenseManager()
            self.assertFalse(mgr.verify_admin_password("다른값"))

    def test_non_string_input_is_rejected(self):
        """입력값이 문자열이 아니거나 빈 입력이면 즉시 거부(설정 확인 전에도 안전)."""
        with patch.object(lm, "LICENSE_ADMIN_PASSWORD", "테스트용-강한-비밀번호-123!"):
            mgr = lm.LicenseManager()
            self.assertFalse(mgr.verify_admin_password(""))
            self.assertFalse(mgr.verify_admin_password(None))  # type: ignore[arg-type]


# ============================================================
# 5~7: 라이선스 서명키 보호
# ============================================================

class TestLicenseSecretProtection(unittest.TestCase):
    def test_5_empty_secret_blocks_issuance(self):
        """5. LICENSE_SECRET_KEY가 비어 있음 → 라이선스 발급 차단."""
        with patch.object(lm, "LICENSE_SECRET_KEY", ""):
            mgr = lm.LicenseManager()
            self.assertFalse(mgr.is_license_secret_configured())
            with self.assertRaises(lm.LicenseConfigurationError):
                mgr.generate_build_license("2026-01-01", "2026-02-01")

    def test_6_placeholder_secret_blocks_issuance(self):
        """6. LICENSE_SECRET_KEY가 플레이스홀더 → 라이선스 발급 차단."""
        with patch.object(lm, "LICENSE_SECRET_KEY", "CHANGE_ME_BEFORE_DISTRIBUTING_RANDOM_STRING"):
            mgr = lm.LicenseManager()
            self.assertFalse(mgr.is_license_secret_configured())
            with self.assertRaises(lm.LicenseConfigurationError):
                mgr.generate_build_license("2026-01-01", "2026-02-01")

    def test_7_valid_secret_generate_and_validate_roundtrip(self):
        """7. 유효한 비밀키 → 기존 라이선스 생성·검증 회귀 없음."""
        with patch.object(lm, "LICENSE_SECRET_KEY", "충분히-긴-테스트용-임의-문자열-abcdefg"):
            mgr = lm.LicenseManager()
            data = mgr.generate_build_license("2020-01-01", "2099-12-31")
            valid, reason = mgr.validate_license_dict(data)
            self.assertTrue(valid, reason)
            self.assertEqual(reason, "")

    def test_validate_returns_false_with_friendly_message_when_secret_missing(self):
        """검증 시점에 키가 없으면 예외를 밖으로 던지지 않고 (False, 메시지)로 안전하게 반환한다."""
        with patch.object(lm, "LICENSE_SECRET_KEY", "충분히-긴-테스트용-임의-문자열-abcdefg"):
            mgr = lm.LicenseManager()
            data = mgr.generate_build_license("2020-01-01", "2099-12-31")

        with patch.object(lm, "LICENSE_SECRET_KEY", ""):
            valid, reason = mgr.validate_license_dict(data)
            self.assertFalse(valid)
            self.assertTrue(reason)
            self.assertNotIn("충분히-긴-테스트용", reason, "오류 메시지에 실제 비밀키를 노출하면 안 됨")


# ============================================================
# 8~10: UI 노출 정책 (LICENSE_ADMIN_UI_ENABLED + 설정 완료 여부)
# ============================================================

class TestUiExposurePolicy(unittest.TestCase):
    """main_window.py는 `LICENSE_ADMIN_UI_ENABLED and license_mgr.is_fully_configured()`를
    그대로 조건으로 사용하므로, 실제 Tk 위젯 없이 이 두 값의 조합만 검증해도
    버튼 노출 여부를 정확히 커버한다(GUI 위젯 자체는 이 프로젝트에서 자동화
    테스트하지 않는 기존 관례를 따른다)."""

    def test_8_ui_disabled_means_button_hidden_regardless_of_config(self):
        """8. UI enabled=false → 버튼 숨김(설정이 유효해도)."""
        with patch.object(lm, "LICENSE_ADMIN_PASSWORD", "유효한-비밀번호"), \
             patch.object(lm, "LICENSE_SECRET_KEY", "유효한-비밀키-값"):
            mgr = lm.LicenseManager()
            ui_enabled = False
            should_show = ui_enabled and mgr.is_fully_configured()
            self.assertFalse(should_show)

    def test_9_ui_enabled_but_password_missing_means_hidden(self):
        """9. UI enabled=true지만 비밀번호 누락 → 버튼 숨김."""
        with patch.object(lm, "LICENSE_ADMIN_PASSWORD", ""), \
             patch.object(lm, "LICENSE_SECRET_KEY", "유효한-비밀키-값"):
            mgr = lm.LicenseManager()
            ui_enabled = True
            should_show = ui_enabled and mgr.is_fully_configured()
            self.assertFalse(should_show)

    def test_10_ui_enabled_and_fully_configured_means_shown(self):
        """10. UI enabled=true, 비밀번호·비밀키 모두 유효 → 버튼 표시."""
        with patch.object(lm, "LICENSE_ADMIN_PASSWORD", "유효한-비밀번호"), \
             patch.object(lm, "LICENSE_SECRET_KEY", "유효한-비밀키-값"):
            mgr = lm.LicenseManager()
            ui_enabled = True
            should_show = ui_enabled and mgr.is_fully_configured()
            self.assertTrue(should_show)


# ============================================================
# 11~15: 운영 관리자 AppUserProfile.is_admin
# ============================================================

class TestAppUserProfileIsAdmin(unittest.TestCase):
    def test_11_admin_approved_is_admin_true(self):
        """11. role=admin, status=approved → is_admin=True."""
        profile = AppUserProfile(id="u1", email="a@example.com", status="approved", role="admin")
        self.assertTrue(profile.is_admin)
        self.assertTrue(profile.can_write)

    def test_12_admin_pending_is_admin_false(self):
        """12. role=admin, status=pending → is_admin=False."""
        profile = AppUserProfile(id="u1", email="a@example.com", status="pending", role="admin")
        self.assertFalse(profile.is_admin)

    def test_13_admin_blocked_is_admin_false(self):
        """13. role=admin, status=blocked → is_admin=False."""
        profile = AppUserProfile(id="u1", email="a@example.com", status="blocked", role="admin")
        self.assertFalse(profile.is_admin)

    def test_14_editor_approved_is_admin_false_but_can_write(self):
        """14. role=editor, status=approved → is_admin=False, can_write=True."""
        profile = AppUserProfile(id="u1", email="a@example.com", status="approved", role="editor")
        self.assertFalse(profile.is_admin)
        self.assertTrue(profile.can_write)

    def test_15_viewer_approved_is_admin_false_and_cannot_write(self):
        """15. role=viewer, status=approved → is_admin=False, can_write=False."""
        profile = AppUserProfile(id="u1", email="a@example.com", status="approved", role="viewer")
        self.assertFalse(profile.is_admin)
        self.assertFalse(profile.can_write)


if __name__ == "__main__":
    unittest.main()
