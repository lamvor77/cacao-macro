# scripts/setup_test_runtime.py 테스트 — 실제 dist/를 건드리지 않도록
# PROJECT_ROOT를 임시 디렉터리로 monkeypatch해서 검증한다.

import importlib.util
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(PROJECT_ROOT, "scripts", "setup_test_runtime.py")

_spec = importlib.util.spec_from_file_location("setup_test_runtime", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["setup_test_runtime"] = _mod
_spec.loader.exec_module(_mod)


class TestSetupTestRuntime(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_root = _mod.PROJECT_ROOT
        _mod.PROJECT_ROOT = self._tmp.name

    def tearDown(self):
        _mod.PROJECT_ROOT = self._orig_root
        self._tmp.cleanup()

    def test_missing_dist_exe_returns_error(self):
        target = os.path.join(self._tmp.name, "test-runtime")
        code = _mod.setup(target, force=False)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(target) and os.path.exists(os.path.join(target, "cacao_macro.exe")))

    def test_existing_target_without_force_returns_error(self):
        dist_dir = os.path.join(self._tmp.name, "dist")
        os.makedirs(dist_dir, exist_ok=True)
        with open(os.path.join(dist_dir, "cacao_macro.exe"), "wb") as f:
            f.write(b"fake")
        with open(os.path.join(self._tmp.name, ".env.example"), "w", encoding="utf-8") as f:
            f.write("SUPABASE_ENABLED=false\n")

        target = os.path.join(self._tmp.name, "test-runtime")
        os.makedirs(target)

        code = _mod.setup(target, force=False)
        self.assertEqual(code, 1)

    def test_successful_setup_copies_files_and_creates_subdirs(self):
        dist_dir = os.path.join(self._tmp.name, "dist")
        os.makedirs(dist_dir, exist_ok=True)
        with open(os.path.join(dist_dir, "cacao_macro.exe"), "wb") as f:
            f.write(b"fake-exe")
        with open(os.path.join(dist_dir, "license_build.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        with open(os.path.join(self._tmp.name, ".env.example"), "w", encoding="utf-8") as f:
            f.write("SUPABASE_ENABLED=false\n")

        target = os.path.join(self._tmp.name, "test-runtime")
        code = _mod.setup(target, force=False)

        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(target, "cacao_macro.exe")))
        self.assertTrue(os.path.exists(os.path.join(target, "license_build.json")))
        self.assertTrue(os.path.exists(os.path.join(target, ".env.example")))
        self.assertTrue(os.path.isdir(os.path.join(target, "storage")))
        self.assertTrue(os.path.isdir(os.path.join(target, "logs")))
        # 운영 .env를 대신 만들어주지 않는다 — 값 채우기는 사용자 몫이다.
        self.assertFalse(os.path.exists(os.path.join(target, ".env")))

    def test_missing_license_file_still_succeeds(self):
        dist_dir = os.path.join(self._tmp.name, "dist")
        os.makedirs(dist_dir, exist_ok=True)
        with open(os.path.join(dist_dir, "cacao_macro.exe"), "wb") as f:
            f.write(b"fake-exe")
        with open(os.path.join(self._tmp.name, ".env.example"), "w", encoding="utf-8") as f:
            f.write("SUPABASE_ENABLED=false\n")

        target = os.path.join(self._tmp.name, "test-runtime")
        code = _mod.setup(target, force=False)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(target, "license_build.json")))


if __name__ == "__main__":
    unittest.main()
