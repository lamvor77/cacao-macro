# Phase 2E: 로컬 자동 저장(디바운스) + Supabase 15분 조건부 동기화 테스트
#
# ControlPanel의 실제 KeyRelease 바인딩/디바운스 타이머(self.after)는 customtkinter
# 위젯과 Tk mainloop에 의존하므로 이 프로젝트의 기존 관례(GUI 위젯 자체는 자동화
# 테스트하지 않음 — tests/ 디렉터리에 GUI 테스트가 전혀 없다)를 따라 여기서는
# 다루지 않는다. 대신 그 아래에서 실제로 동작하는 CloudSyncCoordinator의 새 공개
# 메서드(notify_local_autosave / request_push / shutdown_flush / has_cloud_dirty /
# set_input_in_progress)를 직접 검증한다 — 이 메서드들이 프레임워크와 무관한
# 실제 로직을 담고 있다. GUI 배선 자체는 docs의 수동 테스트 절차로 확인한다.
#
# 실제 Supabase/storage에는 절대 연결/접근하지 않는다 — tests/test_cloud_sync_coordinator.py의
# fake(FakeCloudSyncService/FakeAuthService/_Recorder)를 그대로 재사용한다.
#
# 실행: python -m unittest tests.test_message_cloud_sync_phase2e -v

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.auth_service import AppUserProfile
from services.cloud_state import CloudState
from services.cloud_sync_coordinator import CloudSyncCoordinator, _LocalMessageState
from services.cloud_sync_service import SyncResult
from storage.data_manager import DataManager

from tests.test_cloud_sync_coordinator import FakeAuthService, FakeCloudSyncService, _Recorder, _profile


class GatedFakeCloudSyncService(FakeCloudSyncService):
    """push_messages() 호출을 threading.Event로 원하는 순간까지 붙잡아둘 수 있는 fake.

    "업로드 진행 중에 재수정이 들어오면 pending으로 보존했다가 완료 후 최신값을
    한 번 더 올린다"(동시성 시나리오)를 결정적으로 재현하기 위해 필요하다.
    """

    def __init__(self, enabled: bool = True):
        super().__init__(enabled=enabled)
        self.release_event = threading.Event()
        self.entered_event = threading.Event()  # push_messages 진입을 테스트 스레드에 알림
        self.gate_enabled = False
        self.calls_started = 0  # push_calls(완료 수)와 달리 "시작된" 호출 수 — 게이트 중에도 증가

    def push_messages(self, messages: dict, updated_by=None, device_id=None) -> SyncResult:
        self.calls_started += 1
        if self.gate_enabled:
            self.entered_event.set()
            self.release_event.wait(timeout=5.0)
        return super().push_messages(messages, updated_by=updated_by, device_id=device_id)


class Phase2ETestBase(unittest.TestCase):
    def setUp(self):
        self._tmp_state_dir = tempfile.mkdtemp(prefix="cacao_p2e_state_")
        self._data_manager = DataManager()

    def tearDown(self):
        shutil.rmtree(self._tmp_state_dir, ignore_errors=True)

    def make_coordinator(
        self,
        recorder: _Recorder,
        cloud,
        auth: FakeAuthService,
        poll_interval: float = 0.05,
        dirty_push_interval: float = 5.0,
    ) -> CloudSyncCoordinator:
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
# 로컬 자동 저장 (notify_local_autosave)
# ============================================================

class TestLocalAutosave(Phase2ETestBase):
    def test_autosave_writes_local_file(self):
        """1/5. 자동 저장이 로컬 파일에 실제로 반영되고 atomic write를 사용한다."""
        recorder = _Recorder()
        coord = self.make_coordinator(recorder, FakeCloudSyncService(enabled=False), FakeAuthService(logged_in=False))

        ok = coord.notify_local_autosave({1: "자동 저장된 메시지"})

        self.assertTrue(ok)
        saved = self._data_manager.load(coord._local_file)
        self.assertEqual(saved["messages"].get(1), "자동 저장된 메시지")
        self.assertFalse(os.path.exists(coord._local_file + ".tmp"), "임시 파일이 정리되어야 함")

    def test_same_content_skips_duplicate_write(self):
        """3. 같은 내용을 다시 자동 저장하면 디스크에 다시 쓰지 않는다."""
        recorder = _Recorder()
        coord = self.make_coordinator(recorder, FakeCloudSyncService(enabled=False), FakeAuthService(logged_in=False))

        self.assertTrue(coord.notify_local_autosave({1: "동일 내용"}))
        mtime_after_first = os.path.getmtime(coord._local_file)
        time.sleep(0.05)

        ok = coord.notify_local_autosave({1: "동일 내용"})

        self.assertFalse(ok, "동일 내용이면 저장을 건너뛰어야 함")
        self.assertEqual(os.path.getmtime(coord._local_file), mtime_after_first)

    def test_write_failure_preserves_existing_file(self):
        """6. 쓰기 실패 시 기존 파일이 보존된다."""
        recorder = _Recorder()
        coord = self.make_coordinator(recorder, FakeCloudSyncService(enabled=False), FakeAuthService(logged_in=False))
        coord.notify_local_autosave({1: "원본 내용"})

        def _boom(messages, filepath):
            raise PermissionError("파일이 다른 프로그램에서 열려 있습니다")

        self._data_manager.save_messages = _boom
        ok = coord.notify_local_autosave({1: "실패해야 하는 내용"})

        self.assertFalse(ok)
        self.assertTrue(any("자동 저장 실패" in m for m in recorder.logs))
        # DataManager.save_messages를 되돌리고 실제 파일 내용은 원본 그대로인지 확인
        self._data_manager = DataManager()
        saved = self._data_manager.load(coord._local_file)
        self.assertEqual(saved["messages"].get(1), "원본 내용")

    def test_autosave_never_pushes_to_cloud(self):
        """4/5. 로컬 자동 저장 자체는 클라우드 업로드를 시도하지 않는다(15분 주기 전용)."""
        recorder = _Recorder()
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.notify_local_autosave({1: "입력 중"})
        time.sleep(0.2)

        self.assertEqual(cloud.push_calls, 0)
        self.assertTrue(coord.has_cloud_dirty(), "다음 15분 tick/명시적 이벤트를 위해 dirty는 유지되어야 함")

    def test_applying_remote_blocks_autosave(self):
        """32. 클라우드 반영 도중에는 자동 저장도 억제된다(루프 방지)."""
        recorder = _Recorder()
        coord = self.make_coordinator(recorder, FakeCloudSyncService(enabled=False), FakeAuthService(logged_in=False))
        coord._applying_remote = True

        ok = coord.notify_local_autosave({1: "억제되어야 함"})

        self.assertFalse(ok)
        self.assertFalse(os.path.exists(coord._local_file))


# ============================================================
# 15분 조건부 업로드 (_tick_dirty_cloud_sync)
# ============================================================

class TestDirtyIntervalTick(Phase2ETestBase):
    def test_no_dirty_skips_db_call(self):
        """9. cloud_dirty=false이면 tick에서 DB 요청 자체가 없다."""
        recorder = _Recorder()
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True

        coord._tick_dirty_cloud_sync()

        self.assertEqual(cloud.push_calls, 0)
        self.assertTrue(any("변경 없음, 요청 생략" in m for m in recorder.logs))

    def test_dirty_editor_uploads_on_tick(self):
        """10. cloud_dirty=true + editor면 tick에서 업로드한다."""
        recorder = _Recorder(initial_messages={1: "편집됨"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="편집됨")

        coord._tick_dirty_cloud_sync()
        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 1))

        self.assertEqual(cloud.push_call_args[0].get(1), "편집됨")

    def test_dirty_admin_uploads_on_tick(self):
        """11. admin도 업로드가 허용된다."""
        recorder = _Recorder(initial_messages={1: "관리자 편집"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "admin"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="관리자 편집")

        coord._tick_dirty_cloud_sync()
        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 1))

    def test_viewer_never_uploads(self):
        """12. approved viewer — 로컬만 저장되고 업로드는 시도하지 않는다."""
        recorder = _Recorder(initial_messages={1: "viewer 편집"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "viewer"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="viewer 편집")

        coord._tick_dirty_cloud_sync()
        time.sleep(0.3)

        self.assertEqual(cloud.push_calls, 0)
        self.assertTrue(coord.has_cloud_dirty())

    def test_pending_never_uploads(self):
        """13. pending 상태 — 업로드를 시도하지 않는다."""
        recorder = _Recorder(initial_messages={1: "대기중 편집"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("pending", "viewer"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="대기중 편집")

        coord._tick_dirty_cloud_sync()
        time.sleep(0.3)

        self.assertEqual(cloud.push_calls, 0)

    def test_blocked_never_uploads(self):
        """14. blocked 상태 — 업로드를 시도하지 않는다."""
        recorder = _Recorder(initial_messages={1: "차단된 편집"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("blocked", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="차단된 편집")

        coord._tick_dirty_cloud_sync()
        time.sleep(0.3)

        self.assertEqual(cloud.push_calls, 0)

    def test_logged_out_never_uploads(self):
        """15. 로그아웃 상태 — 업로드를 시도하지 않는다."""
        recorder = _Recorder(initial_messages={1: "로그아웃 상태"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=False)
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="로그아웃 상태")

        coord._tick_dirty_cloud_sync()
        time.sleep(0.2)

        self.assertEqual(cloud.push_calls, 0)

    def test_supabase_disabled_never_uploads(self):
        """16. SUPABASE_ENABLED=false — 업로드를 시도하지 않는다."""
        recorder = _Recorder(initial_messages={1: "비활성 상태"})
        cloud = FakeCloudSyncService(enabled=False)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="비활성 상태")

        coord._tick_dirty_cloud_sync()
        time.sleep(0.2)

        self.assertEqual(cloud.push_calls, 0)

    def test_offline_keeps_dirty_then_recovers_on_next_tick(self):
        """17/18. 오프라인이면 dirty가 유지되고, 복구 후 다음 tick에서 업로드된다."""
        recorder = _Recorder(initial_messages={1: "오프라인 편집"})
        cloud = FakeCloudSyncService(enabled=True)
        cloud.push_result_factory = lambda payload: SyncResult(False, error="오프라인", error_code="connection_error")
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="오프라인 편집")

        coord._tick_dirty_cloud_sync()
        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 1))
        self.assertTrue(coord.has_cloud_dirty(), "업로드 실패 후에도 dirty는 유지되어야 함")

        # 네트워크 복구
        cloud.push_result_factory = lambda payload: SyncResult(True, updated=list(payload.keys()))
        coord._tick_dirty_cloud_sync()
        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 2))
        self.assertTrue(self._wait_until(lambda: not coord.has_cloud_dirty()))


# ============================================================
# 명시적 이벤트 (request_push / notify_local_save / shutdown_flush)
# ============================================================

class TestExplicitEvents(Phase2ETestBase):
    def test_manual_save_pushes_immediately(self):
        """19. 저장 버튼(notify_local_save) 클릭 시 즉시 업로드를 요청한다."""
        recorder = _Recorder()
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.notify_local_save({1: "저장 버튼으로 저장"})

        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 1))
        self.assertEqual(cloud.push_call_args[0].get(1), "저장 버튼으로 저장")

    def test_request_push_with_reason_send_start(self):
        """20. 명시적 reason=send_start로 즉시 업로드 요청이 가능하다."""
        recorder = _Recorder()
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.request_push(messages={1: "발송 스냅샷"}, reason="send_start")

        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 1))
        self.assertEqual(cloud.push_call_args[0].get(1), "발송 스냅샷")

    def test_request_push_does_not_block_caller(self):
        """21. request_push()는 업로드 완료를 기다리지 않고 즉시 반환한다."""
        recorder = _Recorder()
        cloud = GatedFakeCloudSyncService(enabled=True)
        cloud.gate_enabled = True
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        start = time.time()
        coord.request_push(messages={1: "느린 업로드"}, reason="send_start")
        elapsed = time.time() - start

        self.assertLess(elapsed, 0.2, "request_push()가 블로킹되면 안 됨")
        cloud.release_event.set()  # 백그라운드 스레드 정리

    def test_shutdown_flush_pushes_when_dirty(self):
        """25. 종료 직전 cloud_dirty이면 업로드를 시도한다."""
        recorder = _Recorder(initial_messages={1: "종료 전 편집"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="종료 전 편집")

        coord.shutdown_flush(timeout_seconds=2.0)

        self.assertEqual(cloud.push_calls, 1)
        self.assertTrue(coord._closing)

    def test_shutdown_flush_times_out_without_hanging(self):
        """26. 업로드가 끝나지 않아도 지정된 시간 안에 반환한다(영구 대기 금지)."""
        recorder = _Recorder(initial_messages={1: "느린 종료 업로드"})
        cloud = GatedFakeCloudSyncService(enabled=True)
        cloud.gate_enabled = True
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="느린 종료 업로드")

        start = time.time()
        coord.shutdown_flush(timeout_seconds=0.3)
        elapsed = time.time() - start

        self.assertLess(elapsed, 1.0, "timeout_seconds 근처에서 반환해야 함")
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertTrue(any("대기시간 초과" in m for m in recorder.logs))
        cloud.release_event.set()  # 백그라운드 스레드 정리

    def test_shutdown_flush_skips_db_call_when_not_dirty(self):
        """cloud_dirty가 없으면 shutdown_flush도 DB 호출을 하지 않는다."""
        recorder = _Recorder()
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.shutdown_flush(timeout_seconds=2.0)

        self.assertEqual(cloud.push_calls, 0)

    def test_closing_blocks_further_request_push(self):
        """종료 처리 시작 후에는 추가 request_push가 아무 것도 하지 않는다."""
        recorder = _Recorder()
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._closing = True

        coord.request_push(messages={1: "종료 후 요청"}, reason="manual_save")
        time.sleep(0.2)

        self.assertEqual(cloud.push_calls, 0)


# ============================================================
# 동시성: 업로드 직렬화 (원칙 7)
# ============================================================

class TestPushSerialization(Phase2ETestBase):
    def test_edit_during_push_is_uploaded_right_after(self):
        """27/30. A 업로드 중 B로 재수정하면, A 완료 직후 최신값 B가 자동으로 업로드된다."""
        recorder = _Recorder(initial_messages={1: "A"})
        cloud = GatedFakeCloudSyncService(enabled=True)
        cloud.gate_enabled = True
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.request_push(messages={1: "A"}, reason="manual_save")
        self.assertTrue(cloud.entered_event.wait(timeout=2.0), "A 업로드가 시작되어야 함")

        # A가 아직 진행 중인 동안 사용자가 B로 재수정하고 저장 버튼을 누른 상황을 흉내낸다.
        recorder.messages[1] = "B"
        coord.request_push(messages={1: "B"}, reason="manual_save")
        self.assertEqual(cloud.calls_started, 1, "진행 중이면 새 스레드를 만들지 않아야 함")

        cloud.entered_event.clear()
        cloud.release_event.set()  # A 완료
        # A 완료 후 곧바로 B가 이어서 업로드되어야 한다(같은 워커 스레드가 루프 재시작)
        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 2))
        self.assertEqual(cloud.push_call_args[-1].get(1), "B", "최종적으로 클라우드에는 최신값 B가 남아야 함")

    def test_multiple_request_push_while_busy_creates_single_followup(self):
        """28. 업로드 중 request_push를 여러 번 호출해도 새 스레드/추가 업로드는 1개만 예약된다."""
        recorder = _Recorder(initial_messages={1: "A"})
        cloud = GatedFakeCloudSyncService(enabled=True)
        cloud.gate_enabled = True
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.request_push(messages={1: "A"}, reason="manual_save")
        self.assertTrue(cloud.entered_event.wait(timeout=2.0))

        threads_before = threading.active_count()
        for _ in range(5):
            coord.request_push(messages={1: "여러 번 호출"}, reason="manual_save")
        threads_after = threading.active_count()

        self.assertLessEqual(threads_after - threads_before, 0, "새 스레드가 추가로 생기면 안 됨")
        self.assertEqual(cloud.calls_started, 1)

        cloud.release_event.set()
        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 2))
        self.assertFalse(self._wait_until(lambda: cloud.push_calls >= 3, timeout=0.3), "후속 업로드는 정확히 1번만 실행되어야 함")

    def test_stale_push_completion_does_not_clear_newer_dirty(self):
        """29. 오래된 업로드 완료가 그 사이 생긴 최신 dirty를 false로 만들지 않는다."""
        recorder = _Recorder(initial_messages={1: "느린 A"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        # A가 서버로 전송되는 동안, 로컬은 이미 "최신 B"로 한 번 더 자동저장되었다고 가정
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="최신 B")

        # A라는 예전 스냅샷의 push가 뒤늦게 성공 응답을 받은 상황을 직접 재현한다.
        push_result = SyncResult(True, updated=[1])
        coord._mark_synced_after_push(push_result, {1: "느린 A"})

        self.assertTrue(coord._local_state[1].dirty, "최신 로컬 값과 다른 오래된 push 완료는 dirty를 해제하면 안 됨")
        self.assertEqual(coord._local_state[1].last_text, "최신 B", "로컬에 보관된 최신 텍스트가 보존되어야 함")

    def test_conflict_during_request_push_keeps_local_and_sets_conflict_status(self):
        """31. request_push 경로에서도 충돌 시 로컬 데이터를 유지하고 CONFLICT 상태로 표시한다."""
        recorder = _Recorder(initial_messages={1: "로컬 값"})
        cloud = FakeCloudSyncService(enabled=True)
        cloud.push_result_factory = lambda payload: SyncResult(True, updated=[], conflicts=[1])
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.request_push(messages={1: "로컬 값"}, reason="manual_save")
        self.assertTrue(self._wait_until(lambda: cloud.push_calls >= 1))
        self.assertTrue(self._wait_until(lambda: coord.get_status().state == CloudState.CONFLICT))

        self.assertEqual(recorder.messages.get(1), "로컬 값", "충돌 시 로컬 값을 덮어쓰지 않아야 함")


# ============================================================
# 폴링(30초) 동안 입력 진행 중이면 클라우드 값을 조용히 덮어쓰지 않는다 (원칙 8)
# ============================================================

class TestInputInProgressGating(Phase2ETestBase):
    def test_input_in_progress_defers_cloud_apply(self):
        recorder = _Recorder(initial_messages={1: "타이핑 중인 값"})
        cloud = FakeCloudSyncService(enabled=True)
        from services.cloud_sync_service import MessageRecord
        cloud.pull_result = SyncResult(
            True, messages={1: MessageRecord(1, "클라우드 값", "t", "mobile-user", 2)}
        )
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord.set_input_in_progress(True)

        coord._sync_once(is_initial=True, can_write=True, current_user_id="test-user-uuid")

        self.assertEqual(recorder.messages.get(1), "타이핑 중인 값", "입력 진행 중에는 클라우드 값으로 덮어쓰면 안 됨")
        self.assertEqual(len(recorder.applied_calls), 0)

    def test_input_not_in_progress_applies_cloud_value(self):
        """대조군: 입력 중이 아니면 기존처럼 정상적으로 클라우드 값이 반영된다."""
        recorder = _Recorder(initial_messages={1: "이전 값"})
        cloud = FakeCloudSyncService(enabled=True)
        from services.cloud_sync_service import MessageRecord
        cloud.pull_result = SyncResult(
            True, messages={1: MessageRecord(1, "클라우드 값", "t", "mobile-user", 2)}
        )
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord._sync_once(is_initial=True, can_write=True, current_user_id="test-user-uuid")

        self.assertEqual(recorder.messages.get(1), "클라우드 값")


# ============================================================
# 폴링(30초) 자체는 더 이상 dirty 메시지를 즉시 업로드하지 않는다 (원칙 5/8)
# ============================================================

class TestPollingNoLongerPushes(Phase2ETestBase):
    def test_poll_tick_does_not_push_dirty_messages(self):
        recorder = _Recorder(initial_messages={1: "폴링 중 dirty"})
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._running = True
        coord._local_state[1] = _LocalMessageState(version=1, dirty=True, last_text="폴링 중 dirty")

        coord._poll_tick()

        self.assertEqual(cloud.push_calls, 0, "30초 폴링에서는 더 이상 즉시 업로드하지 않아야 함")
        self.assertTrue(coord.has_cloud_dirty(), "dirty 상태는 15분 tick을 위해 유지되어야 함")


if __name__ == "__main__":
    unittest.main()
