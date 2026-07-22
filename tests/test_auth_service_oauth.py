# AuthService의 Google OAuth(PKCE) 로그인 테스트.
#
# 실제 Google/Supabase에는 절대 연결하지 않는다 — supabase-py의 client.auth
# 인터페이스(sign_in_with_oauth/exchange_code_for_session/set_session/
# refresh_session/get_user/sign_out)를 흉내내는 fake만 사용한다. fake의
# 메서드 시그니처/반환 shape는 설치된 supabase==2.31.0(supabase_auth 패키지)의
# 실제 소스를 확인해서 맞춘 것이다.
#
# 실제 콜백 수신은 services/oauth_callback_server.py의 진짜 로컬 소켓을 그대로
# 쓴다 — "브라우저가 리디렉션 URL로 GET 요청을 보내는 것"만 별도 스레드로
# 흉내낸다. webbrowser.open()만 patch해서 실제 브라우저가 뜨지 않게 한다.
#
# 실행: python -m unittest tests.test_auth_service_oauth -v

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.auth_service import AuthService, AuthSession
from services.supabase_client import ClientResult
from config.cloud_settings import CloudConfig


# ============================================================
# supabase_auth 실제 반환 타입을 흉내내는 가벼운 fake
# ============================================================

@dataclass
class FakeUser:
    id: str
    email: str


@dataclass
class FakeSession:
    access_token: str
    refresh_token: str
    expires_at: int  # unix timestamp(초) — 실제 Session.expires_at과 동일한 타입
    user: FakeUser


@dataclass
class FakeAuthResponse:
    user: Optional[FakeUser]
    session: Optional[FakeSession]


@dataclass
class FakeUserResponse:
    user: Optional[FakeUser]


def _future_ts(seconds: int = 3600) -> int:
    return int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp())


def make_fake_auth_response(user_id="uuid-abc", email="tester@example.com") -> FakeAuthResponse:
    user = FakeUser(id=user_id, email=email)
    session = FakeSession(
        access_token="fake-access-token-value",
        refresh_token="fake-refresh-token-value",
        expires_at=_future_ts(),
        user=user,
    )
    return FakeAuthResponse(user=user, session=session)


class FakeGoTrueAuth:
    """supabase_auth.SyncGoTrueClient의 실제 메서드 시그니처를 흉내낸다."""

    def __init__(self):
        self.sign_in_calls: list[dict] = []
        self.exchange_calls: list[dict] = []
        self.set_session_calls: list[tuple] = []
        self.refresh_calls: list[Optional[str]] = []
        self.sign_out_calls = 0

        self.oauth_url = "https://project.supabase.co/auth/v1/authorize?provider=google&code_challenge=x"
        self.exchange_response: Optional[FakeAuthResponse] = None
        self.exchange_exception: Optional[Exception] = None
        self.set_session_response: Optional[FakeAuthResponse] = None
        self.set_session_exception: Optional[Exception] = None
        self.refresh_response: Optional[FakeAuthResponse] = None
        self.refresh_exception: Optional[Exception] = None
        self.get_user_response: Optional[FakeUserResponse] = None

    def sign_in_with_oauth(self, credentials):
        self.sign_in_calls.append(credentials)
        return type("OAuthResponse", (), {"provider": "google", "url": self.oauth_url})()

    def exchange_code_for_session(self, params):
        self.exchange_calls.append(params)
        if self.exchange_exception:
            raise self.exchange_exception
        return self.exchange_response or FakeAuthResponse(user=None, session=None)

    def set_session(self, access_token, refresh_token):
        self.set_session_calls.append((access_token, refresh_token))
        if self.set_session_exception:
            raise self.set_session_exception
        return self.set_session_response or FakeAuthResponse(user=None, session=None)

    def refresh_session(self, refresh_token=None):
        self.refresh_calls.append(refresh_token)
        if self.refresh_exception:
            raise self.refresh_exception
        return self.refresh_response or FakeAuthResponse(user=None, session=None)

    def get_user(self, jwt=None):
        return self.get_user_response

    def sign_out(self, options=None):
        self.sign_out_calls += 1


class FakeTableQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *args, **kwargs):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def execute(self):
        return type("Response", (), {"data": self._rows})()


class FakeSupabaseClient:
    def __init__(self):
        self.auth = FakeGoTrueAuth()
        self.table_rows: dict[str, list[dict]] = {}

    def table(self, name):
        return FakeTableQuery(list(self.table_rows.get(name, [])))


class FakeClientManager:
    """SupabaseClientManager의 get_client()만 흉내낸다."""

    def __init__(self, client: Optional[FakeSupabaseClient] = None):
        self.client = client or FakeSupabaseClient()

    def get_client(self):
        return ClientResult(True, client=self.client)


def _http_get_async(url: str, delay: float = 0.05) -> None:
    def _run():
        time.sleep(delay)
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            e.close()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


class AuthServiceOAuthTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="cacao_auth_oauth_")
        self._client = FakeSupabaseClient()
        self._mgr = FakeClientManager(client=self._client)
        cfg = CloudConfig(enabled=True, url="https://project.supabase.co", anon_key="anon-key",
                           device_id="pc-test")
        self._auth = AuthService(config=cfg, client_manager=self._mgr)
        self._auth._session_path = os.path.join(self._tmp_dir, "session.dat")

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)


class TestLoginFlow(AuthServiceOAuthTestBase):
    def test_1_oauth_url_generated_and_browser_opened(self):
        """1. OAuth URL 생성 — sign_in_with_oauth가 provider=google, redirect_to로 호출된다."""
        self._client.auth.exchange_response = make_fake_auth_response()
        opened = []

        def fake_open(url):
            opened.append(url)
            redirect_to = self._client.auth.sign_in_calls[-1]["options"]["redirect_to"]
            _http_get_async(redirect_to + "?code=fake-code")
            return True

        with patch("services.auth_service.webbrowser.open", side_effect=fake_open):
            result = self._auth.login_with_google(timeout_seconds=5)

        self.assertTrue(result.success, result.error)
        self.assertEqual(len(self._client.auth.sign_in_calls), 1)
        self.assertEqual(self._client.auth.sign_in_calls[0]["provider"], "google")
        self.assertTrue(self._client.auth.sign_in_calls[0]["options"]["redirect_to"].startswith("http://127.0.0.1:"))
        self.assertEqual(opened, [self._client.auth.oauth_url])

    def test_full_login_success_saves_session(self):
        self._client.auth.exchange_response = make_fake_auth_response(user_id="uuid-1", email="a@b.com")

        def fake_open(url):
            redirect_to = self._client.auth.sign_in_calls[-1]["options"]["redirect_to"]
            _http_get_async(redirect_to + "?code=abc123")
            return True

        with patch("services.auth_service.webbrowser.open", side_effect=fake_open):
            result = self._auth.login_with_google(timeout_seconds=5)

        self.assertTrue(result.success)
        self.assertEqual(result.session.user_id, "uuid-1")
        self.assertEqual(result.session.email, "a@b.com")
        # exchange_code_for_session에 code_verifier를 직접 넘기지 않았는지(빈 문자열로
        # 넘겨 SDK가 내부 저장소 값을 쓰게 했는지) 확인
        self.assertEqual(self._client.auth.exchange_calls[0]["auth_code"], "abc123")
        self.assertEqual(self._client.auth.exchange_calls[0]["code_verifier"], "")

    def test_login_cancelled_by_user(self):
        """8번 원칙: 로그인 취소가 예외가 아니라 정상 결과로 처리된다."""
        def fake_open(url):
            redirect_to = self._client.auth.sign_in_calls[-1]["options"]["redirect_to"]
            _http_get_async(redirect_to + "?error=access_denied&error_description=User+cancelled")
            return True

        with patch("services.auth_service.webbrowser.open", side_effect=fake_open):
            result = self._auth.login_with_google(timeout_seconds=5)

        self.assertFalse(result.success)
        self.assertIn("취소", result.error)

    def test_login_timeout(self):
        with patch("services.auth_service.webbrowser.open", return_value=True):
            result = self._auth.login_with_google(timeout_seconds=0.3)

        self.assertFalse(result.success)
        self.assertIn("시간이 초과", result.error)

    def test_7_session_saved_after_exchange(self):
        """7. 세션 저장 — complete_oauth_callback 성공 후 DPAPI로 저장되어 다시 로드된다."""
        self._client.auth.exchange_response = make_fake_auth_response(user_id="uuid-7", email="seven@x.com")

        result = self._auth.complete_oauth_callback(code="c", redirect_to="http://127.0.0.1:1/x")

        self.assertTrue(result.success)
        loaded = self._auth.load_session()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.user_id, "uuid-7")
        self.assertEqual(loaded.email, "seven@x.com")


class TestApplySessionAndRefresh(AuthServiceOAuthTestBase):
    def _seed_session(self, expired: bool = False) -> AuthSession:
        ts = _future_ts(-3600) if expired else _future_ts()
        expires_at_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        session = AuthSession(
            user_id="uuid-x", email="x@example.com",
            access_token="old-access", refresh_token="old-refresh",
            expires_at=expires_at_iso,
        )
        self._auth._save_session(session)
        return session

    def test_8_apply_session_to_client(self):
        """8. 세션 client 적용 — set_session(access_token, refresh_token)이 호출된다."""
        self._seed_session(expired=False)
        self._client.auth.set_session_response = make_fake_auth_response(user_id="uuid-x")

        result = self._auth.apply_session_to_client()

        self.assertTrue(result.success)
        self.assertEqual(len(self._client.auth.set_session_calls), 1)
        self.assertEqual(self._client.auth.set_session_calls[0], ("old-access", "old-refresh"))

    def test_9_refresh_success(self):
        """9. refresh 성공"""
        self._seed_session(expired=True)
        self._client.auth.refresh_response = make_fake_auth_response(user_id="uuid-x", email="refreshed@x.com")

        result = self._auth.refresh_session()

        self.assertTrue(result.success)
        self.assertEqual(result.session.email, "refreshed@x.com")
        self.assertEqual(self._client.auth.refresh_calls, ["old-refresh"])

    def test_10_refresh_failure_requires_login_again(self):
        """10. refresh 실패 — get_session()이 None을 반환해 로그인 필요 상태가 된다."""
        self._seed_session(expired=True)
        self._client.auth.refresh_exception = RuntimeError("invalid_grant")

        refresh_result = self._auth.refresh_session()
        self.assertFalse(refresh_result.success)

        session = self._auth.get_session()  # 내부적으로 refresh를 1회 더 시도하고 실패 → None
        self.assertIsNone(session)
        self.assertFalse(self._auth.is_logged_in())


class TestLogging(AuthServiceOAuthTestBase):
    def test_18_tokens_not_leaked_in_logs(self):
        """18. access/refresh token, code_verifier가 로그에 노출되지 않는다."""
        SECRET_ACCESS = "super-secret-access-token-zzz"
        SECRET_REFRESH = "super-secret-refresh-token-zzz"
        self._client.auth.exchange_response = FakeAuthResponse(
            user=FakeUser(id="uuid-log", email="log@example.com"),
            session=FakeSession(
                access_token=SECRET_ACCESS,
                refresh_token=SECRET_REFRESH,
                expires_at=_future_ts(),
                user=FakeUser(id="uuid-log", email="log@example.com"),
            ),
        )

        with self.assertLogs("services.auth_service", level="DEBUG") as captured:
            result = self._auth.complete_oauth_callback(code="the-auth-code-value", redirect_to="http://127.0.0.1:1/x")

        self.assertTrue(result.success)
        all_messages = "\n".join(captured.output)
        self.assertNotIn(SECRET_ACCESS, all_messages)
        self.assertNotIn(SECRET_REFRESH, all_messages)
        self.assertNotIn("the-auth-code-value", all_messages)

    def test_18_exchange_failure_does_not_log_exception_detail(self):
        """exchange_code_for_session 실패 시에도 예외 상세(코드/verifier 포함 가능)를 로그로 남기지 않는다."""
        self._client.auth.exchange_exception = RuntimeError("invalid_grant: auth_code=the-auth-code-value")

        with self.assertLogs("services.auth_service", level="DEBUG") as captured:
            result = self._auth.complete_oauth_callback(code="the-auth-code-value", redirect_to="http://127.0.0.1:1/x")

        self.assertFalse(result.success)
        all_messages = "\n".join(captured.output)
        self.assertNotIn("the-auth-code-value", all_messages)


if __name__ == "__main__":
    unittest.main()
