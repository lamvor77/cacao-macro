# config/version.py + core/version_check.py 테스트
#
# config/version.py의 값은 모듈 임포트 시점에 한 번 계산되므로(다른 모든
# 설정 모듈과 동일한 패턴 — config/settings.py의 LICENSE_SECRET_KEY 등과
# 동일), 환경변수를 바꾼 뒤 재확인하려면 importlib.reload가 필요하다.
# 실제 운영 환경변수(SUPABASE_* 등)는 건드리지 않는다 — CACAO_MACRO_* 전용
# 키만 monkeypatch한다.

import importlib
import os
import sys
import unittest
from unittest.mock import patch

import config.version as version_module
from core.version_check import (
    compare_versions,
    is_newer_version,
    load_version_manifest,
    parse_version_safe,
)


def _reload_version_module():
    return importlib.reload(version_module)


class TestVersionResolution(unittest.TestCase):
    """1. 버전 형식 확인 / 2. fallback 정상 동작 / 3. 환경변수 주입 시 우선 적용 / 4. 빈 값 처리"""

    def setUp(self):
        self._env_keys = [
            "CACAO_MACRO_VERSION", "CACAO_MACRO_BUILD_CHANNEL",
            "CACAO_MACRO_BUILD_DATE", "CACAO_MACRO_GIT_COMMIT",
        ]
        self._saved = {k: os.environ.get(k) for k in self._env_keys}
        for k in self._env_keys:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _reload_version_module()

    def test_dev_mode_without_env_shows_development(self):
        with patch.object(sys, "frozen", False, create=True):
            mod = _reload_version_module()
            self.assertEqual(mod.APP_VERSION, "development")
            self.assertEqual(mod.BUILD_CHANNEL, "development")

    def test_frozen_mode_without_env_uses_safe_fallback(self):
        with patch.object(sys, "frozen", True, create=True):
            mod = _reload_version_module()
            self.assertEqual(mod.APP_VERSION, "1.2.1")
            self.assertEqual(mod.BUILD_CHANNEL, "stable")

    def test_env_var_overrides_even_in_dev_mode(self):
        os.environ["CACAO_MACRO_VERSION"] = "9.9.9-test"
        with patch.object(sys, "frozen", False, create=True):
            mod = _reload_version_module()
            self.assertEqual(mod.APP_VERSION, "9.9.9-test")

    def test_env_var_overrides_frozen_fallback(self):
        os.environ["CACAO_MACRO_VERSION"] = "2.0.0"
        with patch.object(sys, "frozen", True, create=True):
            mod = _reload_version_module()
            self.assertEqual(mod.APP_VERSION, "2.0.0")

    def test_blank_env_var_treated_as_unset(self):
        os.environ["CACAO_MACRO_VERSION"] = "   "
        with patch.object(sys, "frozen", False, create=True):
            mod = _reload_version_module()
            self.assertEqual(mod.APP_VERSION, "development")

    def test_build_date_and_git_commit_empty_without_env(self):
        mod = _reload_version_module()
        self.assertEqual(mod.BUILD_DATE, "")
        self.assertEqual(mod.GIT_COMMIT, "")
        self.assertEqual(mod.git_commit_short(), "")

    def test_git_commit_short_truncates_to_seven_chars(self):
        os.environ["CACAO_MACRO_GIT_COMMIT"] = "0123456789abcdef"
        mod = _reload_version_module()
        self.assertEqual(mod.git_commit_short(), "0123456")

    def test_version_summary_format(self):
        os.environ["CACAO_MACRO_VERSION"] = "1.2.3"
        os.environ["CACAO_MACRO_BUILD_CHANNEL"] = "stable"
        mod = _reload_version_module()
        self.assertEqual(mod.version_summary(), "1.2.3 (stable)")

    def test_release_target_version_ignores_env_and_dev_fallback(self):
        # 개발 모드 + 환경변수가 설정되어 있어도(APP_VERSION이 "development"나
        # env 값이 되는 것과 무관하게) release_target_version()은 항상 고정
        # 릴리스 번호(_FALLBACK_VERSION/_FALLBACK_CHANNEL)를 반환해야 한다.
        os.environ["CACAO_MACRO_VERSION"] = "9.9.9-should-be-ignored"
        with patch.object(sys, "frozen", False, create=True):
            mod = _reload_version_module()
            version, channel = mod.release_target_version()
        self.assertEqual(version, mod._FALLBACK_VERSION)
        self.assertEqual(channel, mod._FALLBACK_CHANNEL)


class TestVersionComparison(unittest.TestCase):
    """RC와 정식 버전 비교 / 잘못된 버전 처리"""

    def test_rc_is_older_than_release(self):
        self.assertTrue(is_newer_version("2.0.0-rc.1", "2.0.0"))

    def test_release_is_newer_than_next_rc_of_same_version(self):
        # 2.0.0(정식) 다음에 나온 2.0.1-rc.1은 여전히 더 새 버전이다.
        self.assertTrue(is_newer_version("2.0.0", "2.0.1-rc.1"))

    def test_patch_ordering_is_numeric_not_lexicographic(self):
        # 문자열 비교였다면 "1.0.10" < "1.0.9"로 잘못 판정된다.
        self.assertTrue(is_newer_version("1.0.9", "1.0.10"))
        self.assertFalse(is_newer_version("1.0.10", "1.0.9"))

    def test_same_version_is_not_newer(self):
        self.assertFalse(is_newer_version("2.0.0-rc.1", "2.0.0-rc.1"))

    def test_older_candidate_is_not_newer(self):
        self.assertFalse(is_newer_version("1.0.1", "1.0.0"))

    def test_invalid_current_version_returns_false(self):
        self.assertFalse(is_newer_version("not-a-version", "1.0.0"))

    def test_invalid_candidate_version_returns_false(self):
        self.assertFalse(is_newer_version("1.0.0", "not-a-version"))

    def test_development_string_is_not_a_valid_version(self):
        self.assertFalse(is_newer_version("development", "1.0.0"))

    def test_compare_versions_ordering(self):
        self.assertEqual(compare_versions("1.0.0", "1.0.1"), -1)
        self.assertEqual(compare_versions("1.0.1", "1.0.0"), 1)
        self.assertEqual(compare_versions("1.0.0", "1.0.0"), 0)

    def test_compare_versions_invalid_input_returns_zero(self):
        self.assertEqual(compare_versions("garbage", "1.0.0"), 0)

    def test_parse_version_safe_valid(self):
        self.assertIsNotNone(parse_version_safe("2.0.0-rc.1"))

    def test_parse_version_safe_invalid_returns_none(self):
        self.assertIsNone(parse_version_safe("not.a.version!!"))

    def test_parse_version_safe_empty_string_returns_none(self):
        self.assertIsNone(parse_version_safe(""))


class TestVersionManifestLoading(unittest.TestCase):
    """release/version.json 로딩 — 실제 파일시스템 대신 tempfile만 사용한다."""

    def test_missing_manifest_returns_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_version_manifest(tmp))

    def test_valid_manifest_is_parsed(self):
        import json
        import os as _os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            release_dir = _os.path.join(tmp, "release")
            _os.makedirs(release_dir)
            with open(_os.path.join(release_dir, "version.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "version": "2.0.0-rc.1",
                    "channel": "beta",
                    "published_at": "2026-07-18T00:00:00",
                    "minimum_supported_version": "2.0.0-rc.1",
                }, f)
            info = load_version_manifest(tmp)
            self.assertIsNotNone(info)
            self.assertEqual(info.version, "2.0.0-rc.1")
            self.assertEqual(info.channel, "beta")

    def test_corrupted_manifest_returns_none(self):
        import os as _os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            release_dir = _os.path.join(tmp, "release")
            _os.makedirs(release_dir)
            with open(_os.path.join(release_dir, "version.json"), "w", encoding="utf-8") as f:
                f.write("{not valid json")
            self.assertIsNone(load_version_manifest(tmp))


if __name__ == "__main__":
    unittest.main()
