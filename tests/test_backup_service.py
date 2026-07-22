# services/backup_service.py 테스트
#
# 실제 프로젝트의 storage/backup 폴더는 절대 건드리지 않는다 — 매 테스트마다
# tempfile.TemporaryDirectory()로 독립된 가짜 storage/backup 디렉터리를 만들어
# 사용한다.

import json
import os
import tempfile
import unittest
import zipfile

from services.backup_service import (
    BackupError,
    BackupService,
    MANIFEST_FILENAME,
    MAX_AUTO_BACKUPS,
)


def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


class _TempProject:
    """storage/ + backup/ 를 갖춘 가짜 프로젝트 디렉터리. with 블록에서만 존재한다."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.storage_dir = os.path.join(self.root, "storage")
        self.cloud_sync_dir = os.path.join(self.storage_dir, "cloud_sync")
        os.makedirs(self.cloud_sync_dir)
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()

    def make_service(self, app_version="1.0.0-rc.1") -> BackupService:
        return BackupService(storage_dir_fn=lambda: self.storage_dir, app_version=app_version)


class TestCreateBackup(unittest.TestCase):
    def test_normal_backup_creation(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "1.json"), {"rooms": [], "messages": {}})
            svc = proj.make_service()
            record = svc.create_backup(backup_type="manual")
            self.assertTrue(os.path.exists(record.path))
            self.assertEqual(record.backup_type, "manual")
            self.assertEqual(record.file_count, 1)
            self.assertTrue(record.validated)

    def test_manifest_is_written_into_zip(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"x": 1})
            svc = proj.make_service()
            record = svc.create_backup()
            with zipfile.ZipFile(record.path) as zf:
                self.assertIn(MANIFEST_FILENAME, zf.namelist())
                manifest = json.loads(zf.read(MANIFEST_FILENAME))
                self.assertEqual(manifest["format_version"], 1)
                self.assertEqual(len(manifest["files"]), 1)
                self.assertIn("sha256", manifest["files"][0])

    def test_sha256_matches_source_file(self):
        with _TempProject() as proj:
            src = os.path.join(proj.storage_dir, "a.json")
            _write_json(src, {"x": 1})
            import hashlib
            with open(src, "rb") as f:
                expected = hashlib.sha256(f.read()).hexdigest()
            svc = proj.make_service()
            record = svc.create_backup()
            with zipfile.ZipFile(record.path) as zf:
                manifest = json.loads(zf.read(MANIFEST_FILENAME))
                self.assertEqual(manifest["files"][0]["sha256"], expected)

    def test_backup_types_manual_auto_pre_restore(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {})
            svc = proj.make_service()
            for t in ("manual", "auto", "pre_restore"):
                record = svc.create_backup(backup_type=t)
                self.assertEqual(record.backup_type, t)
                self.assertIn(f"backup-{t}-", record.filename)

    def test_invalid_backup_type_rejected(self):
        with _TempProject() as proj:
            svc = proj.make_service()
            with self.assertRaises(BackupError):
                svc.create_backup(backup_type="not_a_real_type")

    def test_secret_files_excluded_from_backup(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"x": 1})
            # 비밀/제외 대상 파일들을 실제로 만들어 둔다.
            with open(os.path.join(proj.cloud_sync_dir, "session.dat"), "wb") as f:
                f.write(b"fake-dpapi-encrypted-token")
            with open(os.path.join(proj.root, ".env"), "w", encoding="utf-8") as f:
                f.write("LICENSE_SECRET_KEY=super-secret\n")
            with open(os.path.join(proj.storage_dir, "a.json.tmp"), "w", encoding="utf-8") as f:
                f.write("{}")
            svc = proj.make_service()
            record = svc.create_backup()
            with zipfile.ZipFile(record.path) as zf:
                names = zf.namelist()
                for forbidden in ("storage/cloud_sync/session.dat", ".env", "storage/a.json.tmp"):
                    self.assertNotIn(forbidden, names)

    def test_allowed_cloud_sync_files_included(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.cloud_sync_dir, "messages.json"), {"1": "hello"})
            _write_json(os.path.join(proj.cloud_sync_dir, "local_sync_state.json"), {})
            svc = proj.make_service()
            record = svc.create_backup()
            with zipfile.ZipFile(record.path) as zf:
                names = zf.namelist()
                self.assertIn("storage/cloud_sync/messages.json", names)
                self.assertIn("storage/cloud_sync/local_sync_state.json", names)

    def test_uses_tempfile_style_tmp_path_then_atomic_rename(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {})
            svc = proj.make_service()
            record = svc.create_backup()
            self.assertFalse(os.path.exists(record.path + ".tmp"))

    def test_real_project_storage_untouched(self):
        """이 테스트 스위트 전체가 실제 프로젝트 storage 파일 개수를 바꾸지 않는지 확인."""
        real_storage = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage")
        before = set(os.listdir(real_storage)) if os.path.isdir(real_storage) else set()
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {})
            svc = proj.make_service()
            svc.create_backup()
        after = set(os.listdir(real_storage)) if os.path.isdir(real_storage) else set()
        self.assertEqual(before, after)


class TestValidateBackup(unittest.TestCase):
    def test_valid_backup_passes(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"x": 1})
            svc = proj.make_service()
            record = svc.create_backup()
            result = svc.validate_backup(record.path)
            self.assertTrue(result.valid)

    def test_corrupted_zip_rejected(self):
        with _TempProject() as proj:
            svc = proj.make_service()
            bad_path = os.path.join(proj.root, "backup", "backup-manual-corrupt.zip")
            os.makedirs(os.path.dirname(bad_path), exist_ok=True)
            with open(bad_path, "wb") as f:
                f.write(b"this is not a zip file at all")
            result = svc.validate_backup(bad_path)
            self.assertFalse(result.valid)

    def test_missing_file_rejected(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"x": 1})
            svc = proj.make_service()
            record = svc.create_backup()
            # zip에서 실제 데이터 파일 하나를 지워 manifest와 불일치시킨다.
            self._remove_entry_from_zip(record.path, "storage/a.json")
            result = svc.validate_backup(record.path)
            self.assertFalse(result.valid)
            self.assertIn("누락", result.reason)

    def test_tampered_manifest_rejected(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"x": 1})
            svc = proj.make_service()
            record = svc.create_backup()
            self._tamper_manifest_sha(record.path)
            result = svc.validate_backup(record.path)
            self.assertFalse(result.valid)
            self.assertIn("SHA-256", result.reason)

    def test_missing_manifest_rejected(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"x": 1})
            svc = proj.make_service()
            record = svc.create_backup()
            self._remove_entry_from_zip(record.path, MANIFEST_FILENAME)
            result = svc.validate_backup(record.path)
            self.assertFalse(result.valid)

    def test_nonexistent_path_rejected(self):
        with _TempProject() as proj:
            svc = proj.make_service()
            result = svc.validate_backup(os.path.join(proj.root, "backup", "does-not-exist.zip"))
            self.assertFalse(result.valid)

    @staticmethod
    def _remove_entry_from_zip(zip_path: str, entry_name: str) -> None:
        tmp_path = zip_path + ".rewrite"
        with zipfile.ZipFile(zip_path, "r") as src, zipfile.ZipFile(tmp_path, "w") as dst:
            for item in src.infolist():
                if item.filename == entry_name:
                    continue
                dst.writestr(item, src.read(item.filename))
        os.replace(tmp_path, zip_path)

    @staticmethod
    def _tamper_manifest_sha(zip_path: str) -> None:
        tmp_path = zip_path + ".rewrite"
        with zipfile.ZipFile(zip_path, "r") as src:
            manifest = json.loads(src.read(MANIFEST_FILENAME))
            manifest["files"][0]["sha256"] = "0" * 64
            with zipfile.ZipFile(tmp_path, "w") as dst:
                for item in src.infolist():
                    if item.filename == MANIFEST_FILENAME:
                        dst.writestr(MANIFEST_FILENAME, json.dumps(manifest))
                    else:
                        dst.writestr(item, src.read(item.filename))
        os.replace(tmp_path, zip_path)


class TestAutoBackupPolicy(unittest.TestCase):
    def test_should_create_auto_backup_when_none_exists(self):
        with _TempProject() as proj:
            svc = proj.make_service()
            self.assertTrue(svc.should_create_auto_backup_today())

    def test_should_not_create_second_auto_backup_same_day(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {})
            svc = proj.make_service()
            svc.create_backup(backup_type="auto")
            self.assertFalse(svc.should_create_auto_backup_today())

    def test_manual_backup_does_not_block_auto_backup_today(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {})
            svc = proj.make_service()
            svc.create_backup(backup_type="manual")
            self.assertTrue(svc.should_create_auto_backup_today())

    def test_cleanup_keeps_most_recent_30_auto_backups(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {})
            svc = proj.make_service()
            # 서로 다른 시각으로 인식되도록 파일명에 인덱스를 직접 반영해 34개를 만든다.
            for i in range(MAX_AUTO_BACKUPS + 4):
                record = svc.create_backup(backup_type="auto")
                # 같은 초 안에 여러 개를 만들면 파일명이 겹칠 수 있으므로 직접 rename한다.
                new_name = f"backup-auto-{i:03d}.zip"
                new_path = os.path.join(svc.get_backup_dir(), new_name)
                os.replace(record.path, new_path)
                self._rewrite_created_at(new_path, f"2026-01-{(i % 28) + 1:02d}T00:00:{i:02d}")
            deleted = svc.cleanup_old_backups()
            remaining = [r for r in svc.list_backups() if r.backup_type == "auto"]
            self.assertEqual(deleted, 4)
            self.assertEqual(len(remaining), MAX_AUTO_BACKUPS)

    @staticmethod
    def _rewrite_created_at(zip_path: str, created_at: str) -> None:
        tmp_path = zip_path + ".rewrite"
        with zipfile.ZipFile(zip_path, "r") as src:
            manifest = json.loads(src.read(MANIFEST_FILENAME))
            manifest["created_at"] = created_at
            with zipfile.ZipFile(tmp_path, "w") as dst:
                for item in src.infolist():
                    if item.filename == MANIFEST_FILENAME:
                        dst.writestr(MANIFEST_FILENAME, json.dumps(manifest))
                    else:
                        dst.writestr(item, src.read(item.filename))
        os.replace(tmp_path, zip_path)


class TestRestoreBackup(unittest.TestCase):
    def test_restore_success_replaces_data(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"value": "old"})
            svc = proj.make_service()
            record = svc.create_backup(backup_type="manual")

            # 백업 이후 데이터를 바꾼다 — 복구하면 백업 시점 값으로 되돌아가야 한다.
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"value": "new"})

            result = svc.restore_backup(record.path)
            self.assertTrue(result.success)
            with open(os.path.join(proj.storage_dir, "a.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["value"], "old")

    def test_restore_creates_pre_restore_backup_first(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"value": "v1"})
            svc = proj.make_service()
            record = svc.create_backup(backup_type="manual")
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"value": "v2"})

            result = svc.restore_backup(record.path)
            self.assertTrue(result.success)
            self.assertTrue(os.path.exists(result.pre_restore_backup_path))
            pre_restore_records = [r for r in svc.list_backups() if r.backup_type == "pre_restore"]
            self.assertEqual(len(pre_restore_records), 1)

    def test_restore_does_not_touch_supabase_or_upload(self):
        """이 서비스는 Supabase client를 아예 import하지 않는다 — 주석에서
        Supabase를 언급하는 것은 허용하되(정책 설명), 실제 import/모듈 참조가
        없다는 사실이 곧 "복구 직후 클라우드 업로드 금지"를 코드 구조로
        보장한다."""
        import ast
        import services.backup_service as module
        with open(module.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imported_names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.append(node.module)
        self.assertFalse(
            any("supabase" in name.lower() for name in imported_names),
            f"backup_service.py가 supabase 관련 모듈을 import함: {imported_names}",
        )

    def test_restore_rejects_invalid_backup_without_touching_storage(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"value": "original"})
            svc = proj.make_service()

            bad_path = os.path.join(svc.get_backup_dir(), "backup-manual-bad.zip")
            with open(bad_path, "wb") as f:
                f.write(b"not a real zip")

            result = svc.restore_backup(bad_path)
            self.assertFalse(result.success)
            with open(os.path.join(proj.storage_dir, "a.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["value"], "original")

    def test_restore_rollback_on_extraction_failure_preserves_original_data(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {"value": "keep-me"})
            svc = proj.make_service()
            record = svc.create_backup(backup_type="manual")

            # 백업 파일의 실제 데이터를 조작해 압축 해제 후 SHA-256 재검증에서
            # 실패하도록 만든다(validate_backup은 통과하지만 restore 도중
            # 두 번째 검증에서 걸리는 상황을 재현하기 위해, validate 시점과
            # restore 시점 사이에 파일이 바뀌는 경우를 시뮬레이션한다) —
            # 여기서는 대신 manifest의 file 엔트리에 존재하지 않는 파일 경로를
            # 추가해 "압축 해제 결과에 파일이 없음" 실패를 재현한다.
            self._add_phantom_manifest_entry(record.path)

            _write_json(os.path.join(proj.storage_dir, "a.json"), {"value": "changed-before-restore"})

            result = svc.restore_backup(record.path)
            self.assertFalse(result.success)
            # 원본(복구 시도 직전 값)이 보존되어야 한다.
            with open(os.path.join(proj.storage_dir, "a.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["value"], "changed-before-restore")

    @staticmethod
    def _add_phantom_manifest_entry(zip_path: str) -> None:
        tmp_path = zip_path + ".rewrite"
        with zipfile.ZipFile(zip_path, "r") as src:
            manifest = json.loads(src.read(MANIFEST_FILENAME))
            # validate_backup()은 이 파일이 zip에 실제로 있는지도 확인하므로,
            # validate 단계 자체를 통과시키기 위해 실제 파일도 함께 추가해야 한다.
            # 대신 여기서는 restore_backup 내부의 "압축 해제 후 재검증" 단계만
            # 노리기 위해, sha256을 실제 내용과 다르게 조작한 새 항목을 만든다.
            manifest["files"].append({
                "relative_path": "storage/phantom.json", "size": 2, "sha256": "f" * 64,
            })
            with zipfile.ZipFile(tmp_path, "w") as dst:
                for item in src.infolist():
                    if item.filename == MANIFEST_FILENAME:
                        dst.writestr(MANIFEST_FILENAME, json.dumps(manifest))
                    else:
                        dst.writestr(item, src.read(item.filename))
                dst.writestr("storage/phantom.json", "{}")
        os.replace(tmp_path, zip_path)


class TestDeleteBackup(unittest.TestCase):
    def test_delete_backup_removes_file(self):
        with _TempProject() as proj:
            _write_json(os.path.join(proj.storage_dir, "a.json"), {})
            svc = proj.make_service()
            record = svc.create_backup()
            self.assertTrue(svc.delete_backup(record.path))
            self.assertFalse(os.path.exists(record.path))

    def test_delete_backup_refuses_path_outside_backup_dir(self):
        with _TempProject() as proj:
            svc = proj.make_service()
            outside_path = os.path.join(proj.storage_dir, "a.json")
            _write_json(outside_path, {})
            with self.assertRaises(BackupError):
                svc.delete_backup(outside_path)
            self.assertTrue(os.path.exists(outside_path))


if __name__ == "__main__":
    unittest.main()
