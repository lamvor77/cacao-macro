# CloudSyncService.push_messages()가 실제 Supabase RLS 정책
# (messages_insert_editor / messages_update_editor: WITH CHECK (fn_can_edit()
# and updated_by = auth.uid()))과 동일한 조건에서 성공/차단되는지 검증한다.
#
# 실제 Supabase에는 연결하지 않는다 — postgrest의 실제 예외 클래스
# (postgrest.exceptions.APIError, .code 속성 포함)를 그대로 사용해 RLS 위반을
# 흉내내는 fake 테이블만 쓴다. 이 버그(실제 최초 업로드에서 INSERT가 전부
# 42501로 실패한 문제)를 재현/회귀 방지하기 위한 테스트다.
#
# 실행: python -m unittest tests.test_cloud_sync_service_rls -v

import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from postgrest.exceptions import APIError as PostgrestAPIError

from config.cloud_settings import CloudConfig
from services.cloud_sync_service import CloudSyncService, MessageRecord
from services.supabase_client import ClientResult


class FakeRLSTable:
    """messages 테이블의 실제 RLS 정책을 최소한으로 흉내낸다:

    messages_insert_editor / messages_update_editor:
        WITH CHECK (fn_can_edit() AND updated_by = auth.uid())

    can_edit=False는 "로그인 안 됨 / 미승인 / viewer 역할"을, current_uid는
    "auth.uid()"를 흉내낸다. 조건을 만족하지 못하면 실제와 동일하게
    postgrest.exceptions.APIError(code='42501')를 던진다.
    """

    def __init__(self, store: dict, current_uid, can_edit: bool):
        self._store = store  # {message_number: row dict} — 클래스 밖(테스트)과 공유되는 실제 저장소
        self._current_uid = current_uid
        self._can_edit = can_edit
        self._op = None
        self._payload = None
        self._filters: dict = {}

    def insert(self, payload):
        self._op = "insert"
        self._payload = dict(payload)
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = dict(payload)
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        self._check_with_check()

        if self._op == "insert":
            number = self._payload["message_number"]
            if number in self._store:
                raise PostgrestAPIError({
                    "code": "23505",
                    "message": f'duplicate key value violates unique constraint "messages_pkey"',
                })
            self._store[number] = dict(self._payload)
            return SimpleNamespace(data=[dict(self._payload)])

        if self._op == "update":
            number = self._filters.get("message_number")
            expected_version = self._filters.get("version")
            row = self._store.get(number)
            if row is None or row.get("version") != expected_version:
                return SimpleNamespace(data=[])  # 낙관적 잠금 충돌 — RLS와 무관, 0 rows
            row.update(self._payload)
            self._store[number] = row
            return SimpleNamespace(data=[dict(row)])

        raise AssertionError("insert()/update() 없이 execute() 호출됨")

    def _check_with_check(self):
        updated_by = self._payload.get("updated_by")
        if not self._can_edit or updated_by != self._current_uid:
            raise PostgrestAPIError({
                "code": "42501",
                "message": 'new row violates row-level security policy for table "messages"',
            })


class FakeRLSClient:
    def __init__(self, store: dict, current_uid, can_edit: bool):
        self._store = store
        self._current_uid = current_uid
        self._can_edit = can_edit

    def table(self, name):
        return FakeRLSTable(self._store, self._current_uid, self._can_edit)


class FakeRLSClientManager:
    def __init__(self, client: FakeRLSClient):
        self._client = client

    def get_client(self):
        return ClientResult(True, client=self._client)


_TMP_CACHE_DIRS: list = []


def tearDownModule():
    # 각 테스트가 만든 임시 캐시 디렉터리를 정리한다. 이 값이 없으면
    # CloudSyncService가 실제 storage/cloud_sync/message_cache.json을
    # 캐시 경로로 써서, 과거에 실제 사용자 캐시 파일을 덮어쓴 사고가 있었다.
    for d in _TMP_CACHE_DIRS:
        shutil.rmtree(d, ignore_errors=True)


def _make_service(current_uid, can_edit):
    store: dict = {}
    client = FakeRLSClient(store, current_uid=current_uid, can_edit=can_edit)
    mgr = FakeRLSClientManager(client)
    cfg = CloudConfig(
        enabled=True, url="https://project.supabase.co", anon_key="anon-key",
        device_id="pc-fallback-device",
    )
    cache_dir = tempfile.mkdtemp(prefix="cacao_rls_cache_")
    _TMP_CACHE_DIRS.append(cache_dir)
    svc = CloudSyncService(cfg, client_manager=mgr, cache_dir=cache_dir)
    return svc, store


class TestPushMessagesRLSSimulation(unittest.TestCase):
    def test_authenticated_editor_matching_updated_by_insert_succeeds(self):
        """authenticated editor + updated_by 일치 → INSERT 성공."""
        svc, store = _make_service(current_uid="uuid-editor-1", can_edit=True)

        result = svc.push_messages({1: "안녕하세요"}, updated_by="uuid-editor-1", device_id="pc-test-device")

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.updated, [1])
        self.assertEqual(store[1]["updated_by"], "uuid-editor-1")
        self.assertEqual(store[1]["source"], "pc")
        self.assertEqual(store[1]["device_id"], "pc-test-device")
        self.assertEqual(store[1]["version"], 1)

    def test_missing_updated_by_blocked_before_db_call(self):
        """updated_by 없음 → DB 호출 전에 즉시 차단(store가 비어있어야 함)."""
        svc, store = _make_service(current_uid="uuid-editor-1", can_edit=True)

        result = svc.push_messages({1: "메시지"}, updated_by=None)

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "invalid_request")
        self.assertEqual(store, {}, "DB에 아무 것도 쓰이면 안 됨")

    def test_mismatched_updated_by_blocked_by_rls(self):
        """updated_by가 실제 인증 사용자와 다름(사칭 시도) → RLS가 차단(과거 버그 재현 시나리오 포함)."""
        svc, store = _make_service(current_uid="uuid-editor-1", can_edit=True)

        result = svc.push_messages({1: "메시지"}, updated_by="uuid-someone-else")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "permission_denied")
        self.assertEqual(store, {})

    def test_anon_session_blocked(self):
        """비로그인/미승인(anon과 동등) 상태 → fn_can_edit()이 false라 항상 차단."""
        svc, store = _make_service(current_uid=None, can_edit=False)

        result = svc.push_messages({1: "메시지"}, updated_by="uuid-anything")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "permission_denied")
        self.assertEqual(store, {})

    def test_update_existing_regression(self):
        """기존 UPDATE 경로도 updated_by/source/device_id가 정확히 반영되는지(회귀)."""
        svc, store = _make_service(current_uid="uuid-editor-1", can_edit=True)
        store[1] = {
            "message_number": 1, "text": "old", "version": 1,
            "updated_by": "uuid-editor-1", "source": "mobile", "device_id": None,
        }
        svc._cache[1] = MessageRecord(1, "old", "2026-01-01T00:00:00Z", "uuid-editor-1", 1)

        result = svc.push_messages({1: "new text"}, updated_by="uuid-editor-1", device_id="pc-2")

        self.assertTrue(result.success, result.error)
        self.assertEqual(store[1]["text"], "new text")
        self.assertEqual(store[1]["version"], 2)
        self.assertEqual(store[1]["updated_by"], "uuid-editor-1")
        self.assertEqual(store[1]["source"], "pc")
        self.assertEqual(store[1]["device_id"], "pc-2")

    def test_update_with_mismatched_updated_by_blocked(self):
        """UPDATE 경로에서도 사칭 시도가 차단되는지."""
        svc, store = _make_service(current_uid="uuid-editor-1", can_edit=True)
        store[1] = {
            "message_number": 1, "text": "old", "version": 1,
            "updated_by": "uuid-editor-1", "source": "mobile", "device_id": None,
        }
        svc._cache[1] = MessageRecord(1, "old", "t", "uuid-editor-1", 1)

        result = svc.push_messages({1: "새 텍스트"}, updated_by="uuid-attacker")

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "permission_denied")
        self.assertEqual(store[1]["text"], "old", "차단됐으면 기존 값이 그대로 유지되어야 함")


if __name__ == "__main__":
    unittest.main()
