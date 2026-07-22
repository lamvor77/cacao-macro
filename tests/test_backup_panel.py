# gui/panels/backup_panel.py + gui/panels/diagnostics_panel.py 테스트
#
# 이 프로젝트의 기존 관례(tests/test_operations_admin_panel.py 참고)를 따라:
# 판단 로직(can_restore_or_delete)은 Tk 없이 순수 함수로 검증하고, 위젯
# 생성/렌더링/비동기 순서 자체는 실제 mainloop() 아래에서 최소 1개의 통합
# 스모크 테스트로만 확인한다. 실제 Supabase/storage에는 접근하지 않는다
# (fake 서비스만 주입).

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from services.auth_service import AppUserProfile
from services.backup_service import BackupRecord
from gui.panels.backup_panel import can_restore_or_delete


def _profile(status="approved", role="viewer"):
    return AppUserProfile(id="u1", email="u1@example.com", status=status, role=role)


class TestCanRestoreOrDelete(unittest.TestCase):
    def test_offline_no_profile_allowed(self):
        self.assertTrue(can_restore_or_delete(None))

    def test_approved_admin_allowed(self):
        self.assertTrue(can_restore_or_delete(_profile(role="admin")))

    def test_approved_editor_blocked(self):
        self.assertFalse(can_restore_or_delete(_profile(role="editor")))

    def test_approved_viewer_blocked(self):
        self.assertFalse(can_restore_or_delete(_profile(role="viewer")))

    def test_pending_admin_role_but_not_approved_blocked(self):
        # is_admin은 approved + admin일 때만 True (AppUserProfile 자체 정의)
        self.assertFalse(can_restore_or_delete(_profile(status="pending", role="admin")))


class _FakeBackupService:
    def __init__(self, records=None):
        self._records = records or []
        self.restore_calls = []
        self.delete_calls = []

    def list_backups(self):
        return list(self._records)

    def restore_backup(self, path):
        self.restore_calls.append(path)
        from services.backup_service import RestoreResult
        return RestoreResult(success=True, pre_restore_backup_path="fake-pre-restore.zip")

    def delete_backup(self, path):
        self.delete_calls.append(path)
        return True

    def create_backup(self, backup_type="manual"):
        raise AssertionError("이 테스트에서는 호출되지 않아야 함")


class TestBackupPanelSmoke(unittest.TestCase):
    def test_panel_renders_backup_list_under_real_mainloop(self):
        import customtkinter as ctk
        from gui.panels.backup_panel import BackupPanel

        records = [
            BackupRecord(
                filename="backup-manual-1.zip", path="C:/fake/backup-manual-1.zip",
                created_at="2026-07-18T10:00:00", backup_type="manual", app_version="1.2.1",
                size_bytes=1234, file_count=2, validated=True,
            ),
        ]
        fake = _FakeBackupService(records=records)

        root = ctk.CTk()
        root.withdraw()
        result = {}
        try:
            panel = BackupPanel(root, fake, current_profile_fn=lambda: _profile(role="admin"))

            def checker():
                deadline = time.time() + 5
                while time.time() < deadline:
                    if panel._tree.get_children():
                        break
                    time.sleep(0.02)
                result["children"] = panel._tree.get_children()
                panel.dispose()
                root.after(0, root.quit)

            threading.Thread(target=checker, daemon=True).start()
            root.mainloop()
        finally:
            root.destroy()

        self.assertEqual(result.get("children"), ("0",), "fake가 반환한 백업 1건이 트리에 채워져야 함")

    def test_restore_cancelled_does_not_call_restore_backup(self):
        """확인 대화상자에서 취소하면 BackupService.restore_backup()이 호출되지 않아야 한다."""
        import customtkinter as ctk
        from gui.panels.backup_panel import BackupPanel

        records = [
            BackupRecord(
                filename="backup-manual-1.zip", path="C:/fake/backup-manual-1.zip",
                created_at="2026-07-18T10:00:00", backup_type="manual", app_version="1.2.1",
                size_bytes=1234, file_count=2, validated=True,
            ),
        ]
        fake = _FakeBackupService(records=records)

        root = ctk.CTk()
        root.withdraw()
        try:
            panel = BackupPanel(root, fake, current_profile_fn=lambda: _profile(role="admin"))

            def do_click_and_quit():
                panel._tree.selection_set("0")
                with patch("gui.panels.backup_panel.messagebox.askyesno", return_value=False):
                    panel._on_restore_click()
                panel.dispose()
                root.quit()

            def checker():
                deadline = time.time() + 5
                while time.time() < deadline and not panel._tree.get_children():
                    time.sleep(0.02)
                root.after(0, do_click_and_quit)

            threading.Thread(target=checker, daemon=True).start()
            root.mainloop()

            self.assertEqual(fake.restore_calls, [], "취소했는데 restore_backup()이 호출됨")
        finally:
            root.destroy()

    def test_restore_blocked_for_non_admin_logged_in_user(self):
        import customtkinter as ctk
        from gui.panels.backup_panel import BackupPanel

        records = [
            BackupRecord(
                filename="backup-manual-1.zip", path="C:/fake/backup-manual-1.zip",
                created_at="2026-07-18T10:00:00", backup_type="manual", app_version="1.2.1",
                size_bytes=1234, file_count=2, validated=True,
            ),
        ]
        fake = _FakeBackupService(records=records)

        root = ctk.CTk()
        root.withdraw()
        try:
            panel = BackupPanel(root, fake, current_profile_fn=lambda: _profile(role="viewer"))
            call_tracker = {}

            def do_click_and_quit():
                panel._tree.selection_set("0")
                with patch("gui.panels.backup_panel.messagebox.askyesno", return_value=True), \
                     patch("gui.panels.backup_panel.messagebox.showerror") as mock_error:
                    panel._on_restore_click()
                    call_tracker["error_called"] = mock_error.called
                panel.dispose()
                root.quit()

            def checker():
                deadline = time.time() + 5
                while time.time() < deadline and not panel._tree.get_children():
                    time.sleep(0.02)
                root.after(0, do_click_and_quit)

            threading.Thread(target=checker, daemon=True).start()
            root.mainloop()

            self.assertEqual(fake.restore_calls, [], "viewer 권한으로 restore_backup()이 호출됨")
            self.assertTrue(call_tracker.get("error_called"), "권한 없음 오류 대화상자가 표시되어야 함")
        finally:
            root.destroy()


class TestDiagnosticsPanelSmoke(unittest.TestCase):
    def test_panel_creates_and_renders_snapshot_under_real_mainloop(self):
        import customtkinter as ctk
        from gui.panels.diagnostics_panel import DiagnosticsPanel

        fake_diag_service = MagicMock()
        from services.diagnostics_service import DiagnosticsSnapshot
        fake_diag_service.collect.return_value = DiagnosticsSnapshot(collected_at="2026-07-18T10:00:00")
        fake_diag_service.to_copy_text.return_value = "[애플리케이션]\n버전: 1.2.1"

        root = ctk.CTk()
        root.withdraw()
        result = {}
        try:
            panel = DiagnosticsPanel(root, fake_diag_service)

            def checker():
                deadline = time.time() + 5
                while time.time() < deadline:
                    text = panel._text.get("1.0", "end").strip()
                    if text and text != "조회 중...":
                        break
                    time.sleep(0.02)
                result["text"] = panel._text.get("1.0", "end")
                panel.dispose()
                root.after(0, root.quit)

            threading.Thread(target=checker, daemon=True).start()
            root.mainloop()
        finally:
            root.destroy()

        self.assertIn("애플리케이션", result.get("text", ""))
        fake_diag_service.collect.assert_called()


if __name__ == "__main__":
    unittest.main()
