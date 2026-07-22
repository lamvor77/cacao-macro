# config/settings.py의 IS_TEST_ENVIRONMENT 플래그 테스트
# (Test Environment Deployment & E2E Validation Sprint 1절)
#
# 다른 설정 상수(LICENSE_SECRET_KEY 등)와 동일하게 모듈 임포트 시점에 한 번만
# 계산되므로, 값을 바꿔가며 확인하려면 importlib.reload가 필요하다
# (tests/test_version.py와 동일한 패턴). 실제 .env의 다른 값은 건드리지 않는다.

import importlib
import os
import unittest

import config.settings as settings_module


def _reload_settings_module():
    return importlib.reload(settings_module)


class TestIsTestEnvironmentFlag(unittest.TestCase):
    def setUp(self):
        self._env_keys = ["APP_ENV", "SUPABASE_ENVIRONMENT"]
        self._saved = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _reload_settings_module()

    def test_no_env_vars_means_production(self):
        mod = _reload_settings_module()
        self.assertFalse(mod.IS_TEST_ENVIRONMENT)

    def test_app_env_test_enables_flag(self):
        os.environ["APP_ENV"] = "test"
        mod = _reload_settings_module()
        self.assertTrue(mod.IS_TEST_ENVIRONMENT)

    def test_supabase_environment_test_enables_flag(self):
        os.environ["SUPABASE_ENVIRONMENT"] = "test"
        mod = _reload_settings_module()
        self.assertTrue(mod.IS_TEST_ENVIRONMENT)

    def test_case_insensitive(self):
        os.environ["APP_ENV"] = "TEST"
        mod = _reload_settings_module()
        self.assertTrue(mod.IS_TEST_ENVIRONMENT)

    def test_production_value_does_not_enable_flag(self):
        os.environ["APP_ENV"] = "production"
        mod = _reload_settings_module()
        self.assertFalse(mod.IS_TEST_ENVIRONMENT)


if __name__ == "__main__":
    unittest.main()
