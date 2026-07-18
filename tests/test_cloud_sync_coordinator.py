# CloudSyncCoordinator 테스트
#
# 실제 Supabase에는 절대 연결하지 않는다 — CloudSyncService/AuthService를
# 흉내내는 fake 객체만 사용한다 (CloudSyncCoordinator가 이 두 서비스의 "공개
# 인터페이스"에만 의존하도록 설계되어 있어서 가능하다).
#
# 실행: python -m unittest tests.test_cloud_sync_coordinator -v
#       (프로젝트 루트에서 실행)

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
from services.cloud_state import CloudState, CloudStatusInfo
from services.cloud_sync_coordinator import CloudSyncCoordinator, _LocalMessageState
from services.cloud_sync_service import MessageRecord, SyncResult
from storage.data_manager import DataManager


# ============================================================
# 테스트 더블 (fake) — 실제 네트워크/파일시스템(storage/cloud_sync 제외) 없음
# ============================================================

class FakeCloudSyncService:
    """CloudSyncService의 공개 인터페이스(is_enabled/pull_messages/push_messages/
    check_connection/get_sync_status)만 흉내낸다."""

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self.pull_result = SyncResult(True, messages={})
        # 기본 push 동작: 넘어온 메시지 전부 성공 처리
        self.push_result_factory = lambda payload: SyncResult(True, updated=list(payload.keys()))
        self.pull_calls = 0
        self.push_calls = 0
        self.push_call_args: list[dict] = []

    def is_enabled(self) -> bool:
        return self._enabled

    def check_connection(self) -> SyncResult:
        return SyncResult(True)

    def pull_messages(self) -> SyncResult:
        self.pull_calls += 1
        return self.pull_result

    def push_messages(self, messages: dict, updated_by=None, device_id=None) -> SyncResult:
        self.push_calls += 1
        self.push_call_args.append(dict(messages))
        return self.push_result_factory(messages)

    def get_sync_status(self):
        return None


class _FakeSession:
    user_id = "test-user-uuid"


def _default_profile() -> AppUserProfile:
    """기존(Phase 2C) 테스트가 별도 설정 없이도 그대로 통과하도록, 로그인되어
    있으면 기본적으로 "approved editor"(쓰기 가능)인 것으로 취급한다."""
    return AppUserProfile(id="test-user-uuid", email="test@example.com", status="approved", role="editor")


class FakeAuthService:
    """AuthService의 공개 인터페이스(is_logged_in/get_session/get_app_user_profile)를 흉내낸다."""

    def __init__(self, logged_in: bool = True, profile: AppUserProfile | None = None):
        self._logged_in = logged_in
        self.profile = profile if profile is not None else _default_profile()
        self.profile_calls = 0

    def is_logged_in(self) -> bool:
        return self._logged_in

    def get_session(self):
        return _FakeSession() if self._logged_in else None

    def get_app_user_profile(self):
        self.profile_calls += 1
        return self.profile if self._logged_in else None


class _Recorder:
    """콜백 호출을 기록하는 헬퍼 (get_messages_fn/apply_messages_fn/log_fn/status_fn 대체용)."""

    def __init__(self, initial_messages: dict | None = None):
        self.messages: dict[int, str] = dict(initial_messages or {})
        self.applied_calls: list[dict] = []
        self.logs: list[str] = []
        self.statuses: list[CloudStatusInfo] = []
        self.notify_scheduler_calls = 0

    def get_messages(self) -> dict:
        return dict(self.messages)

    def apply_messages(self, messages: dict) -> None:
        self.applied_calls.append(dict(messages))
        self.messages.update(messages)

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    def status(self, info: CloudStatusInfo) -> None:
        self.statuses.append(info)

    def notify_scheduler(self) -> None:
        self.notify_scheduler_calls += 1


class CoordinatorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp_state_dir = tempfile.mkdtemp(prefix="cacao_cloud_state_")
        self._data_manager = DataManager()  # 실제 storage/ 사용하지만 파일 경로는 매번 명시적으로 지정함

    def tearDown(self):
        shutil.rmtree(self._tmp_state_dir, ignore_errors=True)

    def make_coordinator(
        self,
        recorder: _Recorder,
        cloud: FakeCloudSyncService,
        auth: FakeAuthService,
        poll_interval: float = 0.05,
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
        )


# ============================================================
# 1~7: 초기 동기화 정책(_reconcile) + 로컬/업로드/오프라인/실패 시 로컬 유지
# ============================================================

class TestReconcilePolicy(CoordinatorTestBase):
    def test_1_local_only_uploads(self):
        """1. 로컬만 존재할 때 클라우드 업로드"""
        recorder = _Recorder(initial_messages={1: "로컬 전용 메시지"})
        cloud = FakeCloudSyncService()
        cloud.pull_result = SyncResult(True, messages={})  # 클라우드에는 아무 것도 없음
        auth = FakeAuthService(logged_in=True)
        coord = self.make_coordinator(recorder, cloud, auth)

        coord._sync_once(is_initial=True, can_write=True, current_user_id="test-user-uuid")

        self.assertEqual(cloud.push_calls, 1)
        self.assertIn(1, cloud.push_call_args[0])
        self.assertEqual(cloud.push_call_args[0][1], "로컬 전용 메시지")

    def test_2_cloud_only_applies_to_ui_and_local(self):
        """2. 클라우드만 존재할 때 UI/로컬 반영"""
        recorder = _Recorder(initial_messages={})
        cloud = FakeCloudSyncService()
        cloud.pull_result = SyncResult(
            True,
            messages={1: MessageRecord(1, "클라우드 메시지", "2026-01-01T00:00:00Z", "mobile-user", 1)},
        )
        auth = FakeAuthService(logged_in=True)
        coord = self.make_coordinator(recorder, cloud, auth)

        coord._sync_once(is_initial=True, can_write=True, current_user_id="test-user-uuid")

        self.assertEqual(recorder.messages.get(1), "클라우드 메시지")
        self.assertEqual(len(recorder.applied_calls), 1)
        # 로컬 JSON에도 반영되었는지 확인
        saved = self._data_manager.load(coord._local_file)
        self.assertEqual(saved["messages"].get(1), "클라우드 메시지")

    def test_3_cloud_newer_applies(self):
        """3. 클라우드가 최신일 때 클라우드 적용 (로컬 dirty 아님)"""
        recorder = _Recorder(initial_messages={1: "이전 버전"})
        cloud = FakeCloudSyncService()
        cloud.pull_result = SyncResult(
            True, messages={1: MessageRecord(1, "새 버전", "t", "mobile-user", 3)}
        )
        auth = FakeAuthService(logged_in=True)
        coord = self.make_coordinator(recorder, cloud, auth)
        coord._local_state[1] = _LocalMessageState(version=2, dirty=False, last_text="이전 버전")

        coord._sync_once(is_initial=True, can_write=True, current_user_id="test-user-uuid")

        self.assertEqual(recorder.messages.get(1), "새 버전")
        self.assertEqual(cloud.push_calls, 0)

    def test_4_local_dirty_and_uptodate_uploads(self):
        """4. 로컬이 최신(dirty)일 때 업로드"""
        recorder = _Recorder(initial_messages={1: "로컬에서 수정함"})
        cloud = FakeCloudSyncService()
        cloud.pull_result = SyncResult(
            True, messages={1: MessageRecord(1, "예전 클라우드 값", "t", "someone", 2)}
        )
        auth = FakeAuthService(logged_in=True)
        coord = self.make_coordinator(recorder, cloud, auth)
        # 로컬은 버전 2를 마지막으로 확인했고, 그 뒤 로컬에서 수정함(dirty) — 클라우드는 그대로 버전 2
        coord._local_state[1] = _LocalMessageState(version=2, dirty=True, last_text="로컬에서 수정함")

        coord._sync_once(is_initial=True, can_write=True, current_user_id="test-user-uuid")

        self.assertEqual(cloud.push_calls, 1)
        self.assertEqual(cloud.push_call_args[0][1], "로컬에서 수정함")
        self.assertEqual(len(recorder.applied_calls), 0)  # 클라우드 값으로 덮어쓰지 않음

    def test_5_both_changed_is_conflict(self):
        """5. 양쪽 모두 변경 → conflict, 어느 쪽도 자동 덮어쓰지 않음"""
        recorder = _Recorder(initial_messages={1: "로컬 편집본"})
        cloud = FakeCloudSyncService()
        cloud.pull_result = SyncResult(
            True, messages={1: MessageRecord(1, "모바일 편집본", "t", "mobile-user", 3)}
        )
        auth = FakeAuthService(logged_in=True)
        coord = self.make_coordinator(recorder, cloud, auth)
        # 로컬은 버전 2를 기준으로 편집(dirty)했는데, 클라우드는 그 사이 버전 3으로 더 나아감
        coord._local_state[1] = _LocalMessageState(version=2, dirty=True, last_text="로컬 편집본")

        coord._sync_once(is_initial=True, can_write=True, current_user_id="test-user-uuid")

        self.assertEqual(recorder.messages.get(1), "로컬 편집본")  # 로컬 값 유지(조용히 사라지지 않음)
        self.assertEqual(cloud.push_calls, 0)  # 클라우드 값도 덮어쓰지 않음(push 시도 안 함)
        self.assertEqual(coord.get_status().state, CloudState.CONFLICT)

    def test_6_offline_local_save_still_succeeds(self):
        """6. 오프라인일 때도 로컬 저장은 성공한다"""
        recorder = _Recorder()
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=True)
        coord = self.make_coordinator(recorder, cloud, auth)

        # notify_local_save는 클라우드 상태와 무관하게 로컬 저장을 먼저 동기적으로 수행한다.
        coord.notify_local_save({1: "오프라인 중 작성한 메시지"})

        saved = self._data_manager.load(coord._local_file)
        self.assertEqual(saved["messages"].get(1), "오프라인 중 작성한 메시지")

    def test_7_upload_failure_keeps_local_data(self):
        """7. 클라우드 업로드 실패 시 로컬 데이터는 유지된다"""
        recorder = _Recorder()
        cloud = FakeCloudSyncService()
        cloud.push_result_factory = lambda payload: SyncResult(
            False, error="네트워크 오류", error_code="connection_error"
        )
        auth = FakeAuthService(logged_in=True)
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.notify_local_save({1: "저장은 되어야 함"})
        # notify_local_save가 백그라운드 스레드로 push를 시도하므로 잠깐 대기
        time.sleep(0.3)

        saved = self._data_manager.load(coord._local_file)
        self.assertEqual(saved["messages"].get(1), "저장은 되어야 함")
        self.assertEqual(coord.get_status().state, CloudState.OFFLINE)


# ============================================================
# 8~12: 동시성/루프방지/스케줄러 연동/시작속도/종료
# ============================================================

class TestConcurrencyAndLifecycle(CoordinatorTestBase):
    def test_8_polling_skips_when_busy(self):
        """8. 이전 polling이 끝나지 않았으면 중복 실행하지 않는다"""
        recorder = _Recorder()
        cloud = FakeCloudSyncService()
        auth = FakeAuthService(logged_in=True)
        coord = self.make_coordinator(recorder, cloud, auth)

        coord._running = True  # _poll_tick()은 start()로 진입했을 때를 가정하므로 직접 설정
        coord._sync_busy.acquire()  # 이미 동기화 중인 상태를 흉내냄
        try:
            coord._poll_tick()
        finally:
            coord._sync_busy.release()

        self.assertEqual(cloud.pull_calls, 0)
        self.assertTrue(any("건너뜀" in m for m in recorder.logs))

    def test_9_applying_remote_prevents_save_loop(self):
        """9. 클라우드 값을 UI에 반영하는 동안 저장 콜백이 다시 업로드를 시도하지 않는다"""
        recorder = _Recorder(initial_messages={1: "구버전"})
        cloud = FakeCloudSyncService()
        auth = FakeAuthService(logged_in=True)

        # apply_messages_fn이 (가상의 자동저장 훅처럼) 즉시 notify_local_save를 재호출한다고 가정.
        # coord2는 아래에서 정의되지만, 이 클로저는 실제로 호출되는 시점(_apply_to_ui_and_local
        # 내부에서 apply_messages_fn이 실행될 때)에만 coord2를 조회하므로 문제 없다.
        def apply_and_try_reentrant_save(messages: dict) -> None:
            recorder.apply_messages(messages)
            coord2.notify_local_save(recorder.get_messages())

        coord2 = CloudSyncCoordinator(
            get_messages_fn=recorder.get_messages,
            apply_messages_fn=apply_and_try_reentrant_save,
            log_fn=recorder.log,
            status_fn=recorder.status,
            notify_scheduler_fn=recorder.notify_scheduler,
            data_manager=self._data_manager,
            cloud_service=cloud,
            auth_service=auth,
            poll_interval_seconds=0.05,
            state_dir=self._tmp_state_dir,
        )

        cloud_records = {1: MessageRecord(1, "새버전", "t", "mobile-user", 2)}
        coord2._apply_to_ui_and_local({1: "새버전"}, cloud_records)

        # apply 도중 재진입한 notify_local_save가 push를 트리거하지 않아야 한다
        self.assertEqual(cloud.push_calls, 0)

    def test_10_scheduler_snapshot_preserved_during_group_send(self):
        """10. 발송 중 cloud update가 와도 기존 scheduler snapshot은 유지된다 (연동 확인)"""
        import core.scheduler as sched_mod

        sched_mod._is_kakao_running = lambda: True

        class FakeSender:
            def __init__(self):
                self.sent = []

            def send_message(self, room_name, text):
                self.sent.append((room_name, text))
                return True

        messages_state = {i: f"메시지{i}" for i in range(1, 13)}
        scheduler = sched_mod.AutoScheduler(
            get_rooms_fn=lambda: {"방1": True},
            get_messages_fn=lambda: dict(messages_state),
            log_fn=lambda msg: recorder_logs.append(msg),
        )
        recorder_logs: list[str] = []
        scheduler._sender = FakeSender()
        scheduler._running = True
        scheduler._group_in_progress = True  # 그룹 발송 중을 흉내냄

        recorder = _Recorder(initial_messages=dict(messages_state))
        cloud = FakeCloudSyncService()
        auth = FakeAuthService(logged_in=True)
        coord = CloudSyncCoordinator(
            get_messages_fn=recorder.get_messages,
            apply_messages_fn=recorder.apply_messages,
            log_fn=recorder.log,
            status_fn=recorder.status,
            notify_scheduler_fn=scheduler.notify_cloud_update,  # 실제 스케줄러 메서드 연결
            data_manager=self._data_manager,
            cloud_service=cloud,
            auth_service=auth,
            poll_interval_seconds=0.05,
            state_dir=self._tmp_state_dir,
        )

        coord._apply_to_ui_and_local({1: "발송 중 도착한 새 메시지"}, {1: MessageRecord(1, "발송 중 도착한 새 메시지", "t", "u", 2)})

        self.assertEqual(scheduler._group_in_progress, True)  # 그룹 발송 상태 자체는 coordinator가 건드리지 않음
        self.assertTrue(any("현재 그룹 종료 후 다음 발송부터 적용" in m for m in recorder_logs))

    def test_11_start_returns_immediately_even_if_cloud_is_slow(self):
        """11. 프로그램 시작 시 클라우드가 느려도 UI가 즉시 열린다 (start()가 블로킹하지 않음)"""
        recorder = _Recorder()
        cloud = FakeCloudSyncService()
        original_pull = cloud.pull_messages

        def slow_pull():
            time.sleep(0.3)
            return original_pull()

        cloud.pull_messages = slow_pull
        auth = FakeAuthService(logged_in=True)
        coord = self.make_coordinator(recorder, cloud, auth, poll_interval=5)

        start_time = time.time()
        coord.start()
        elapsed = time.time() - start_time

        self.assertLess(elapsed, 0.1, f"start()가 블로킹됨 (elapsed={elapsed:.3f}s)")
        coord.stop(wait_seconds=1.0)

    def test_12_stop_cleans_up_thread(self):
        """12. 종료 시 폴링 스레드가 안전하게 정리된다"""
        recorder = _Recorder()
        cloud = FakeCloudSyncService()
        auth = FakeAuthService(logged_in=True)
        coord = self.make_coordinator(recorder, cloud, auth, poll_interval=0.05)

        coord.start()
        time.sleep(0.05)
        self.assertTrue(coord._thread.is_alive())

        coord.stop(wait_seconds=2.0)

        self.assertFalse(coord._thread.is_alive())
        self.assertFalse(coord._running)


# ============================================================
# 게이트 동작: 미설정/로그인 필요 시 네트워크 시도 자체를 하지 않는지
# ============================================================

class TestGating(CoordinatorTestBase):
    def test_not_configured_skips_network(self):
        recorder = _Recorder()
        cloud = FakeCloudSyncService(enabled=False)
        auth = FakeAuthService(logged_in=False)
        coord = self.make_coordinator(recorder, cloud, auth)

        coord._initial_sync()

        self.assertEqual(cloud.pull_calls, 0)
        self.assertEqual(coord.get_status().state, CloudState.NOT_CONFIGURED)

    def test_login_required_skips_network(self):
        recorder = _Recorder()
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=False)
        coord = self.make_coordinator(recorder, cloud, auth)

        coord._initial_sync()

        self.assertEqual(cloud.pull_calls, 0)
        self.assertEqual(coord.get_status().state, CloudState.LOGIN_REQUIRED)

    def test_notify_local_save_skips_push_when_not_logged_in(self):
        recorder = _Recorder()
        cloud = FakeCloudSyncService(enabled=True)
        auth = FakeAuthService(logged_in=False)
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.notify_local_save({1: "로그인 안 된 상태에서 저장"})
        time.sleep(0.2)

        self.assertEqual(cloud.push_calls, 0)
        saved = self._data_manager.load(coord._local_file)
        self.assertEqual(saved["messages"].get(1), "로그인 안 된 상태에서 저장")


# ============================================================
# Phase 2D: app_users 승인 상태/역할별 동작 (11~17)
# ============================================================

def _profile(status: str, role: str) -> AppUserProfile:
    return AppUserProfile(id="test-user-uuid", email="test@example.com", status=status, role=role)


class TestAppUserGating(CoordinatorTestBase):
    def test_11_pending_never_attempts_pull(self):
        """11. pending 상태 — 로그인은 됐지만 메시지 pull/push를 시도하지 않는다."""
        recorder = _Recorder()
        cloud = FakeCloudSyncService()
        auth = FakeAuthService(logged_in=True, profile=_profile("pending", "viewer"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord._initial_sync()

        self.assertEqual(cloud.pull_calls, 0)
        self.assertEqual(coord.get_status().state, CloudState.APPROVAL_PENDING)

    def test_12_blocked_never_attempts_pull(self):
        """12. blocked 상태 — 메시지 pull/push를 시도하지 않는다."""
        recorder = _Recorder()
        cloud = FakeCloudSyncService()
        auth = FakeAuthService(logged_in=True, profile=_profile("blocked", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord._initial_sync()

        self.assertEqual(cloud.pull_calls, 0)
        self.assertEqual(coord.get_status().state, CloudState.BLOCKED)

    def test_13_approved_viewer_reads_but_never_pushes(self):
        """13. approved viewer — pull(읽기)은 되지만 로컬이 dirty여도 업로드는 하지 않는다."""
        recorder = _Recorder(initial_messages={1: "viewer가 로컬에서 고친 값"})
        cloud = FakeCloudSyncService()
        cloud.pull_result = SyncResult(
            True, messages={1: MessageRecord(1, "클라우드 값", "t", "someone", 5)}
        )
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "viewer"))
        coord = self.make_coordinator(recorder, cloud, auth)
        # dirty=True로 만들어 "로컬이 최신"인 것처럼 보이게 해도 viewer는 업로드하면 안 됨
        coord._local_state[1] = _LocalMessageState(version=5, dirty=True, last_text="viewer가 로컬에서 고친 값")

        coord._initial_sync()

        self.assertEqual(cloud.pull_calls, 1, "viewer도 조회는 가능해야 함")
        self.assertEqual(cloud.push_calls, 0, "viewer는 업로드를 시도하면 안 됨")
        self.assertEqual(coord.get_status().state, CloudState.CONNECTED_READ_ONLY)

    def test_14_approved_editor_can_push(self):
        """14. approved editor — 업로드가 허용된다."""
        recorder = _Recorder(initial_messages={1: "editor가 수정"})
        cloud = FakeCloudSyncService()
        cloud.pull_result = SyncResult(True, messages={})
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord._initial_sync()

        self.assertEqual(cloud.push_calls, 1)
        self.assertEqual(coord.get_status().state, CloudState.CONNECTED)

    def test_15_approved_admin_can_push(self):
        """15. approved admin — 업로드가 허용된다."""
        recorder = _Recorder(initial_messages={1: "admin이 수정"})
        cloud = FakeCloudSyncService()
        cloud.pull_result = SyncResult(True, messages={})
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "admin"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord._initial_sync()

        self.assertEqual(cloud.push_calls, 1)
        self.assertEqual(coord.get_status().state, CloudState.CONNECTED)

    def test_16_on_login_success_restarts_initial_sync(self):
        """16. 로그인 성공 후 coordinator가 즉시 초기 동기화를 재개한다."""
        recorder = _Recorder()
        cloud = FakeCloudSyncService()
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.on_login_success()
        for _ in range(50):
            if cloud.pull_calls > 0:
                break
            time.sleep(0.05)

        self.assertGreaterEqual(cloud.pull_calls, 1)
        self.assertIn(CloudState.CONNECTED, [s.state for s in recorder.statuses])

    def test_17_on_logout_sets_login_required_and_stops_writes(self):
        """17. 로그아웃 후 상태가 즉시 로그인 필요로 바뀌고, 이후 저장은 업로드를 시도하지 않는다."""
        recorder = _Recorder()
        cloud = FakeCloudSyncService()
        auth = FakeAuthService(logged_in=True, profile=_profile("approved", "editor"))
        coord = self.make_coordinator(recorder, cloud, auth)

        coord.on_logout()
        self.assertEqual(coord.get_status().state, CloudState.LOGIN_REQUIRED)

        # 실제 로그아웃 상황을 흉내내기 위해 fake의 로그인 상태도 False로 바꾼다
        # (실제 AuthService.logout()이 세션 파일을 지우면 is_logged_in()이 False가 되는 것과 동일)
        auth._logged_in = False
        coord.notify_local_save({1: "로그아웃 후 로컬 저장"})
        time.sleep(0.2)

        self.assertEqual(cloud.push_calls, 0)
        saved = self._data_manager.load(coord._local_file)
        self.assertEqual(saved["messages"].get(1), "로그아웃 후 로컬 저장", "로그아웃 후에도 로컬 저장은 되어야 함")


if __name__ == "__main__":
    unittest.main()
