# scripts/check_rls_rpc_permissions.py 테스트
#
# 이 스크립트는 실제로 테스트 프로젝트에 쓰기를 수행하는 도구이므로, 가장
# 중요한 것은 "운영 프로젝트에는 절대 실행되지 않는다"는 안전장치다. 이
# 테스트는 실제 Supabase에 연결하지 않고 _guard_not_production()과
# _expect() 순수 로직만 검증한다.

import importlib.util
import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(PROJECT_ROOT, "scripts", "check_rls_rpc_permissions.py")

_spec = importlib.util.spec_from_file_location("check_rls_rpc_permissions", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["check_rls_rpc_permissions"] = _mod
_spec.loader.exec_module(_mod)


class TestProductionGuard(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: os.environ.get(k) for k in ("APP_ENV", "SUPABASE_ENVIRONMENT")
        }
        os.environ.pop("APP_ENV", None)
        os.environ.pop("SUPABASE_ENVIRONMENT", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_known_production_host_blocked_even_with_test_env(self):
        os.environ["APP_ENV"] = "test"
        with self.assertRaises(SystemExit) as ctx:
            _mod._guard_not_production("https://nojdwuoronqmvpdptvlr.supabase.co")
        self.assertEqual(ctx.exception.code, 2)

    def test_known_production_host_blocked_case_insensitive(self):
        os.environ["APP_ENV"] = "test"
        with self.assertRaises(SystemExit):
            _mod._guard_not_production("https://NOJDWUORONQMVPDPTVLR.supabase.co")

    def test_missing_test_env_flag_blocked_even_for_unknown_host(self):
        with self.assertRaises(SystemExit) as ctx:
            _mod._guard_not_production("https://some-other-project.supabase.co")
        self.assertEqual(ctx.exception.code, 2)

    def test_app_env_test_with_safe_host_passes(self):
        os.environ["APP_ENV"] = "test"
        try:
            _mod._guard_not_production("https://some-other-project.supabase.co")
        except SystemExit:
            self.fail("안전한 테스트 프로젝트 URL + APP_ENV=test 조합이 차단되면 안 됨")

    def test_supabase_environment_test_with_safe_host_passes(self):
        os.environ["SUPABASE_ENVIRONMENT"] = "test"
        try:
            _mod._guard_not_production("https://some-other-project.supabase.co")
        except SystemExit:
            self.fail("안전한 테스트 프로젝트 URL + SUPABASE_ENVIRONMENT=test 조합이 차단되면 안 됨")


class TestExpectHelper(unittest.TestCase):
    def test_should_succeed_and_does_succeed_is_pass(self):
        result = _mod._expect("role", "op", True, lambda: None)
        self.assertEqual(result.level, "PASS")

    def test_should_succeed_but_raises_is_fail(self):
        def _boom():
            raise RuntimeError("denied")
        result = _mod._expect("role", "op", True, _boom)
        self.assertEqual(result.level, "FAIL")

    def test_should_be_denied_and_raises_is_pass(self):
        def _boom():
            raise RuntimeError("denied")
        result = _mod._expect("role", "op", False, _boom)
        self.assertEqual(result.level, "PASS")

    def test_should_be_denied_but_succeeds_is_fail(self):
        result = _mod._expect("role", "op", False, lambda: None)
        self.assertEqual(result.level, "FAIL")
        self.assertIn("위험", result.detail)


if __name__ == "__main__":
    unittest.main()
