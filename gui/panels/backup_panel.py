# 백업 관리 화면 (Release Candidate Sprint 1)
#
# 권한 정책(스펙 9절 권장안을 그대로 채택):
#   - 수동 백업 생성: 일반 사용자도 가능
#   - 복구: 운영 관리자만 가능(AppUserProfile.is_admin) — UI 노출은 편의
#     기능일 뿐 보안 경계가 아니다(Phase 4-2와 동일한 원칙). 이 화면은
#     로컬 파일 작업만 하므로 서버 측 재검증 대상이 없다 — 그래서 여기서는
#     "노출 여부"가 곧 유일한 게이트다. 관리자 개념이 없는 완전 오프라인
#     단독 사용자를 막지 않기 위해, current_profile이 None(비-Supabase
#     오프라인 전용 사용)이어도 복구를 허용한다 — 로그인 자체를 안 한
#     사용자에게 "관리자가 아니라 복구 불가"라고 막는 것은 이 프로그램의
#     오프라인 우선 원칙과 배치된다. 즉 "로그인했는데 admin이 아님"일 때만
#     복구 버튼을 막는다.
#   - 삭제: 복구와 동일 정책(관리자 권장)을 적용한다.
#
# 복구/삭제는 반드시 확인 대화상자를 거친다(스펙 9절).

import logging
import threading
import tkinter.messagebox as messagebox
from datetime import datetime
from tkinter import ttk
from typing import Callable, Optional

import customtkinter as ctk

from config.settings import FONT_FAMILY
from gui.panels.admin_ui_state import AdminRequestSequencer
from services.auth_service import AppUserProfile
from services.backup_service import BackupRecord, BackupService

logger = logging.getLogger(__name__)

_TYPE_LABELS_KO = {"auto": "자동", "manual": "수동", "pre_restore": "복구 전 안전백업", "unknown": "알 수 없음"}


def _format_dt(value: str) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def can_restore_or_delete(profile: Optional[AppUserProfile]) -> bool:
    """복구/삭제 버튼 활성화 판정 — 순수 함수(테스트 용이).

    로그인하지 않은 완전 오프라인 사용(profile=None)은 허용, 로그인했는데
    admin이 아니면 차단한다.
    """
    if profile is None:
        return True
    return profile.is_admin


class BackupPanel(ctk.CTkToplevel):
    """백업 목록/생성/복구/삭제 화면."""

    def __init__(
        self,
        parent,
        backup_service: BackupService,
        current_profile_fn: Callable[[], Optional[AppUserProfile]],
        on_restore_completed: Optional[Callable[[], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self._backup = backup_service
        self._current_profile_fn = current_profile_fn
        self._on_restore_completed = on_restore_completed or (lambda: None)
        self._log = log_callback or (lambda msg: None)
        self._seq = AdminRequestSequencer()
        self._records: list[BackupRecord] = []

        self.title("백업 관리")
        self.geometry("760x480")
        self.minsize(640, 400)
        self.transient(parent)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._create_toolbar()
        self._create_table()
        self._refresh()

    def _create_toolbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))

        ctk.CTkButton(
            bar, text="지금 백업", width=90, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            command=self._on_backup_now,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            bar, text="새로고침", width=90, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            command=self._refresh,
        ).pack(side="left", padx=(0, 6))

        self._restore_btn = ctk.CTkButton(
            bar, text="복구", width=90, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#E65100", hover_color="#BF360C", command=self._on_restore_click,
        )
        self._restore_btn.pack(side="left", padx=(0, 6))

        self._delete_btn = ctk.CTkButton(
            bar, text="삭제", width=90, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#B71C1C", hover_color="#7F0000", command=self._on_delete_click,
        )
        self._delete_btn.pack(side="left")

    def _create_table(self) -> None:
        frame = ctk.CTkFrame(self)
        frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        columns = ("created_at", "type", "app_version", "size", "validated")
        headers = {
            "created_at": "생성일", "type": "유형", "app_version": "앱 버전",
            "size": "크기", "validated": "검증 상태",
        }
        widths = {"created_at": 160, "type": 100, "app_version": 110, "size": 90, "validated": 90}

        self._tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            self._tree.heading(col, text=headers[col])
            self._tree.column(col, width=widths[col], anchor="w")
        self._tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=vsb.set)

    # ===== 새로고침 =====

    def _refresh(self) -> None:
        gen = self._seq.start()
        threading.Thread(target=lambda: self._do_refresh(gen), daemon=True).start()

    def _do_refresh(self, gen: int) -> None:
        try:
            records = self._backup.list_backups()
            error = None
        except Exception as e:
            logger.exception("백업 목록 조회 오류")
            records, error = [], e
        self.after(0, lambda: self._apply_refresh(gen, records, error))

    def _apply_refresh(self, gen: int, records: list, error) -> None:
        if not self._seq.accept(gen):
            self._seq.finish(gen)
            return
        self._seq.finish(gen)
        if error is not None:
            messagebox.showerror("오류", f"백업 목록을 불러오지 못했습니다: {error}", parent=self)
            return
        self._records = records
        self._tree.delete(*self._tree.get_children())
        for i, r in enumerate(records):
            validated_text = "정상" if r.validated else f"손상({r.validation_error})"
            self._tree.insert("", "end", iid=str(i), values=(
                _format_dt(r.created_at), _TYPE_LABELS_KO.get(r.backup_type, r.backup_type),
                r.app_version or "-", _format_size(r.size_bytes), validated_text,
            ))
        self._apply_permission_state()

    def _apply_permission_state(self) -> None:
        allowed = can_restore_or_delete(self._current_profile_fn())
        state = "normal" if allowed else "disabled"
        self._restore_btn.configure(state=state)
        self._delete_btn.configure(state=state)

    def _selected_record(self) -> Optional[BackupRecord]:
        selection = self._tree.selection()
        if not selection:
            return None
        idx = int(selection[0])
        if 0 <= idx < len(self._records):
            return self._records[idx]
        return None

    # ===== 지금 백업 =====

    def _on_backup_now(self) -> None:
        threading.Thread(target=self._do_backup_now, daemon=True).start()

    def _do_backup_now(self) -> None:
        try:
            record = self._backup.create_backup(backup_type="manual")
            self._log(f"[INFO] 수동 백업 완료: {record.filename}")
            self.after(0, self._refresh)
        except Exception as e:
            logger.exception("수동 백업 생성 오류")
            self.after(0, lambda: messagebox.showerror("백업 실패", str(e), parent=self))

    # ===== 복구 =====

    def _on_restore_click(self) -> None:
        record = self._selected_record()
        if record is None:
            messagebox.showwarning("선택 필요", "복구할 백업을 선택하세요.", parent=self)
            return
        if not can_restore_or_delete(self._current_profile_fn()):
            messagebox.showerror("권한 없음", "복구는 운영 관리자만 수행할 수 있습니다.", parent=self)
            return

        confirmed = messagebox.askyesno(
            "백업 복구",
            "선택한 백업으로 현재 데이터를 교체합니다.\n"
            "복구 직전 현재 데이터가 자동으로 백업됩니다.\n"
            "프로그램이 재시작될 수 있습니다.\n\n계속하시겠습니까?",
            parent=self,
        )
        if not confirmed:
            return

        self._restore_btn.configure(state="disabled")
        threading.Thread(target=lambda: self._do_restore(record), daemon=True).start()

    def _do_restore(self, record: BackupRecord) -> None:
        result = self._backup.restore_backup(record.path)
        self.after(0, lambda: self._apply_restore_result(result))

    def _apply_restore_result(self, result) -> None:
        self._apply_permission_state()
        if result.success:
            self._log(f"[INFO] 백업 복구 완료 (복구 전 안전백업: {result.pre_restore_backup_path})")
            messagebox.showinfo(
                "복구 완료",
                "백업 복구가 완료되었습니다.\n변경 사항을 반영하려면 프로그램을 재시작하세요.",
                parent=self,
            )
            self._on_restore_completed()
            self._refresh()
        else:
            self._log(f"[오류] 백업 복구 실패: {result.error}")
            messagebox.showerror("복구 실패", f"기존 데이터는 보존되었습니다.\n\n{result.error}", parent=self)

    # ===== 삭제 =====

    def _on_delete_click(self) -> None:
        record = self._selected_record()
        if record is None:
            messagebox.showwarning("선택 필요", "삭제할 백업을 선택하세요.", parent=self)
            return
        if not can_restore_or_delete(self._current_profile_fn()):
            messagebox.showerror("권한 없음", "삭제는 운영 관리자만 수행할 수 있습니다.", parent=self)
            return

        confirmed = messagebox.askyesno(
            "백업 삭제", f"이 백업을 삭제합니다: {record.filename}\n되돌릴 수 없습니다. 계속하시겠습니까?", parent=self,
        )
        if not confirmed:
            return

        try:
            self._backup.delete_backup(record.path)
            self._log(f"[INFO] 백업 삭제: {record.filename}")
            self._refresh()
        except Exception as e:
            messagebox.showerror("삭제 실패", str(e), parent=self)

    def dispose(self) -> None:
        self._seq.dispose()
