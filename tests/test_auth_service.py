# AuthService 보조 테스트 — 세션 로드/만료확인/DPAPI 암호화 저장/로그아웃.
# Google OAuth 로그인 흐름(login_with_google/complete_oauth_callback 등, Phase 2D)은
# tests/test_auth_service_oauth.py에서 별도로 다룬다.
#
# 실행: python -m unittest tests.test_auth_service -v

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.auth_service import AuthService, AuthSession, _SESSION_SCHEMA_VERSION
from config.cloud_settings import get_cloud_config

try:
    import win32crypt
except ImportError:
    win32crypt = None


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


@unittest.skipUnless(win32crypt is not None, "win32crypt(pywin32)를 사용할 수 없는 환경")
class TestSessionFileVersioning(unittest.TestCase):
    """세션 파일 포맷 버전 검사 — 이전 버전(또는 알 수 없는 버전)의 세션
    파일을 새 버전 코드가 그대로 신뢰해 파싱하는 것을 막는다. EXE 버전을
    올릴 때 이전 실행에서 남은 session.dat를 그대로 재사용하는 경우를
    원천 차단하기 위함."""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="cacao_auth_version_test_")
        self._service = AuthService(config=get_cloud_config())
        self._service._session_path = os.path.join(self._tmp_dir, "session.dat")

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _write_raw_session_file(self, extra_payload: dict) -> None:
        """_save_session()을 거치지 않고, 임의의 payload로 세션 파일을 직접
        만든다 — 과거 버전이 남긴 파일이나 알 수 없는 버전의 파일을 흉내내기
        위함."""
        raw = json.dumps(extra_payload).encode("utf-8")
        encrypted = win32crypt.CryptProtectData(raw, "cacao_macro Supabase session", None, None, None, 0)
        with open(self._service._session_path, "wb") as f:
            f.write(encrypted)

    def test_saved_session_file_embeds_current_schema_version(self):
        """_save_session()이 저장하는 파일에는 현재 스키마 버전이 들어있다."""
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        session = AuthSession("u", "e@x.com", "a", "r", future)
        self._service._save_session(session)

        with open(self._service._session_path, "rb") as f:
            encrypted = f.read()
        _, decrypted = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
        raw = json.loads(decrypted.decode("utf-8"))
        self.assertEqual(raw.get("session_version"), _SESSION_SCHEMA_VERSION)

    def test_session_file_without_version_field_is_discarded(self):
        """session_version 필드 자체가 없는 파일(버전 관리 도입 이전, 예:
        버전 관리 도입 이전에 남은 세션 파일)은 호환되지 않는 것으로 보고 폐기된다."""
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        legacy_payload = {
            "user_id": "u", "email": "e@x.com",
            "access_token": "old-access", "refresh_token": "old-refresh",
            "expires_at": future,
            # session_version 키가 아예 없음 — 구버전 파일 흉내
        }
        self._write_raw_session_file(legacy_payload)
        self.assertTrue(os.path.exists(self._service._session_path))

        loaded = self._service._load_session()

        self.assertIsNone(loaded)
        self.assertFalse(self._service.is_logged_in())
        # 폐기된 세션 파일은 삭제되어, 다음 실행에서도 같은 파일을 다시
        # 읽으려 하지 않는다(재로그인 강제).
        self.assertFalse(os.path.exists(self._service._session_path))

    def test_session_file_with_mismatched_version_is_discarded(self):
        """알 수 없는(미래 또는 과거) 버전 번호가 찍힌 세션 파일도 폐기된다."""
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        mismatched_payload = {
            "user_id": "u", "email": "e@x.com",
            "access_token": "old-access", "refresh_token": "old-refresh",
            "expires_at": future,
            "session_version": _SESSION_SCHEMA_VERSION + 999,
        }
        self._write_raw_session_file(mismatched_payload)

        loaded = self._service._load_session()

        self.assertIsNone(loaded)
        self.assertFalse(self._service.is_logged_in())
        self.assertFalse(os.path.exists(self._service._session_path))


if __name__ == "__main__":
    unittest.main()
