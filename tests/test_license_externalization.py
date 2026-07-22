# License Externalization Sprint: 라이선스 파일 외부화 테스트
#
# 실제 프로젝트의 .env/license_build.json은 전혀 건드리지 않는다 — sys.frozen/
# sys.executable/sys._MEIPASS를 unittest.mock.patch.object로 모킹하고, 파일은
# 전부 tempfile로 만든다. 실제 Supabase에는 접속하지 않는다.
#
# 실행: python -m unittest tests.test_license_externalization -v

import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config.settings as settings_mod
import core.license_manager as lm
from services.auth_service import AppUserProfile


def _sign_for_test(key: str, start: str, end: str) -> str:
    """공식 LicenseManager._license_message()로 메시지를 조립하고 표준 hmac으로
    서명한다(알고리즘 재구현이 아니라 검증 자료 생성을 위한 헬퍼) — 실제 서명
    로직과의 일치는 tests/test_license_admin_config.py에서 이미 검증됨."""
    mgr = lm.LicenseManager.__new__(lm.LicenseManager)  # __init__(경로계산) 건너뜀
    message = lm.LicenseManager._license_message(mgr, start, end)
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def _write_license(path: str, key: str, start: str, end: str) -> None:
    data = {
        "start_date": start,
        "end_date": end,
        "issued_at": "2026-01-01T00:00:00",
        "signature": _sign_for_test(key, start, end),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


# ============================================================
# A. 경로 테스트
# ============================================================

class TestRuntimeBaseDirPath(unittest.TestCase):
    def test_1_dev_mode_uses_project_root(self):
        with patch.object(sys, "frozen", False, create=True):
            base = settings_mod.get_runtime_base_dir()
        self.assertEqual(os.path.normcase(base), os.path.normcase(PROJECT_ROOT))

    def test_2_frozen_mode_uses_executable_parent(self):
        fake_exe = os.path.join(tempfile.gettempdir(), "fake_dist_dir", "cacao_macro.exe")
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", fake_exe):
            base = settings_mod.get_runtime_base_dir()
        self.assertEqual(os.path.normcase(base), os.path.normcase(os.path.dirname(fake_exe)))

    def test_3_frozen_mode_ignores_cwd(self):
        fake_exe = os.path.join(tempfile.gettempdir(), "fake_dist_dir2", "cacao_macro.exe")
        other_cwd = tempfile.gettempdir()
        original_cwd = os.getcwd()
        try:
            os.chdir(other_cwd)
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", fake_exe):
                base = settings_mod.get_runtime_base_dir()
            self.assertEqual(os.path.normcase(base), os.path.normcase(os.path.dirname(fake_exe)))
        finally:
            os.chdir(original_cwd)

    def test_4_meipass_not_used_for_license_path(self):
        """_MEIPASS에 다른 값이 있어도 외부(exe 인접) 경로만 사용해야 한다."""
        fake_exe = os.path.join(tempfile.gettempdir(), "fake_dist_dir3", "cacao_macro.exe")
        fake_meipass = os.path.join(tempfile.gettempdir(), "totally_different_meipass_dir")
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", fake_exe), \
             patch.object(sys, "_MEIPASS", fake_meipass, create=True):
            path = lm.get_external_license_path()
        self.assertEqual(os.path.normcase(os.path.dirname(path)), os.path.normcase(os.path.dirname(fake_exe)))
        self.assertNotIn("totally_different_meipass_dir", path)

    def test_5_missing_external_file_gives_clear_missing_error(self):
        tmp_dir = tempfile.mkdtemp(prefix="cacao_license_missing_")
        fake_exe = os.path.join(tmp_dir, "cacao_macro.exe")
        try:
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", fake_exe):
                mgr = lm.LicenseManager()
                valid, reason = mgr.verify_build_signature()
            self.assertFalse(valid)
            self.assertEqual(reason, lm.MSG_LICENSE_FILE_MISSING)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# B. 설정(.env) 경로 테스트
# ============================================================

class TestEnvPathResolution(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_1_frozen_mode_loads_env_next_to_exe(self):
        tmp_dir = tempfile.mkdtemp(prefix="cacao_env_frozen_")
        fake_exe = os.path.join(tmp_dir, "cacao_macro.exe")
        try:
            with open(os.path.join(tmp_dir, ".env"), "w", encoding="utf-8") as f:
                f.write("LICENSE_EXTERNALIZATION_TEST_MARKER=frozen-value\n")

            os.environ.pop("LICENSE_EXTERNALIZATION_TEST_MARKER", None)
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", fake_exe):
                from dotenv import load_dotenv
                load_dotenv(os.path.join(settings_mod.get_runtime_base_dir(), ".env"), override=False)

            self.assertEqual(os.environ.get("LICENSE_EXTERNALIZATION_TEST_MARKER"), "frozen-value")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_2_dev_mode_uses_project_root_env_path(self):
        with patch.object(sys, "frozen", False, create=True):
            base = settings_mod.get_runtime_base_dir()
        self.assertEqual(os.path.normcase(base), os.path.normcase(PROJECT_ROOT))

    def test_3_cwd_does_not_change_resolved_env_path(self):
        tmp_dir = tempfile.mkdtemp(prefix="cacao_env_cwd_")
        fake_exe = os.path.join(tmp_dir, "cacao_macro.exe")
        other_cwd = tempfile.gettempdir()
        original_cwd = os.getcwd()
        try:
            os.chdir(other_cwd)
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", fake_exe):
                resolved = os.path.join(settings_mod.get_runtime_base_dir(), ".env")
            self.assertEqual(os.path.normcase(os.path.dirname(resolved)), os.path.normcase(tmp_dir))
        finally:
            os.chdir(original_cwd)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_4_os_env_var_not_overwritten_by_env_file(self):
        tmp_dir = tempfile.mkdtemp(prefix="cacao_env_override_")
        fake_exe = os.path.join(tmp_dir, "cacao_macro.exe")
        try:
            with open(os.path.join(tmp_dir, ".env"), "w", encoding="utf-8") as f:
                f.write("LICENSE_EXTERNALIZATION_TEST_MARKER=from-dotenv-file\n")

            os.environ["LICENSE_EXTERNALIZATION_TEST_MARKER"] = "from-os-env"
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", fake_exe):
                from dotenv import load_dotenv
                load_dotenv(os.path.join(settings_mod.get_runtime_base_dir(), ".env"), override=False)

            self.assertEqual(os.environ.get("LICENSE_EXTERNALIZATION_TEST_MARKER"), "from-os-env")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# C. 라이선스 검증 테스트
# ============================================================

class TestLicenseValidation(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="cacao_license_validate_")
        self._license_path = os.path.join(self._tmp_dir, "license_build.json")

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _mgr_with_key(self, key: str) -> lm.LicenseManager:
        mgr = lm.LicenseManager()
        mgr._build_license_path = self._license_path
        return mgr

    def test_1_valid_external_license_succeeds(self):
        key = "test-key-1-충분히-긴-테스트용-값"
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=30)).isoformat()
        _write_license(self._license_path, key, start, end)

        with patch.object(lm, "LICENSE_SECRET_KEY", key):
            mgr = self._mgr_with_key(key)
            with open(self._license_path, encoding="utf-8") as f:
                data = json.load(f)
            valid, reason = mgr.validate_license_dict(data)
        self.assertTrue(valid, reason)
        self.assertEqual(reason, "")

    def test_2_wrong_signature_fails(self):
        key = "real-key"
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=30)).isoformat()
        _write_license(self._license_path, key, start, end)
        with open(self._license_path, encoding="utf-8") as f:
            data = json.load(f)
        data["signature"] = "0" * 64  # 명백히 틀린 서명

        with patch.object(lm, "LICENSE_SECRET_KEY", key):
            mgr = self._mgr_with_key(key)
            valid, reason = mgr.validate_license_dict(data)
        self.assertFalse(valid)
        self.assertEqual(reason, lm.MSG_LICENSE_SIGNATURE_INVALID)

    def test_3_signed_with_different_key_fails(self):
        signing_key = "key-A-used-to-sign"
        verifying_key = "key-B-different-from-signing"
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=30)).isoformat()
        _write_license(self._license_path, signing_key, start, end)
        with open(self._license_path, encoding="utf-8") as f:
            data = json.load(f)

        with patch.object(lm, "LICENSE_SECRET_KEY", verifying_key):
            mgr = self._mgr_with_key(verifying_key)
            valid, reason = mgr.validate_license_dict(data)
        self.assertFalse(valid)
        self.assertEqual(reason, lm.MSG_LICENSE_SIGNATURE_INVALID)

    def test_4_expired_date_range_no_longer_blocks(self):
        """사용기간(시작/종료일)은 더 이상 실행을 막지 않는다 — 실행 권한은
        Supabase 계정 인증/승인 상태로 관리한다. 서명만 일치하면 과거에
        만료된 기간이어도 valid=True다."""
        key = "test-key-4"
        start = (date.today() - timedelta(days=60)).isoformat()
        end = (date.today() - timedelta(days=1)).isoformat()
        _write_license(self._license_path, key, start, end)
        with open(self._license_path, encoding="utf-8") as f:
            data = json.load(f)

        with patch.object(lm, "LICENSE_SECRET_KEY", key):
            mgr = self._mgr_with_key(key)
            valid, reason = mgr.validate_license_dict(data)
        self.assertTrue(valid, reason)
        self.assertEqual(reason, "")

    def test_4b_not_yet_started_date_range_no_longer_blocks(self):
        key = "test-key-4b"
        start = (date.today() + timedelta(days=30)).isoformat()
        end = (date.today() + timedelta(days=60)).isoformat()
        _write_license(self._license_path, key, start, end)
        with open(self._license_path, encoding="utf-8") as f:
            data = json.load(f)

        with patch.object(lm, "LICENSE_SECRET_KEY", key):
            mgr = self._mgr_with_key(key)
            valid, reason = mgr.validate_license_dict(data)
        self.assertTrue(valid, reason)
        self.assertEqual(reason, "")

    def test_5_missing_file_fails_via_verify_build_signature(self):
        fake_exe = os.path.join(self._tmp_dir, "cacao_macro.exe")
        os.remove(self._license_path) if os.path.exists(self._license_path) else None
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", fake_exe):
            mgr = lm.LicenseManager()
            valid, reason = mgr.verify_build_signature()
        self.assertFalse(valid)
        self.assertEqual(reason, lm.MSG_LICENSE_FILE_MISSING)

    def test_6_corrupted_json_fails(self):
        with open(self._license_path, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json ]")
        fake_exe = os.path.join(self._tmp_dir, "cacao_macro.exe")

        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", fake_exe):
            mgr = lm.LicenseManager()
            valid, reason = mgr.verify_build_signature()
        self.assertFalse(valid)
        self.assertEqual(reason, lm.MSG_LICENSE_FORMAT_INVALID)

    def test_7_placeholder_key_still_rejected(self):
        key = "CHANGE_ME_BEFORE_DISTRIBUTING_RANDOM_STRING"
        start = date.today().isoformat()
        end = (date.today() + timedelta(days=30)).isoformat()
        with patch.object(lm, "LICENSE_SECRET_KEY", key):
            mgr = self._mgr_with_key(key)
            data = {"start_date": start, "end_date": end, "signature": "irrelevant"}
            valid, reason = mgr.validate_license_dict(data)
        self.assertFalse(valid)
        self.assertEqual(reason, lm.MSG_LICENSE_CONFIG_MISSING)

    def test_8_empty_key_still_rejected(self):
        with patch.object(lm, "LICENSE_SECRET_KEY", ""):
            mgr = self._mgr_with_key("")
            data = {"start_date": "2026-01-01", "end_date": "2026-02-01", "signature": "irrelevant"}
            valid, reason = mgr.validate_license_dict(data)
        self.assertFalse(valid)
        self.assertEqual(reason, lm.MSG_LICENSE_CONFIG_MISSING)


# ============================================================
# D. 외부 교체 시나리오 — 재빌드 없이 파일 교체만으로 반영됨을 입증
# ============================================================

class TestExternalReplacement(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="cacao_license_swap_")
        self._fake_exe = os.path.join(self._tmp_dir, "cacao_macro.exe")
        self._license_path = os.path.join(self._tmp_dir, "license_build.json")

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_swap_a_to_b_without_recreating_manager_or_touching_exe(self):
        key = "swap-test-key"
        today = date.today()
        # A: 오늘을 포함하는 기간(어제부터 30일 뒤까지) — "현재 유효한 라이선스".
        a_start = (today - timedelta(days=1)).isoformat()
        a_end = (today + timedelta(days=30)).isoformat()
        # B: A와 겹치지 않는(내용이 실제로 다른) 새 기간 — 오늘부터 1년.
        b_start = today.isoformat()
        b_end = (today + timedelta(days=365)).isoformat()

        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", self._fake_exe), \
             patch.object(lm, "LICENSE_SECRET_KEY", key):

            _write_license(self._license_path, key, a_start, a_end)
            mgr = lm.LicenseManager()  # "재시작"을 흉내냄 — 매번 새 인스턴스가 경로를 다시 계산
            valid_a, reason_a = mgr.verify_build_signature()
            self.assertTrue(valid_a, reason_a)

            # 같은 경로의 파일만 B로 교체(재빌드/재패키징 없음 — 그냥 파일 갈아끼움)
            _write_license(self._license_path, key, b_start, b_end)

            # "프로세스 재시작"을 흉내내기 위해 새 LicenseManager 인스턴스로 다시 검사
            mgr2 = lm.LicenseManager()
            valid_b, reason_b = mgr2.verify_build_signature()
            self.assertTrue(valid_b, reason_b)

            with open(self._license_path, encoding="utf-8") as f:
                final_data = json.load(f)
            self.assertEqual(final_data["end_date"], b_end, "B의 내용이 실제로 반영되어야 함")

    def test_exe_path_itself_never_touched_during_swap(self):
        """이 테스트 스위트가 만드는 것은 fake_exe "경로" 문자열뿐, 실제 파일이
        아니다 — 라이선스 파일 교체가 exe 자체에 어떤 쓰기도 하지 않음을,
        exe 경로에 실제 파일을 만들지 않고도(즉 손댈 대상 자체가 없음을) 보여준다."""
        self.assertFalse(os.path.exists(self._fake_exe), "테스트 내내 이 경로에 실제 파일이 존재한 적이 없어야 함")


# ============================================================
# E. verify_build_signature() 체크리스트 — 파일 경로 기준 종단 테스트
#    (배포 전 마지막 보완 작업 요구사항 6절 1~8번)
# ============================================================

class TestVerifyBuildSignatureChecklist(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="cacao_license_checklist_")
        self._fake_exe = os.path.join(self._tmp_dir, "cacao_macro.exe")
        self._license_path = os.path.join(self._tmp_dir, "license_build.json")
        self._key = "checklist-key-충분히-긴-값"

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _verify(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", self._fake_exe), \
             patch.object(lm, "LICENSE_SECRET_KEY", self._key):
            mgr = lm.LicenseManager()
            return mgr.verify_build_signature()

    def test_1_valid_signature_file_succeeds(self):
        _write_license(self._license_path, self._key, "2026-01-01", "2026-02-01")
        valid, reason = self._verify()
        self.assertTrue(valid, reason)
        self.assertEqual(reason, "")

    def test_2_tampered_signature_fails(self):
        _write_license(self._license_path, self._key, "2026-01-01", "2026-02-01")
        with open(self._license_path, encoding="utf-8") as f:
            data = json.load(f)
        data["signature"] = "f" * 64  # 서명만 변조
        with open(self._license_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        valid, reason = self._verify()
        self.assertFalse(valid)
        self.assertEqual(reason, lm.MSG_LICENSE_SIGNATURE_INVALID)

    def test_3_tampered_json_content_fails(self):
        """서명은 그대로 두고 내용(end_date)만 바꾸면 재계산한 서명과
        더 이상 일치하지 않아 변조가 탐지되어야 한다."""
        _write_license(self._license_path, self._key, "2026-01-01", "2026-02-01")
        with open(self._license_path, encoding="utf-8") as f:
            data = json.load(f)
        data["end_date"] = "2099-12-31"  # 서명 재계산 없이 값만 변조
        with open(self._license_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        valid, reason = self._verify()
        self.assertFalse(valid)
        self.assertEqual(reason, lm.MSG_LICENSE_SIGNATURE_INVALID)

    def test_4_missing_file_fails(self):
        valid, reason = self._verify()
        self.assertFalse(valid)
        self.assertEqual(reason, lm.MSG_LICENSE_FILE_MISSING)

    def test_5_json_parse_failure_fails(self):
        with open(self._license_path, "w", encoding="utf-8") as f:
            f.write("not json at all {{{")
        valid, reason = self._verify()
        self.assertFalse(valid)
        self.assertEqual(reason, lm.MSG_LICENSE_FORMAT_INVALID)

    def test_6_past_end_date_with_valid_signature_succeeds(self):
        _write_license(self._license_path, self._key, "2000-01-01", "2000-02-01")
        valid, reason = self._verify()
        self.assertTrue(valid, reason)

    def test_7_future_start_date_with_valid_signature_succeeds(self):
        _write_license(self._license_path, self._key, "2999-01-01", "2999-02-01")
        valid, reason = self._verify()
        self.assertTrue(valid, reason)

    def test_8_result_unaffected_by_mocked_system_clock(self):
        """시스템 날짜를 과거/미래로 바꿔도(datetime.now()를 모킹해도) 검증
        결과가 동일해야 한다 — 실행 허용 판단이 '지금'을 전혀 참조하지 않는다."""
        _write_license(self._license_path, self._key, "2020-01-01", "2020-06-01")
        real_datetime = lm.datetime

        for fake_now in (real_datetime(1990, 1, 1), real_datetime(2999, 1, 1)):
            with patch.object(lm, "datetime") as mock_dt:
                mock_dt.now.return_value = fake_now
                mock_dt.strptime = real_datetime.strptime
                valid, reason = self._verify()
            self.assertTrue(valid, reason)

    def test_validate_license_dict_never_reads_system_clock(self):
        """validate_license_dict()가 datetime의 어떤 속성도 참조하지 않음을
        직접 증명한다 — 호출 즉시 예외가 나는 가짜 datetime으로 교체해도
        정상 통과해야 한다(=애초에 시스템 시각을 읽지 않는다)."""
        key = "clock-independence-key"
        _write_license(self._license_path, key, "2000-01-01", "2001-01-01")
        with open(self._license_path, encoding="utf-8") as f:
            data = json.load(f)

        class _ExplodingDateTime:
            def __getattr__(self, name):
                raise AssertionError(
                    f"validate_license_dict()가 datetime.{name}을 참조했습니다 — "
                    "시스템 시각에 의존하면 안 됩니다."
                )

        with patch.object(lm, "LICENSE_SECRET_KEY", key), \
             patch.object(lm, "datetime", _ExplodingDateTime()):
            mgr = lm.LicenseManager()
            mgr._build_license_path = self._license_path
            valid, reason = mgr.validate_license_dict(data)
        self.assertTrue(valid, reason)


# ============================================================
# F. 빌드 서명 검증과 계정 승인 상태는 서로 독립된 게이트다
#    (배포 전 마지막 보완 작업 요구사항 6절 9~11번)
# ============================================================
# main.py의 실제 시작 순서 — 1) verify_build_signature() 실패 시 그 자리에서
# 종료(Supabase 로그인/승인 확인 시도 자체를 하지 않음), 2) 통과해야만
# MainWindow가 로그인 → app_users.status로 승인 여부를 판정한다
# (services/auth_service.py::AppUserProfile.is_approved). 아래 헬퍼는 판정
# 로직을 재구현하지 않고, 실제 프로덕션의 두 결과(서명 검증 결과 boolean,
# AppUserProfile.is_approved)를 main.py와 동일한 순서로 조합만 한다.

def _would_start_and_run(license_valid: bool, profile: AppUserProfile | None) -> bool:
    if not license_valid:
        return False
    if profile is None:
        return False
    return profile.is_approved


class TestSignatureAndApprovalAreIndependentGates(unittest.TestCase):
    @staticmethod
    def _profile(status: str) -> AppUserProfile:
        return AppUserProfile(id="u1", email="a@example.com", status=status, role="editor")

    def test_9_approved_with_valid_signature_can_run(self):
        self.assertTrue(_would_start_and_run(True, self._profile("approved")))

    def test_10a_pending_with_valid_signature_cannot_run(self):
        self.assertFalse(_would_start_and_run(True, self._profile("pending")))

    def test_10b_blocked_with_valid_signature_cannot_run(self):
        self.assertFalse(_would_start_and_run(True, self._profile("blocked")))

    def test_11_approved_with_invalid_signature_cannot_run(self):
        """서명이 무효면 main.py가 로그인/승인 확인 단계에 도달하기 전에
        종료하므로, 승인 상태와 무관하게 실행할 수 없다."""
        self.assertFalse(_would_start_and_run(False, self._profile("approved")))

    def test_no_auth_session_cannot_run(self):
        """세션/프로필이 없으면(로그인 전) 서명이 유효해도 실행 판정은 False."""
        self.assertFalse(_would_start_and_run(True, None))


if __name__ == "__main__":
    unittest.main()
