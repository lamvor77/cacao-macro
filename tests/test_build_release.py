# scripts/build_release.py 테스트
#
# 실제 프로젝트의 dist/release 폴더는 절대 건드리지 않는다 — 매 테스트마다
# tempfile로 가짜 프로젝트 루트를 만들어 그 안에서만 패키징을 수행한다.
# 실제 exe를 빌드하지 않으므로 "cacao_macro.exe"는 더미 바이트로 대체한다
# (SHA-256/파일 존재 검증 로직 자체를 검증하는 것이 목적이며, PyInstaller
# 실행 여부와는 무관하다).

import json
import os
import tempfile
import unittest

from scripts.build_release import build_release, scan_for_forbidden_content, sha256_of_file


def _make_fake_project(root: str, include_license: bool = True) -> None:
    os.makedirs(os.path.join(root, "dist"), exist_ok=True)
    with open(os.path.join(root, "dist", "cacao_macro.exe"), "wb") as f:
        f.write(b"fake-exe-bytes-for-testing")

    with open(os.path.join(root, ".env.example"), "w", encoding="utf-8") as f:
        f.write("SUPABASE_ENABLED=false\n")

    if include_license:
        with open(os.path.join(root, "license_build.json"), "w", encoding="utf-8") as f:
            json.dump({"start_date": "2026-01-01", "end_date": "2027-01-01", "signature": "x"}, f)

    os.makedirs(os.path.join(root, "docs"), exist_ok=True)
    with open(os.path.join(root, "docs", "release_readme_ko.txt"), "w", encoding="utf-8") as f:
        f.write("사용 안내\n")

    with open(os.path.join(root, "INSTALL.md"), "w", encoding="utf-8") as f:
        f.write("# 설치 안내\n")


class TestBuildRelease(unittest.TestCase):
    def test_missing_exe_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                build_release(tmp)

    def test_manifest_required_fields_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_project(tmp)
            result = build_release(tmp)
            manifest_path = os.path.join(result["package_dir"], "release_manifest.json")
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
            for key in (
                "app_name", "version", "channel", "built_at", "git_commit",
                "files", "required_external_files", "license_external",
                "backup_format_version",
            ):
                self.assertIn(key, manifest, f"release_manifest.json에 {key} 필드 누락")
            self.assertEqual(manifest["files"][0]["name"], "cacao_macro.exe")
            self.assertIn("sha256", manifest["files"][0])

    def test_checksums_match_actual_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_project(tmp)
            result = build_release(tmp)
            checksum_path = os.path.join(result["package_dir"], "checksums.sha256")
            with open(checksum_path, encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
            for line in lines:
                sha, name = line.split("  ", 1)
                actual = sha256_of_file(os.path.join(result["package_dir"], name))
                self.assertEqual(sha, actual, f"{name}의 checksum이 실제 파일과 다름")

    def test_no_real_env_in_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_project(tmp)
            # 실수로 실제 .env가 프로젝트 루트에 있어도 패키지에는 들어가면 안 된다.
            with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                f.write("LICENSE_SECRET_KEY=real-secret-value\n")
            result = build_release(tmp)
            self.assertEqual(result["problems"], [])
            self.assertFalse(os.path.exists(os.path.join(result["package_dir"], ".env")))
            self.assertTrue(os.path.exists(os.path.join(result["package_dir"], ".env.example")))

    def test_secret_and_token_files_not_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_project(tmp)
            result = build_release(tmp)
            names = os.listdir(result["package_dir"])
            for forbidden in (".env", "token.json", "credentials.json", "session.dat"):
                self.assertNotIn(forbidden, names)

    def test_exe_and_license_present_in_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_project(tmp, include_license=True)
            result = build_release(tmp)
            self.assertTrue(os.path.exists(os.path.join(result["package_dir"], "cacao_macro.exe")))
            self.assertTrue(os.path.exists(os.path.join(result["package_dir"], "license_build.json")))
            self.assertTrue(result["license_included"])

    def test_missing_license_is_reported_not_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_project(tmp, include_license=False)
            result = build_release(tmp)
            self.assertFalse(result["license_included"])
            self.assertFalse(os.path.exists(os.path.join(result["package_dir"], "license_build.json")))

    def test_nested_release_version_json_written(self):
        """core/version_check.py의 load_version_manifest()가 읽는
        release/version.json이 패키지 내부(중첩)에 실제로 생성되는지 확인."""
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_project(tmp)
            result = build_release(tmp)
            from core.version_check import load_version_manifest
            info = load_version_manifest(result["package_dir"])
            self.assertIsNotNone(info)
            self.assertEqual(info.version, result["version"])

    def test_scan_for_forbidden_content_detects_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as f:
                f.write("SECRET=x\n")
            problems = scan_for_forbidden_content(tmp)
            self.assertIn(".env", problems)

    def test_scan_for_forbidden_content_ignores_forbidden_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = os.path.join(tmp, "logs")
            os.makedirs(log_dir)
            with open(os.path.join(log_dir, "2026-07-18.log"), "w", encoding="utf-8") as f:
                f.write("log line\n")
            problems = scan_for_forbidden_content(tmp)
            self.assertEqual(problems, [])

    def test_real_project_dist_and_release_untouched(self):
        """이 테스트 스위트가 실제 프로젝트의 dist/release 폴더 내용을 바꾸지 않는지 확인."""
        real_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        real_release = os.path.join(real_root, "release")
        before = set(os.listdir(real_release)) if os.path.isdir(real_release) else None
        with tempfile.TemporaryDirectory() as tmp:
            _make_fake_project(tmp)
            build_release(tmp)
        after = set(os.listdir(real_release)) if os.path.isdir(real_release) else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
