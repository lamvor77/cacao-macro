# Phase 2A(클라우드 서비스 계층 기반)에 대한 회귀 테스트.
#
# Phase 2A 작업 세션에서 임시 스크립트(scratchpad)로만 검증했던 내용을 이번
# Phase 2C 작업을 계기로 저장소의 정식 테스트로 옮겼다 — 앞으로 core/scheduler.py,
# storage/data_manager.py, config/cloud_settings.py, services/supabase_client.py,
# services/cloud_sync_service.py를 건드릴 때 이 파일로 회귀를 확인할 수 있다.
#
# 실행: python -m unittest tests.test_phase2a_regression -v

import os
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestCloudConfigDefaults(unittest.TestCase):
    """config/cloud_settings.py — 환경변수 없음/비활성화 시 기존과 동일하게 동작하는지."""

    def setUp(self):
        # 단순히 os.environ.pop()으로 지우기만 하면 안 된다 — config.cloud_settings는
        # 임포트 시점에 python-dotenv의 load_dotenv()를 호출하는데, dotenv는 기본적으로
        # "이미 os.environ에 있는 키는 건드리지 않지만, 없는 키는 .env 파일 값으로
        # 채운다." 이 저장소 루트에 실제 Supabase 자격 증명이 담긴 .env 파일이 있는
        # 상태(정상적인 개발 환경)에서 importlib.reload(config.cloud_settings)가 다시
        # 일어나면(아래 test_enabled_without_url_falls_back_to_disabled 등), pop()으로
        # 지운 키가 .env 파일 값으로 도로 채워져 테스트가 실제 자격 증명에 오염된다.
        # 빈 문자열로 "설정은 되어 있지만 비어있음" 상태를 만들면(키 자체는 os.environ에
        # 존재) dotenv가 덮어쓰지 않으면서, get_cloud_config()의 파싱 로직은 빈
        # 문자열을 "미설정"과 동일하게 취급한다 — 두 요구사항을 동시에 만족한다.
        self._saved_env = {}
        for key in [
            "SUPABASE_ENABLED", "SUPABASE_URL", "SUPABASE_ANON_KEY",
            "SUPABASE_SYNC_INTERVAL_SECONDS", "SUPABASE_DEVICE_ID",
        ]:
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = ""

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_disabled(self):
        from config.cloud_settings import get_cloud_config
        cfg = get_cloud_config()
        self.assertFalse(cfg.enabled)

    def test_sync_interval_env_var_removed_from_config(self):
        """legacy messages 상시 30초 polling 제거 — CloudConfig에 더 이상
        sync_interval_seconds 필드가 없어야 한다(요구사항 5절)."""
        from config.cloud_settings import get_cloud_config
        cfg = get_cloud_config()
        self.assertFalse(hasattr(cfg, "sync_interval_seconds"))

    def test_sync_interval_env_var_set_logs_deprecation_warning(self):
        """SUPABASE_SYNC_INTERVAL_SECONDS가 여전히 .env에 남아있어도 프로그램이
        죽지 않고, 더 이상 쓰이지 않는다는 경고만 남겨야 한다."""
        os.environ["SUPABASE_SYNC_INTERVAL_SECONDS"] = "30"
        try:
            from config.cloud_settings import get_cloud_config
            with self.assertLogs("config.cloud_settings", level="WARNING") as cm:
                get_cloud_config()
            self.assertTrue(any("더 이상 사용되지 않습니다" in line for line in cm.output))
        finally:
            os.environ["SUPABASE_SYNC_INTERVAL_SECONDS"] = ""

    def test_device_id_auto_generated(self):
        from config.cloud_settings import get_cloud_config
        cfg = get_cloud_config()
        self.assertTrue(cfg.device_id)

    def test_enabled_without_url_falls_back_to_disabled(self):
        os.environ["SUPABASE_ENABLED"] = "true"
        import importlib
        import config.cloud_settings as cs
        importlib.reload(cs)
        cfg = cs.get_cloud_config()
        self.assertFalse(cfg.enabled)


class TestCloudSyncServiceGating(unittest.TestCase):
    """services/cloud_sync_service.py — 비활성화 상태에서 네트워크 시도 없이 즉시 반환하는지."""

    def test_disabled_service_never_touches_network(self):
        from config.cloud_settings import CloudConfig
        from services.cloud_sync_service import CloudSyncService

        cfg = CloudConfig(enabled=False, url="", anon_key="", device_id="pc-test")
        svc = CloudSyncService(cfg)

        self.assertFalse(svc.is_enabled())
        self.assertFalse(svc.pull_messages().success)
        self.assertFalse(svc.push_messages({1: "x"}).success)
        self.assertFalse(svc.check_connection().success)


class TestSchedulerSnapshot(unittest.TestCase):
    """core/scheduler.py — 그룹 발송 스냅샷과 notify_cloud_update() 로그 문구."""

    def test_group_send_uses_snapshot_and_cloud_notify_logs_correctly(self):
        import core.scheduler as sched_mod
        sched_mod._is_kakao_running = lambda: True

        sent_log = []
        logs = []

        class FakeSender:
            def send_message(self, room_name, text):
                sent_log.append((room_name, text))
                return True

        messages_state = {i: f"원본메시지{i}" for i in range(1, 13)}
        get_messages_calls = {"count": 0}

        def get_messages_fn():
            get_messages_calls["count"] += 1
            return dict(messages_state)

        scheduler = sched_mod.AutoScheduler(
            get_rooms_fn=lambda: {"방1": True, "방2": True},
            get_messages_fn=get_messages_fn,
            log_fn=logs.append,
        )
        scheduler._sender = FakeSender()
        scheduler._running = True

        from config.settings import MESSAGE_GROUPS

        original_send = FakeSender.send_message
        mutated = {"flag": False}

        def send_and_mutate(self, room_name, text):
            result = original_send(self, room_name, text)
            if room_name == "방1" and not mutated["flag"] and text == "원본메시지3":
                messages_state[1] = "변경된메시지1"
                scheduler.notify_cloud_update()
                mutated["flag"] = True
            return result

        FakeSender.send_message = send_and_mutate
        try:
            scheduler._send_group("A", MESSAGE_GROUPS["A"])
        finally:
            FakeSender.send_message = original_send

        self.assertEqual(get_messages_calls["count"], 1)
        room2_msgs = [t for r, t in sent_log if r == "방2"]
        self.assertEqual(room2_msgs, ["원본메시지1", "원본메시지2", "원본메시지3"])
        self.assertTrue(any("현재 그룹 종료 후 다음 발송부터 적용" in m for m in logs))
        self.assertFalse(scheduler._group_in_progress)


class TestDataManagerRoundTrip(unittest.TestCase):
    """storage/data_manager.py — 저장/불러오기 시그니처와 동작이 그대로인지."""

    def test_save_and_load_round_trip(self):
        from storage.data_manager import DataManager

        dm = DataManager()
        tmp_dir = tempfile.mkdtemp(prefix="cacao_test_")
        tmp_path = os.path.join(tmp_dir, "test_rooms.json")

        rooms = {"방A": True, "방B": False}
        messages = {i: f"메시지{i}" for i in range(1, 13)}

        dm.save(rooms, messages, tmp_path)
        loaded = dm.load(tmp_path)

        self.assertIsNotNone(loaded)
        loaded_rooms = {r["name"]: r["checked"] for r in loaded.get("rooms", [])}
        self.assertEqual(loaded_rooms, rooms)
        self.assertEqual(loaded.get("messages"), messages)


if __name__ == "__main__":
    unittest.main()
