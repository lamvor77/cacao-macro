# Phase 2E 후속: LOCAL_PENDING(정상 업로드 대기) vs 실제 CONFLICT 구분 테스트
#
# 이전 버그: 30초 폴링이 "로컬 dirty + 클라우드 버전이 내가 마지막으로 확인한
# 버전보다 앞섬"이라는 조건만으로 충돌을 판정했다. 15분 조건부 업로드 구조
# (Phase 2E)에서는 이 조건이 "내가 아직 업로드하지 못했을 뿐"인 정상 대기
# 상태에서도 흔히 성립해, 다른 기기의 실제 개입 없이도 충돌로 잘못 집계됐다.
# last_synced_text(마지막으로 클라우드와 일치를 확인한 텍스트)를 추가로
# 비교해 두 상태를 구분한다.
#
# 실제 Supabase/storage에는 절대 연결/접근하지 않는다.
#
# 실행: python -m unittest tests.test_local_pending_vs_conflict -v

import os
import shutil
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.cloud_state import CloudState
from services.cloud_sync_coordinator import CloudSyncCoordinator, _LocalMessageState
from services.cloud_sync_service import MessageRecord, SyncResult
from storage.data_manager import DataManager

from tests.test_cloud_sync_coordinator import FakeAuthService, FakeCloudSyncService, _Recorder, _profile


class PendingVsConflictTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp_state_dir = tempfile.mkdtemp(prefix="cacao_pending_conflict_")
        self._data_manager = DataManager()

    def tearDown(self):
        shutil.rmtree(self._tmp_state_dir, ignore_errors=True)

    def make_coordinator(self, recorder, cloud, auth, poll_interval=0.05, dirty_push_interval=5.0):
        return CloudSyncCoordinator(
            get_messages_fn=recorder.get_messages,
            apply_messages_fn=recorder.apply_messages,
            log_fn=recorder.log,
            status_fn=recorder.status,
            notify_scheduler_fn=recorder.notify_scheduler,
            data_manager=self._data_manager,
            cloud_service=cloud,
            auth_service=auth,
            poll_interval_seconds=poll_interval,
            state_dir=self._tmp_state_dir,
            dirty_push_interval_seconds=dirty_push_interval,
        )

    def _wait_until(self, predicate, timeout=2.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()


# ============================================================
# 1~4: _reconcile() 분류 자체 (last_synced_text 기반)
# ============================================================

class TestReconcileClassification(PendingVsConflictTestBase):
    def test_1_local_only_change_is_pending_not_conflict(self):
        """1. last_synced=A, local=B(dirty), remote=A(변경 없음) → pending 1, conflict 0."""
        recorder = _Recorder()
        coord = self.make_coordinator(recorder, FakeCloudSyncService(), FakeAuthService())
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="B", last_synced_text="A")
        cloud_records = {1: MessageRecord(1, "A", "t", "someone", 1)}

        to_apply, to_push, conflicts, pending, identical = coord._reconcile({1: "B"}, cloud_records)

        self.assertEqual(pending, [1])
        self.assertEqual(conflicts, [])
        self.assertEqual(to_push, [1], "정상 대기 상태도 업로드 후보에는 포함되어야 함")
        self.assertEqual(to_apply, {})

    def test_2_real_remote_change_is_conflict(self):
        """2. last_synced=A, local=B(dirty), remote=C(버전 증가, 실제 변경) → pending 0, conflict 1."""
        recorder = _Recorder()
        coord = self.make_coordinator(recorder, FakeCloudSyncService(), FakeAuthService())
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="B", last_synced_text="A")
        cloud_records = {1: MessageRecord(1, "C", "t", "다른기기", 2)}

        to_apply, to_push, conflicts, pending, identical = coord._reconcile({1: "B"}, cloud_records)

        self.assertEqual(conflicts, [1])
        self.assertEqual(pending, [])
        self.assertEqual(to_push, [], "진짜 충돌은 업로드 후보에 포함되면 안 됨")

    def test_3_remote_only_change_applies(self):
        """3. last_synced=A, local=A(clean), remote=B → applied 1, conflict 0."""
        recorder = _Recorder()
        coord = self.make_coordinator(recorder, FakeCloudSyncService(), FakeAuthService())
        coord._local_state[1] = _LocalMessageState(version=1, dirty=False, last_text="A", last_synced_text="A")
        cloud_records = {1: MessageRecord(1, "B", "t", "다른기기", 2)}

        to_apply, to_push, conflicts, pending, identical = coord._reconcile({1: "A"}, cloud_records)

        self.assertEqual(to_apply, {1: "B"})
        self.assertEqual(conflicts, [])
        self.assertEqual(pending, [])

    def test_4_converged_to_same_text_clears_dirty_no_conflict(self):
        """4. local=B(dirty), remote=B(우연히 같은 값으로 수렴) → conflict 0, dirty 해제."""
        recorder = _Recorder()
        coord = self.make_coordinator(recorder, FakeCloudSyncService(), FakeAuthService())
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="B", last_synced_text="A")
        cloud_records = {1: MessageRecord(1, "B", "t", "다른기기", 9)}

        to_apply, to_push, conflicts, pending, identical = coord._reconcile({1: "B"}, cloud_records)
        self.assertEqual(identical, [1])
        self.assertEqual(conflicts, [])
        self.assertEqual(pending, [])

        coord._mark_identical(identical, {1: "B"}, cloud_records)
        self.assertFalse(coord._local_state[1].dirty, "동일한 값으로 수렴했으면 dirty가 해제되어야 함")
        self.assertFalse(coord._local_state[1].conflict)
        self.assertEqual(coord._local_state[1].last_synced_text, "B")


# ============================================================
# 5~6: 15분 tick의 pending/conflict 처리 분리
# ============================================================

class TestTickBehavior(PendingVsConflictTestBase):
    def test_5_pending_message_uploads_on_tick(self):
        """5. pending 상태 메시지는 15분 tick에서 정상 업로드된다."""
        recorder = _Recorder(initial_messages={1: "B"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="B", last_synced_text="A", conflict=False)

        coord._tick_dirty_cloud_sync()

        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 1))
        self.assertEqual(cloud.push_call_args[0].get(1), "B")

    def test_6_conflict_message_never_auto_uploads_on_tick(self):
        """6. conflict 상태 메시지는 15분 tick에서 자동 업로드되지 않는다."""
        recorder = _Recorder(initial_messages={1: "B"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="B", last_synced_text="A", conflict=True)

        coord._tick_dirty_cloud_sync()
        time.sleep(0.3)

        self.assertEqual(cloud.push_calls, 0)

    def test_conflict_message_does_not_block_other_pushable_dirty_messages(self):
        """섹션4: 한 메시지가 conflict여도 충돌 없는 다른 dirty 메시지는 정상 업로드된다."""
        recorder = _Recorder(initial_messages={1: "충돌 메시지", 2: "정상 대기 메시지"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="충돌 메시지", conflict=True)
        coord._local_state[2] = _LocalMessageState(version=1, dirty=True, last_text="정상 대기 메시지", last_synced_text="옛값", conflict=False)

        coord._tick_dirty_cloud_sync()

        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 1))
        payload = cloud.push_call_args[0]
        self.assertNotIn(1, payload, "충돌 메시지는 자동 업로드 payload에서 제외되어야 함")
        self.assertEqual(payload.get(2), "정상 대기 메시지")


# ============================================================
# 7~10: 로그/상태 집계 (실제 _sync_once 경로)
# ============================================================

class TestSyncOnceLoggingAndCounts(PendingVsConflictTestBase):
    def test_7_pending_logs_info_not_warning(self):
        """7. pending 상태는 INFO 로그로 남는다(경고 아님)."""
        recorder = _Recorder(initial_messages={1: "B"})
        cloud = FakeCloudSyncService(enabled=True)
        cloud.pull_result = SyncResult(True, messages={1: MessageRecord(1, "A", "t", "u", 1)})
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="B", last_synced_text="A")

        coord._sync_once(is_initial=False, can_write=True, current_user_id="test-user-uuid", allow_reconcile_push=False)

        self.assertTrue(any("[INFO] 로컬 변경 대기" in m for m in recorder.logs))
        self.assertFalse(any("[경고]" in m for m in recorder.logs), "정상 대기 상태를 경고로 표시하면 안 됨")

    def test_8_real_conflict_logs_warning(self):
        """8. 실제 충돌 상태는 경고([경고]) 로그로 남는다."""
        recorder = _Recorder(initial_messages={1: "B"})
        cloud = FakeCloudSyncService(enabled=True)
        cloud.pull_result = SyncResult(True, messages={1: MessageRecord(1, "C", "t", "다른기기", 2)})
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="B", last_synced_text="A")

        coord._sync_once(is_initial=False, can_write=True, current_user_id="test-user-uuid", allow_reconcile_push=False)

        self.assertTrue(any("[경고] 실제 변경 충돌 감지" in m for m in recorder.logs))
        self.assertEqual(coord.get_status().state, CloudState.CONFLICT)

    def test_9_mixed_pending_and_conflict_counted_separately(self):
        """9. 메시지 1은 pending, 메시지 2는 conflict → 대기 1, 충돌 1로 각각 집계."""
        recorder = _Recorder(initial_messages={1: "B", 2: "D"})
        cloud = FakeCloudSyncService(enabled=True)
        cloud.pull_result = SyncResult(True, messages={
            1: MessageRecord(1, "A", "t", "u", 1),   # 변경 없음 → pending
            2: MessageRecord(2, "실제로 바뀐 값", "t", "다른기기", 5),  # 실제 변경 → conflict
        })
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="B", last_synced_text="A")
        coord._local_state[2] = _LocalMessageState(version=1, dirty=True, last_text="D", last_synced_text="C")

        coord._sync_once(is_initial=False, can_write=True, current_user_id="test-user-uuid", allow_reconcile_push=False)

        self.assertTrue(any("대기 1건, 충돌 1건" in m for m in recorder.logs))
        self.assertTrue(coord._local_state[1].conflict is False)
        self.assertTrue(coord._local_state[2].conflict is True)

    def test_10_single_local_edit_during_manual_refresh_is_pending_not_conflict(self):
        """10. 로컬 수정 1개 후 수동 새로고침 → "충돌 1건"이 아니라 "대기 1건, 충돌 0건"."""
        recorder = _Recorder(initial_messages={1: "로컬에서 수정함"})
        cloud = FakeCloudSyncService(enabled=True)
        # 원격은 이 PC가 마지막으로 확인한 그대로 — 아무도 건드리지 않았다.
        cloud.pull_result = SyncResult(True, messages={1: MessageRecord(1, "이전 확인값", "t", "u", 1)})
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._local_state[1] = _LocalMessageState(
            version=1, dirty=True, last_text="로컬에서 수정함", last_synced_text="이전 확인값",
        )

        coord._do_manual_refresh()

        self.assertTrue(any("대기 1건, 충돌 0건" in m for m in recorder.logs))
        self.assertFalse(any("충돌 1건" in m and "대기 0건" in m for m in recorder.logs))
        self.assertNotEqual(coord.get_status().state, CloudState.CONFLICT)
        self.assertEqual(cloud.push_calls, 0, "수동 새로고침에서는 여전히 즉시 업로드하지 않아야 함")


if __name__ == "__main__":
    unittest.main()
