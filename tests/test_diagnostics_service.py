# services/diagnostics_service.py 테스트
#
# 실제 Supabase/파일시스템 상태에 의존하지 않도록 모든 의존성을 fake로
# 대체한다 — 실제 storage/logs/license_build.json은 절대 건드리지 않는다
# (tempfile만 사용).

import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from services.auth_service import AppUserProfile, AuthSession
from services.cloud_state import CloudState, CloudStatusInfo
from services.diagnostics_service import DiagnosticsService, FORBIDDEN_KEYWORDS, mask_host


# ===== 테스트용 fake 의존성 =====

@dataclass
class _FakeClientResult:
    success: bool
    error: str = ""
    error_code: str = ""


@dataclass
class _FakeCloudConfig:
    enabled: bool = True
    url: str = "https://abcdefgh1234.supabase.co"
    shared_messages_enabled: bool = True
    legacy_messages_sync_enabled: bool = True
    shared_messages_primary: bool = True


class _FakeClientManager:
    def __init__(self, configured=True, client_ok=True, check_ok=True):
        self.config = _FakeCloudConfig(enabled=configured)
        self._client_ok = client_ok
        self._check_ok = check_ok

    def get_client(self):
        if self._client_ok:
            return _FakeClientResult(True)
        return _FakeClientResult(False, error_code="missing_config")

    def check_connection(self):
        return _FakeClientResult(self._check_ok)


class _FakeAuthService:
    def __init__(self, session=None, client_manager=None):
        self._session = session
        self.client_manager = client_manager or _FakeClientManager()

    def load_session(self):
        return self._session


class _FakeCloudCoordinator:
    def __init__(self, status=None):
        self._status = status or CloudStatusInfo(CloudState.NOT_CONFIGURED)

    def get_status(self):
        return self._status


class _FakeLicenseManager:
    def __init__(self, valid=True, reason=""):
        self._valid = valid
        self._reason = reason

    def verify_build_signature(self):
        return self._valid, self._reason


class _FakeDataManager:
    def __init__(self, storage_dir):
        self._dir = storage_dir

    def get_storage_dir(self):
        return self._dir

    def list_saved_files(self):
        return [
            os.path.join(self._dir, f) for f in os.listdir(self._dir) if f.endswith(".json")
        ]


class _FakeBackupRecord:
    def __init__(self, created_at, validated):
        self.created_at = created_at
        self.validated = validated


class _FakeBackupService:
    def __init__(self, backups=None, backup_dir="C:/fake/backup"):
        self._backups = backups or []
        self._dir = backup_dir

    def get_backup_dir(self):
        return self._dir

    def list_backups(self):
        return self._backups


def _make_service(
    session=None, client_manager=None, cloud_status=None, license_valid=True,
    license_reason="", profile=None, storage_dir=None, backup_service=None,
    check_network=False, message_source_fn=None,
):
    auth = _FakeAuthService(session=session, client_manager=client_manager)
    cloud = _FakeCloudCoordinator(status=cloud_status)
    lic = _FakeLicenseManager(valid=license_valid, reason=license_reason)
    data = _FakeDataManager(storage_dir or tempfile.gettempdir())
    return DiagnosticsService(
        auth_service=auth,
        cloud_coordinator=cloud,
        license_manager=lic,
        data_manager=data,
        current_profile_fn=lambda: profile,
        backup_service=backup_service,
        check_network=check_network,
        message_source_fn=message_source_fn,
    )


class TestTestEnvironmentFlag(unittest.TestCase):
    """Test Environment Deployment & E2E Validation Sprint 1절 — 진단 스냅샷과
    복사 텍스트에 환경 구분이 정확히 반영되는지 확인한다."""

    def test_production_by_default(self):
        with patch("services.diagnostics_service.IS_TEST_ENVIRONMENT", False):
            svc = _make_service()
            snapshot = svc.collect()
            self.assertFalse(snapshot.app.is_test_environment)
            text = svc.to_copy_text(snapshot)
            self.assertIn("환경: 운영(production)", text)
            self.assertNotIn("TEST ENVIRONMENT", text)

    def test_test_environment_flag_reflected_in_snapshot_and_copy_text(self):
        with patch("services.diagnostics_service.IS_TEST_ENVIRONMENT", True):
            svc = _make_service()
            snapshot = svc.collect()
            self.assertTrue(snapshot.app.is_test_environment)
            text = svc.to_copy_text(snapshot)
            self.assertIn("환경: TEST ENVIRONMENT", text)


class TestAppVersionInDiagnostics(unittest.TestCase):
    """배포 전 버전 표시 정리 — 진단정보의 버전이 config/version.py의
    APP_VERSION을 그대로 반영하는지(별도 하드코딩 없음) 확인한다.

    AppDiagnostics.version의 기본값은 모듈 임포트 시점에 한 번 바인딩되므로
    (dataclass 필드 기본값), 여기서는 "지금 이 프로세스에서 import된
    APP_VERSION과 정확히 같은 값을 쓰는지"를 직접 확인한다 — 값을 임의로
    다른 문자열로 바꿔 검증하는 방식(패치)은 dataclass 기본값 바인딩 시점
    특성상 효과가 없다."""

    def test_snapshot_version_matches_config_version_app_version(self):
        from config.version import APP_VERSION as CONFIG_APP_VERSION

        svc = _make_service()
        snapshot = svc.collect()
        self.assertEqual(snapshot.app.version, CONFIG_APP_VERSION)
        text = svc.to_copy_text(snapshot)
        self.assertIn(f"버전: {CONFIG_APP_VERSION}", text)


class TestMaskHost(unittest.TestCase):
    def test_masks_long_prefix(self):
        self.assertEqual(mask_host("https://abcdefgh1234.supabase.co"), "abcd********.supabase.co")

    def test_empty_url_returns_empty(self):
        self.assertEqual(mask_host(""), "")

    def test_short_prefix_still_masked(self):
        masked = mask_host("https://ab.supabase.co")
        self.assertTrue(masked.startswith("a"))
        self.assertNotIn("ab.supabase.co", masked)

    def test_invalid_url_returns_empty(self):
        self.assertEqual(mask_host("not a url"), "")


class TestDiagnosticsCollection(unittest.TestCase):
    def test_auth_section_uses_local_session_only_no_network(self):
        session = AuthSession(
            user_id="u1", email="test@example.com", access_token="tok",
            refresh_token="rtok", expires_at="2099-01-01T00:00:00+00:00",
        )
        profile = AppUserProfile(id="u1", email="test@example.com", status="approved", role="admin")
        svc = _make_service(session=session, profile=profile)
        snapshot = svc.collect()
        self.assertTrue(snapshot.auth.logged_in)
        self.assertEqual(snapshot.auth.email, "test@example.com")
        self.assertTrue(snapshot.auth.is_admin)

    def test_auth_section_no_session_shows_logged_out(self):
        svc = _make_service(session=None)
        snapshot = svc.collect()
        self.assertFalse(snapshot.auth.logged_in)
        self.assertEqual(snapshot.auth.email, "")
        self.assertIsNone(snapshot.auth.is_admin)

    def test_expired_session_shows_logged_out(self):
        session = AuthSession(
            user_id="u1", email="test@example.com", access_token="tok",
            refresh_token="rtok", expires_at="2020-01-01T00:00:00+00:00",
        )
        svc = _make_service(session=session)
        snapshot = svc.collect()
        self.assertFalse(snapshot.auth.logged_in)

    def test_network_not_checked_by_default(self):
        cm = _FakeClientManager(configured=True, client_ok=True, check_ok=True)
        cm.check_connection = MagicMock(side_effect=AssertionError("네트워크 호출이 발생하면 안 됨"))
        svc = _make_service(client_manager=cm, check_network=False)
        snapshot = svc.collect()
        self.assertEqual(snapshot.supabase.network_status, "확인 안 함")

    def test_network_checked_when_enabled(self):
        cm = _FakeClientManager(configured=True, client_ok=True, check_ok=True)
        svc = _make_service(client_manager=cm, check_network=True)
        snapshot = svc.collect()
        self.assertEqual(snapshot.supabase.network_status, "연결됨")

    def test_not_configured_supabase_skips_network_check(self):
        cm = _FakeClientManager(configured=False)
        svc = _make_service(client_manager=cm, check_network=True)
        snapshot = svc.collect()
        self.assertEqual(snapshot.supabase.network_status, "미설정")

    def test_sync_section_reads_status_without_triggering_new_sync(self):
        status = CloudStatusInfo(CloudState.CONFLICT, detail="충돌", pending_count=2, conflict_count=3)
        svc = _make_service(cloud_status=status)
        snapshot = svc.collect()
        self.assertEqual(snapshot.sync.pending_count, 2)
        self.assertEqual(snapshot.sync.conflict_count, 3)
        self.assertEqual(snapshot.sync.last_sync_state, "conflict")

    def test_license_valid_reports_file_exists_without_period_fields(self):
        """사용기간(start_date/end_date/remaining_days)은 더 이상 진단 정보에
        포함하지 않는다 — 실행 권한은 계정 인증/승인 상태로 관리한다."""
        with tempfile.TemporaryDirectory() as tmp:
            license_path = os.path.join(tmp, "license_build.json")
            end_date = (date.today() + timedelta(days=10)).isoformat()
            with open(license_path, "w", encoding="utf-8") as f:
                json.dump({"start_date": "2020-01-01", "end_date": end_date, "signature": "x"}, f)
            with patch("services.diagnostics_service.get_external_license_path", return_value=license_path):
                svc = _make_service(license_valid=True)
                snapshot = svc.collect()
            self.assertTrue(snapshot.license.file_exists)
            self.assertTrue(snapshot.license.valid)
            self.assertFalse(hasattr(snapshot.license, "remaining_days"))
            self.assertFalse(hasattr(snapshot.license, "start_date"))
            self.assertFalse(hasattr(snapshot.license, "end_date"))

    def test_license_missing_file(self):
        with patch("services.diagnostics_service.get_external_license_path", return_value="Z:/does/not/exist.json"):
            svc = _make_service(license_valid=False, license_reason="라이선스 파일이 없습니다.")
            snapshot = svc.collect()
        self.assertFalse(snapshot.license.file_exists)
        self.assertFalse(snapshot.license.valid)
        self.assertEqual(snapshot.license.reason, "라이선스 파일이 없습니다.")

    def test_storage_section_counts_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.json", "b.json"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write("{}")
            svc = _make_service(storage_dir=tmp)
            snapshot = svc.collect()
        self.assertEqual(snapshot.storage.data_file_count, 2)
        self.assertTrue(snapshot.storage.readable)

    def test_backup_section_without_backup_service(self):
        svc = _make_service(backup_service=None)
        snapshot = svc.collect()
        self.assertEqual(snapshot.backup.error, "백업 서비스 미구성")

    def test_backup_section_with_backups(self):
        backups = [_FakeBackupRecord("2026-07-18T10:00:00", True)]
        svc = _make_service(backup_service=_FakeBackupService(backups=backups))
        snapshot = svc.collect()
        self.assertEqual(snapshot.backup.backup_count, 1)
        self.assertEqual(snapshot.backup.latest_backup_at, "2026-07-18T10:00:00")
        self.assertTrue(snapshot.backup.latest_backup_valid)


class TestMessageSourceDiagnostics(unittest.TestCase):
    """Production Stabilization Sprint 11.B — 런타임 출처 표시."""

    def test_reads_feature_flags_from_config(self):
        cm = _FakeClientManager()
        cm.config.shared_messages_enabled = True
        cm.config.legacy_messages_sync_enabled = True
        cm.config.shared_messages_primary = False
        svc = _make_service(client_manager=cm)
        snapshot = svc.collect()
        self.assertTrue(snapshot.message_source.shared_messages_enabled)
        self.assertTrue(snapshot.message_source.legacy_sync_enabled)
        self.assertFalse(snapshot.message_source.shared_messages_primary)

    def test_without_message_source_fn_defaults_to_empty(self):
        svc = _make_service(message_source_fn=None)
        snapshot = svc.collect()
        self.assertEqual(snapshot.message_source.per_message_sources, {})
        self.assertEqual(snapshot.message_source.error, "")

    def test_with_message_source_fn_returns_per_message_map(self):
        svc = _make_service(message_source_fn=lambda: {1: "shared_messages_server", 2: "legacy_local"})
        snapshot = svc.collect()
        self.assertEqual(snapshot.message_source.per_message_sources, {1: "shared_messages_server", 2: "legacy_local"})

    def test_message_source_fn_exception_isolated(self):
        def bad_fn():
            raise RuntimeError("boom")
        svc = _make_service(message_source_fn=bad_fn)
        snapshot = svc.collect()  # collect() 자체는 죽지 않아야 함
        self.assertEqual(snapshot.message_source.error, "조회 실패")

    def test_fallback_cache_path_populated(self):
        svc = _make_service()
        snapshot = svc.collect()
        self.assertIn("messages.json", snapshot.message_source.fallback_cache_path)


class TestSectionIsolation(unittest.TestCase):
    """한 섹션이 예외를 던져도 나머지 섹션과 collect() 자체는 죽지 않는다."""

    def test_broken_cloud_coordinator_does_not_crash_collect(self):
        cloud = MagicMock()
        cloud.get_status.side_effect = RuntimeError("boom")
        auth = _FakeAuthService()
        lic = _FakeLicenseManager()
        data = _FakeDataManager(tempfile.gettempdir())
        svc = DiagnosticsService(
            auth_service=auth, cloud_coordinator=cloud, license_manager=lic,
            data_manager=data, current_profile_fn=lambda: None,
        )
        snapshot = svc.collect()  # 예외를 밖으로 던지지 않아야 한다
        self.assertEqual(snapshot.sync.error, "조회 실패")
        # 다른 섹션은 정상 수집되었어야 한다
        self.assertEqual(snapshot.auth.error, "")

    def test_broken_license_manager_does_not_crash_collect(self):
        lic = MagicMock()
        lic.verify_build_signature.side_effect = RuntimeError("boom")
        auth = _FakeAuthService()
        cloud = _FakeCloudCoordinator()
        data = _FakeDataManager(tempfile.gettempdir())
        svc = DiagnosticsService(
            auth_service=auth, cloud_coordinator=cloud, license_manager=lic,
            data_manager=data, current_profile_fn=lambda: None,
        )
        snapshot = svc.collect()
        self.assertEqual(snapshot.license.error, "조회 실패")
        self.assertEqual(snapshot.sync.error, "")


class TestCopyTextNoSecrets(unittest.TestCase):
    def test_copy_text_contains_no_forbidden_keywords(self):
        session = AuthSession(
            user_id="u1", email="test@example.com", access_token="SECRET_TOKEN_VALUE",
            refresh_token="SECRET_REFRESH_VALUE", expires_at="2099-01-01T00:00:00+00:00",
        )
        svc = _make_service(session=session)
        snapshot = svc.collect()
        text = svc.to_copy_text(snapshot)
        lowered = text.lower()
        for keyword in FORBIDDEN_KEYWORDS:
            self.assertNotIn(keyword.lower(), lowered, f"금지 키워드가 복사 텍스트에 포함됨: {keyword}")
        # 실제 토큰 값 자체도 포함되면 안 된다(설령 필드명이 없어도).
        self.assertNotIn("SECRET_TOKEN_VALUE", text)
        self.assertNotIn("SECRET_REFRESH_VALUE", text)

    def test_copy_text_contains_email_but_not_admin_password_field(self):
        session = AuthSession(
            user_id="u1", email="admin@example.com", access_token="tok",
            refresh_token="rtok", expires_at="2099-01-01T00:00:00+00:00",
        )
        svc = _make_service(session=session)
        snapshot = svc.collect()
        text = svc.to_copy_text(snapshot)
        self.assertIn("admin@example.com", text)
        self.assertNotIn("LICENSE_ADMIN_PASSWORD", text)
        self.assertNotIn("LICENSE_SECRET_KEY", text)


if __name__ == "__main__":
    unittest.main()
