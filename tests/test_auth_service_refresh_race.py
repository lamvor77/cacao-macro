# 세션 갱신 경쟁 조건("Invalid Refresh Token: Already Used") 재현/회귀 테스트.
#
# 배경: 운영 EXE 1개만 실행 중인데도 "Supabase 세션 갱신 실패: Invalid Refresh
# Token: Already Used" 오류가 발생했다 — 외부 다중 실행이 아니라, 같은 프로세스
# 안에서 여러 스레드(자동 새로고침 타이머, 수동 새로고침, Realtime 재연결,
# API 호출 전후 등)가 동시에 refresh_session()을 호출해, 1회용(rotate)
# refresh_token으로 두 번째 네트워크 요청을 보낸 것이 원인으로 파악됐다.
#
# 이 파일은 그 수정(services/auth_service.py의 락 직렬화/원자적 저장/
# "Already Used" 무한 재시도 방지)이 실제로 동작하는지 다음 5가지를 검증한다.
#   (a) 두 스레드가 동시에 refresh를 요청해도 실제 네트워크 요청은 1건만 나간다.
#   (b) 갱신 성공 시 새 refresh_token이 메모리 캐시와 디스크 파일 모두에 반영된다.
#   (c) session.dat를 저장하는 코드는 services/auth_service.py 안에만 있다
#       (다른 경로에서 오래된 세션 객체로 덮어쓸 수 없음 — 정적 회귀 검사).
#   (d) 저장 도중 오류가 나도 기존 파일이 손상되지 않고, 임시 파일도 남지 않는다.
#   (e) "Already Used" 오류 이후 같은 refresh_token으로 다시 네트워크 요청을
#       보내지 않는다(세션은 로컬에서 즉시 무효화된다).
#
# 실제 Supabase에는 절대 연결하지 않는다 — tests/test_auth_service_oauth.py의
# fake(FakeSupabaseClient 등)를 그대로 재사용한다.
#
# 실행: python -m unittest tests.test_auth_service_refresh_race -v

import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.auth_service import AuthService, AuthSession
from config.cloud_settings import CloudConfig
from tests.test_auth_service_oauth import (
    FakeAuthResponse, FakeClientManager, FakeGoTrueAuth, FakeSession, FakeSupabaseClient,
    FakeUser, make_fake_auth_response,
)

AUTH_SERVICE_PATH = os.path.join(PROJECT_ROOT, "services", "auth_service.py")
_EXCLUDED_DIRS = {".git", "__pycache__", "node_modules", "dist", "build", "release", "test-runtime", ".venv"}


def _future_iso(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class SlowFakeGoTrueAuth(FakeGoTrueAuth):
    """refresh_session()이 delay초 동안 블로킹하는 fake — 두 스레드의 refresh
    요청이 실제로 겹치도록(경쟁 상황을 재현하도록) 만들기 위함."""

    def __init__(self, delay: float = 0.2):
        super().__init__()
        self._delay = delay

    def refresh_session(self, refresh_token=None):
        time.sleep(self._delay)
        return super().refresh_session(refresh_token)


class RefreshRaceTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="cacao_auth_race_")
        self._client = FakeSupabaseClient()
        self._mgr = FakeClientManager(client=self._client)
        cfg = CloudConfig(enabled=True, url="https://project.supabase.co", anon_key="anon-key",
                           device_id="pc-test")
        self._auth = AuthService(config=cfg, client_manager=self._mgr)
        self._auth._session_path = os.path.join(self._tmp_dir, "session.dat")

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _seed_session(self, refresh_token: str = "old-refresh", expired: bool = True) -> AuthSession:
        expires_at = _future_iso(-3600) if expired else _future_iso()
        session = AuthSession(
            user_id="uuid-x", email="x@example.com",
            access_token="old-access", refresh_token=refresh_token,
            expires_at=expires_at,
        )
        self._auth._save_session(session)
        return session


class TestConcurrentRefresh(RefreshRaceTestBase):
    def test_a_two_concurrent_threads_only_trigger_one_network_refresh(self):
        """(a) 두 스레드가 동시에 refresh_session()을 호출해도 실제 Supabase
        refresh 요청은 1건만 나가고, 두 스레드 모두 같은(성공한) 결과를 받는다."""
        self._seed_session(refresh_token="old-refresh", expired=True)
        self._client.auth = SlowFakeGoTrueAuth(delay=0.2)
        self._client.auth.refresh_response = make_fake_auth_response(user_id="uuid-x", email="new@x.com")

        results = []

        def _call():
            results.append(self._auth.refresh_session())

        t1 = threading.Thread(target=_call)
        t2 = threading.Thread(target=_call)
        t1.start()
        time.sleep(0.03)  # t1이 락을 먼저 잡고 네트워크 호출에 들어가도록 약간의 시차를 둔다
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.success for r in results), [r.error for r in results])
        # 실제 네트워크 refresh 요청은 정확히 1건만 나가야 한다 — 동일한
        # refresh_token으로 두 번째 요청을 보내면 실제 Supabase에서는
        # "Already Used"로 실패하기 때문에, 애초에 두 번째 요청 자체가
        # 나가지 않아야 한다.
        self.assertEqual(self._client.auth.refresh_calls, ["old-refresh"])
        self.assertEqual(results[0].session.access_token, results[1].session.access_token)


class TestPersistence(RefreshRaceTestBase):
    def test_b_new_refresh_token_persisted_to_memory_and_disk(self):
        """(b) 갱신 성공 시 새 refresh_token이 메모리 캐시와 디스크 파일 모두에
        즉시 반영된다."""
        self._seed_session(refresh_token="old-refresh", expired=True)
        self._client.auth.refresh_response = FakeAuthResponse(
            user=FakeUser(id="uuid-x", email="x@example.com"),
            session=FakeSession(
                access_token="new-access-token", refresh_token="new-refresh-token",
                expires_at=int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
                user=FakeUser(id="uuid-x", email="x@example.com"),
            ),
        )

        with self.assertLogs("services.auth_service", level="DEBUG") as captured:
            result = self._auth.refresh_session()

        self.assertTrue(result.success)
        self.assertEqual(result.session.refresh_token, "new-refresh-token")

        # 메모리 캐시
        self.assertIsNotNone(self._auth._session_cache)
        self.assertEqual(self._auth._session_cache.refresh_token, "new-refresh-token")

        # 디스크(새 AuthService 인스턴스로 다시 읽어도 동일해야 함 — 캐시가
        # 아니라 실제로 파일에 반영됐는지 확인)
        fresh_auth = AuthService(config=self._auth._config, client_manager=self._mgr)
        fresh_auth._session_path = self._auth._session_path
        loaded = fresh_auth.load_session()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.refresh_token, "new-refresh-token")
        self.assertEqual(loaded.access_token, "new-access-token")

        # 원문 토큰이 로그에 남지 않아야 한다(요구사항 6절)
        all_messages = "\n".join(captured.output)
        self.assertNotIn("new-refresh-token", all_messages)
        self.assertNotIn("new-access-token", all_messages)
        self.assertNotIn("old-refresh", all_messages)


class TestNoExternalSavePath(unittest.TestCase):
    def test_c_only_auth_service_writes_session_file(self):
        """(c) session.dat를 저장하는 코드(_save_session 호출 또는 AuthSession
        구성 후 저장)는 services/auth_service.py 안에만 있어야 한다.

        이 파일 밖 어딘가에 세션을 저장하는 별도 경로가 있으면, 그 경로가
        들고 있는 오래된(rotate되기 전) 세션 객체가 방금 갱신되어 저장된
        최신 세션 파일을 덮어쓸 위험이 생긴다(요구사항 5절) — 이를 1회성
        조사가 아니라 회귀 방지 테스트로 고정한다."""
        offenders = []
        for root, dirs, files in os.walk(PROJECT_ROOT):
            dirs[:] = [d for d in dirs if d not in _EXCLUDED_DIRS]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                if os.path.abspath(path) == AUTH_SERVICE_PATH:
                    continue
                if os.path.sep + "tests" + os.path.sep in path or path.endswith(
                    os.path.sep + "tests"
                ):
                    continue  # 테스트는 fake/mock으로 _save_session을 참조할 수 있어 제외
                with open(path, encoding="utf-8") as f:
                    content = f.read()
                if "_save_session(" in content or re.search(r"\bAuthSession\s*\(", content):
                    offenders.append(os.path.relpath(path, PROJECT_ROOT))
        self.assertEqual(offenders, [], f"session.dat를 직접 쓰거나 AuthSession을 구성하는 외부 경로 발견: {offenders}")


class TestSaveFailureSafety(RefreshRaceTestBase):
    def test_d_failed_save_does_not_corrupt_existing_session_file(self):
        """(d) 저장 도중 오류(예: fsync 실패)가 나도 기존 파일은 손상되지 않고
        그대로 남으며, 임시 파일(.tmp)도 남기지 않는다."""
        good_session = AuthSession(
            user_id="u-good", email="good@x.com",
            access_token="good-access", refresh_token="good-refresh",
            expires_at=_future_iso(),
        )
        self.assertTrue(self._auth._save_session(good_session))

        bad_session = AuthSession(
            user_id="u-bad", email="bad@x.com",
            access_token="bad-access", refresh_token="bad-refresh",
            expires_at=_future_iso(),
        )
        with patch("services.auth_service.os.fsync", side_effect=OSError("disk full")):
            saved_ok = self._auth._save_session(bad_session)

        self.assertFalse(saved_ok)

        # 기존 파일이 그대로 남아 있어야 한다(새 내용으로 일부만 덮어써지지 않음)
        loaded = self._auth._load_session()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.access_token, "good-access")
        self.assertEqual(loaded.refresh_token, "good-refresh")

        # 임시 파일이 남아있지 않아야 한다
        tmp_path = self._auth._session_path + ".tmp"
        self.assertFalse(os.path.exists(tmp_path))


class TestNoRetryAfterAlreadyUsed(RefreshRaceTestBase):
    def test_e_no_retry_with_same_refresh_token_after_already_used(self):
        """(e) "Invalid Refresh Token: Already Used" 오류를 만나면 (1) 로컬
        세션이 즉시 무효화되고, (2) 같은 refresh_token으로는 다시 네트워크
        요청을 보내지 않는다(무한 재시도 없음)."""
        self._seed_session(refresh_token="reused-refresh", expired=True)
        self._client.auth.refresh_exception = Exception("Invalid Refresh Token: Already Used")

        result1 = self._auth.refresh_session()
        self.assertFalse(result1.success)
        self.assertTrue(result1.error.startswith("INVALID_REFRESH_TOKEN"), result1.error)
        self.assertEqual(len(self._client.auth.refresh_calls), 1)
        # 세션이 로컬에서 무효화(파일 삭제)되어야 한다 — 재로그인 필요 상태
        self.assertFalse(os.path.exists(self._auth._session_path))
        self.assertFalse(self._auth.is_logged_in())

        # 파일이 같은 refresh_token으로 재생성되더라도(예: 경쟁하던 다른
        # 스레드가 이미 읽어뒀던 값을 뒤늦게 다시 쓰는 극단적 상황을 가정)
        # 같은 토큰으로는 다시 네트워크 요청을 보내지 않아야 한다.
        self._seed_session(refresh_token="reused-refresh", expired=True)
        result2 = self._auth.refresh_session()
        self.assertFalse(result2.success)
        self.assertTrue(result2.error.startswith("INVALID_REFRESH_TOKEN"), result2.error)
        self.assertEqual(
            len(self._client.auth.refresh_calls), 1,
            "같은 refresh_token으로 두 번째 네트워크 요청을 보내면 안 됨(무한 재시도 방지)",
        )


if __name__ == "__main__":
    unittest.main()
