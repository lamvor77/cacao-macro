# AuthService 보조 테스트 — 세션 로드/만료확인/DPAPI 암호화 저장/로그아웃.
# Google OAuth 로그인 흐름(login_with_google/complete_oauth_callback 등, Phase 2D)은
# tests/test_auth_service_oauth.py에서 별도로 다룬다.
#
# 실행: python -m unittest tests.test_auth_service -v

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.auth_service import AuthService, AuthSession
from config.cloud_settings import get_cloud_config


class TestAuthService(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="cacao_auth_test_")
        self._service = AuthService(config=get_cloud_config())
        # _session_dir()는 실제 프로젝트 storage/cloud_sync/를 가리키므로,
        # 테스트 전용 경로로 바꿔치기한다 (실제 프로젝트 파일을 건드리지 않기 위함).
        self._service._session_path = os.path.join(self._tmp_dir, "session.dat")

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_no_session_file_means_not_logged_in(self):
        self.assertIsNone(self._service.get_session())
        self.assertFalse(self._service.is_logged_in())

    def test_dpapi_round_trip(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        session = AuthSession(
            user_id="uuid-1234",
            email="test@example.com",
            access_token="fake-access-token",
            refresh_token="fake-refresh-token",
            expires_at=future,
        )
        self._service._save_session(session)

        # 저장된 파일이 평문이 아닌지 확인 (토큰 문자열이 그대로 보이면 안 됨)
        with open(self._service._session_path, "rb") as f:
            raw_bytes = f.read()
        self.assertNotIn(b"fake-access-token", raw_bytes)
        self.assertNotIn(b"fake-refresh-token", raw_bytes)

        loaded = self._service._load_session()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.user_id, "uuid-1234")
        self.assertEqual(loaded.access_token, "fake-access-token")
        self.assertFalse(loaded.is_expired())
        self.assertTrue(self._service.is_logged_in())

    def test_expired_session_without_refresh_token_fails_closed(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        session = AuthSession(
            user_id="uuid-5678",
            email="expired@example.com",
            access_token="expired-token",
            refresh_token="",
            expires_at=past,
        )
        self._service._save_session(session)

        # 만료 + refresh_token 없음 → get_session()은 None을 반환해야 함(로그인 필요)
        self.assertIsNone(self._service.get_session())
        self.assertFalse(self._service.is_logged_in())

    def test_logout_removes_session_file(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        session = AuthSession("u", "e@x.com", "a", "r", future)
        self._service._save_session(session)
        self.assertTrue(os.path.exists(self._service._session_path))

        self._service.logout()

        self.assertFalse(os.path.exists(self._service._session_path))
        self.assertFalse(self._service.is_logged_in())


if __name__ == "__main__":
    unittest.main()
