# AuthService와 CloudSyncService가 실제로 "같은" Supabase Client 인스턴스(및 그
# 안의 인증 세션)를 공유하는지 검증한다.
#
# 버그였던 것: CloudSyncCoordinator가 cloud_service를 주입받지 않으면 자체적으로
# CloudSyncService()를 새로 만들었는데, 이 서비스는 AuthService와 무관한 별도의
# SupabaseClientManager/Client를 갖게 되어 로그인 세션이 전혀 반영되지 않았다 —
# 그 결과 messages INSERT/UPDATE가 항상 RLS(42501)로 거부됐다. 이 파일은 그 수정을
# 검증한다.
#
# 실제 Supabase에는 연결하지 않는다.
#
# 실행: python -m unittest tests.test_client_sharing -v

import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config.cloud_settings import CloudConfig
from services.auth_service import AuthService
from services.cloud_sync_coordinator import CloudSyncCoordinator
from services.cloud_sync_service import CloudSyncService
from services.supabase_client import ClientResult


# ============================================================
# 최소 fake — supabase_auth의 실제 반환 shape만 흉내낸다
# ============================================================

@dataclass
class FakeUser:
    id: str
    email: str


@dataclass
class FakeSession:
    access_token: str
    refresh_token: str
    expires_at: int
    user: FakeUser


@dataclass
class FakeAuthResponse:
    user: Optional[FakeUser]
    session: Optional[FakeSession]


def _future_ts(seconds: int = 3600) -> int:
    return int((datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp())


class FakeGoTrueAuth:
    def __init__(self):
        self.set_session_calls: list[tuple] = []
        self.refresh_calls: list[Optional[str]] = []
        self.get_user_calls = 0
        self.current_user: Optional[FakeUser] = None  # set_session/refresh가 갱신

    def set_session(self, access_token, refresh_token):
        self.set_session_calls.append((access_token, refresh_token))
        return FakeAuthResponse(user=self.current_user, session=self._session_for(access_token, refresh_token))

    def refresh_session(self, refresh_token=None):
        self.refresh_calls.append(refresh_token)
        # refresh는 항상 새 토큰을 발급한다고 가정(실제 SDK와 동일한 결과 shape)
        new_user = FakeUser(id=self.current_user.id, email=self.current_user.email)
        new_session = FakeSession(
            access_token="refreshed-access-token", refresh_token="refreshed-refresh-token",
            expires_at=_future_ts(), user=new_user,
        )
        self.current_user = new_user
        return FakeAuthResponse(user=new_user, session=new_session)

    def get_user(self, jwt=None):
        self.get_user_calls += 1
        if self.current_user is None:
            return None
        return type("UserResponse", (), {"user": self.current_user})()

    def _session_for(self, access_token, refresh_token):
        if self.current_user is None:
            return None
        return FakeSession(access_token=access_token, refresh_token=refresh_token,
                            expires_at=_future_ts(), user=self.current_user)


class FakeTableQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k):
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
        self.table_calls: list[str] = []

    def table(self, name):
        self.table_calls.append(name)
        return FakeTableQuery(list(self.table_rows.get(name, [])))


class FakeClientManager:
    def __init__(self, client: FakeSupabaseClient):
        self.client = client

    def get_client(self):
        return ClientResult(True, client=self.client)


class TestClientSharing(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="cacao_client_share_")
        self._client = FakeSupabaseClient()
        self._mgr = FakeClientManager(self._client)
        cfg = CloudConfig(enabled=True, url="https://project.supabase.co", anon_key="anon-key",
                           sync_interval_seconds=30, device_id="pc-test")
        self._auth = AuthService(config=cfg, client_manager=self._mgr)
        self._auth._session_path = os.path.join(self._tmp_dir, "session.dat")

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_coordinator_shares_client_manager_between_auth_and_cloud(self):
        """CloudSyncCoordinator가 cloud_service 없이 만들어지면(MainWindow의 실제
        사용 패턴), AuthService와 같은 client_manager(=같은 Client)를 써야 한다."""
        coord = CloudSyncCoordinator(
            get_messages_fn=lambda: {},
            apply_messages_fn=lambda m: None,
            log_fn=lambda m: None,
            status_fn=lambda info: None,
            notify_scheduler_fn=lambda: None,
            auth_service=self._auth,  # cloud_service는 일부러 주지 않음
            state_dir=self._tmp_dir,
        )

        self.assertIs(coord._auth, self._auth)
        self.assertIs(coord._cloud.client_manager, self._auth.client_manager)
        self.assertIs(coord._cloud.client_manager.get_client().client, self._client)

    def test_default_cloud_sync_service_shares_manager_when_given_one(self):
        """CloudSyncService(client_manager=...)로 명시적으로 공유시키면 같은 Client를 쓴다."""
        svc = CloudSyncService(client_manager=self._mgr)
        self.assertIs(svc.client_manager, self._mgr)
        self.assertIs(svc.client_manager.get_client().client, self._client)

    def test_session_restored_then_matches_current_user(self):
        """저장된 세션을 로드(복원)하면 그 UUID가 get_current_user()/get_app_user_profile()에도 그대로 쓰인다."""
        user = FakeUser(id="uuid-restored", email="restored@example.com")
        self._client.auth.current_user = user

        future = datetime.fromtimestamp(_future_ts(), tz=timezone.utc).isoformat()
        from services.auth_service import AuthSession
        session = AuthSession(
            user_id="uuid-restored", email="restored@example.com",
            access_token="stored-access", refresh_token="stored-refresh", expires_at=future,
        )
        self._auth._save_session(session)

        restored = self._auth.get_session()
        self.assertIsNotNone(restored)
        self.assertEqual(restored.user_id, "uuid-restored")

        current_user = self._auth.get_current_user()
        self.assertIsNotNone(current_user)
        self.assertEqual(current_user.id, "uuid-restored")
        # apply_session_to_client()가 실제로 이 client의 set_session을 호출했는지
        self.assertGreaterEqual(len(self._client.auth.set_session_calls), 1)
        self.assertEqual(self._client.auth.set_session_calls[-1], ("stored-access", "stored-refresh"))

    def test_refresh_applies_new_session_to_shared_client(self):
        """refresh_session() 후 새 세션이 CloudSyncService가 쓰는 것과 같은 Client에도 적용된다."""
        user = FakeUser(id="uuid-refresh-target", email="r@example.com")
        self._client.auth.current_user = user

        past = datetime.fromtimestamp(_future_ts(-3600), tz=timezone.utc).isoformat()
        from services.auth_service import AuthSession
        expired_session = AuthSession(
            user_id="uuid-refresh-target", email="r@example.com",
            access_token="old-access", refresh_token="old-refresh", expires_at=past,
        )
        self._auth._save_session(expired_session)

        # get_session()은 만료를 감지하고 refresh_session()을 자동 호출한다
        session = self._auth.get_session()

        self.assertIsNotNone(session)
        self.assertEqual(session.access_token, "refreshed-access-token")
        self.assertEqual(self._client.auth.refresh_calls, ["old-refresh"])

        # CloudSyncService가 실제로 참조하는 client가 바로 이 client이므로,
        # 이후 push_messages()가 이 새 세션으로 인증된 상태에서 요청을 보내게 된다
        # (client_manager 공유 확인)
        cloud = CloudSyncService(client_manager=self._mgr)
        self.assertIs(cloud.client_manager.get_client().client, self._client)


if __name__ == "__main__":
    unittest.main()
