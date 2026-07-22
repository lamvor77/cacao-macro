# config/cloud_settings.py — SUPABASE_REALTIME_ENABLED 파싱 테스트
# (Mobile 실시간 동기화 스프린트에서 추가된 독립 스위치)

import os
import unittest

from config.cloud_settings import get_cloud_config


class TestRealtimeEnabledConfig(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: os.environ.get(k)
            for k in ("SUPABASE_ENABLED", "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_REALTIME_ENABLED")
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_defaults_to_true_when_unset(self):
        os.environ.pop("SUPABASE_REALTIME_ENABLED", None)
        config = get_cloud_config()
        self.assertTrue(config.realtime_enabled)

    def test_explicit_false_is_respected(self):
        os.environ["SUPABASE_ENABLED"] = "true"
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        os.environ["SUPABASE_ANON_KEY"] = "fake-key"
        os.environ["SUPABASE_REALTIME_ENABLED"] = "false"
        config = get_cloud_config()
        self.assertFalse(config.realtime_enabled)
        self.assertTrue(config.enabled, "SUPABASE_ENABLED 자체는 realtime과 독립적으로 유지되어야 함")

    def test_explicit_true_is_respected(self):
        os.environ["SUPABASE_REALTIME_ENABLED"] = "true"
        config = get_cloud_config()
        self.assertTrue(config.realtime_enabled)


if __name__ == "__main__":
    unittest.main()
