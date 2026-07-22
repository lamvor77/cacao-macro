# services/cloud_sync_service.py의 pull_message() (단건 조회) 테스트
#
# 상시 30초 폴링을 제거하면서 발송 직전 검증(core/legacy_send_verification.py)이
# 이 메서드로 message_no 1건만 조회한다 — 전체 12건을 읽는 pull_messages()와는
# 분리된 경로임을 확인한다. 실제 Supabase에는 연결하지 않는다.

import shutil
import tempfile
import unittest
from types import SimpleNamespace

from config.cloud_settings import CloudConfig
from services.cloud_sync_service import CloudSyncService, MessageRecord
from services.supabase_client import ClientResult


class _FakeSelectQuery:
    def __init__(self, rows_by_number: dict, raise_error: bool = False):
        self._rows = rows_by_number
        self._raise_error = raise_error
        self._number_filter = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, column, value):
        assert column == "message_number"
        self._number_filter = value
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._raise_error:
            raise ConnectionError("network down")
        row = self._rows.get(self._number_filter)
        return SimpleNamespace(data=[row] if row is not None else [])


class _FakeClient:
    def __init__(self, rows_by_number: dict, raise_error: bool = False):
        self._rows = rows_by_number
        self._raise_error = raise_error
        self.select_call_count = 0

    def table(self, _name):
        self.select_call_count += 1
        return _FakeSelectQuery(self._rows, raise_error=self._raise_error)


class _FakeClientManager:
    def __init__(self, client):
        self._client = client

    def get_client(self):
        return ClientResult(True, client=self._client)

    def check_connection(self):
        return ClientResult(True)


class TestPullMessage(unittest.TestCase):
    def setUp(self):
        # 실제 storage/cloud_sync/message_cache.json을 건드리지 않도록 매번
        # 격리된 임시 캐시 디렉터리를 쓴다(CloudSyncService의 기존 관례).
        self._tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _make_service(self, rows_by_number=None, raise_error=False, enabled=True):
        client = _FakeClient(rows_by_number or {}, raise_error=raise_error)
        config = CloudConfig(enabled=enabled, url="https://x.supabase.co", anon_key="k",
                              device_id="pc-test")
        svc = CloudSyncService(client_manager=_FakeClientManager(client), config=config, cache_dir=self._tmp_dir)
        return svc, client

    def test_returns_message_record_for_existing_number(self):
        svc, client = self._make_service(rows_by_number={
            3: {"message_number": 3, "text": "hello", "updated_at": "2026-01-01T00:00:00Z",
                "updated_by": "u1", "version": 4},
        })
        record = svc.pull_message(3)
        self.assertIsInstance(record, MessageRecord)
        self.assertEqual(record.number, 3)
        self.assertEqual(record.text, "hello")
        self.assertEqual(record.version, 4)

    def test_returns_none_when_number_not_found(self):
        svc, _ = self._make_service(rows_by_number={})
        self.assertIsNone(svc.pull_message(7))

    def test_returns_none_on_network_error(self):
        svc, _ = self._make_service(raise_error=True)
        self.assertIsNone(svc.pull_message(1))

    def test_returns_none_when_disabled(self):
        svc, client = self._make_service(rows_by_number={1: {"message_number": 1, "text": "x", "version": 1}}, enabled=False)
        self.assertIsNone(svc.pull_message(1))
        self.assertEqual(client.select_call_count, 0, "비활성화 상태면 테이블 조회 자체를 시도하면 안 됨")

    def test_does_not_mutate_internal_cache(self):
        """pull_messages()/push_messages()의 CAS 기준 캐시를 이 단건 조회가
        건드리면 안 된다 — 전체 재조회와 독립적으로 동작해야 한다."""
        svc, _ = self._make_service(rows_by_number={
            5: {"message_number": 5, "text": "y", "updated_at": "", "updated_by": "u", "version": 9},
        })
        svc.pull_message(5)
        self.assertNotIn(5, svc._cache)

    def test_only_queries_requested_number_not_all(self):
        svc, client = self._make_service(rows_by_number={
            1: {"message_number": 1, "text": "a", "version": 1},
            2: {"message_number": 2, "text": "b", "version": 1},
        })
        record = svc.pull_message(2)
        self.assertEqual(record.number, 2)
        self.assertEqual(client.select_call_count, 1, "단건 조회는 테이블 접근이 1회여야 함")


if __name__ == "__main__":
    unittest.main()
