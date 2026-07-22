# 운영 관리자 전용 사용자 관리 창 (Phase 4-2)
#
# UI → AdminService → Supabase RPC → 감사로그/DB 원칙을 그대로 따른다 — 이 파일은
# app_users/admin_audit_logs 테이블 이름을 전혀 모르고 AdminService의 공개
# 메서드(list_users/approve_user/block_user/unblock_user/update_user_role/
# list_audit_logs)만 호출한다. 새 Supabase client도 만들지 않는다 —
# MainWindow가 만들어 주입하는 AdminService(공유 client_manager 기반)를 그대로 쓴다.
#
# 판정/변환 로직(권한 노출, 버튼 활성화, payload 생성, 오류 메시지 매핑, 비동기
# 응답 순서 관리)은 gui/panels/admin_ui_state.py에 전부 분리되어 있다 — 이 파일은
# 그 결과로 위젯을 그리고 이벤트를 연결하는 역할만 한다.

import logging
import threading
import tkinter as tk
import tkinter.messagebox as messagebox
from datetime import datetime
from tkinter import ttk
from typing import Callable, Optional

import customtkinter as ctk

from config.settings import FONT_FAMILY
from gui.panels.admin_ui_state import (
    APPROVE_DIALOG_ROLES,
    DEFAULT_PAGE_SIZE,
    RESTORE_STATUS_OPTIONS,
    ROLE_CHANGE_DIALOG_ROLES,
    ROLE_FILTER_OPTIONS,
    STATUS_FILTER_OPTIONS,
    AdminRequestSequencer,
    action_label_ko,
    build_approve_payload,
    build_audit_log_params,
    build_block_payload,
    build_list_users_params,
    build_role_change_payload,
    build_unblock_payload,
    can_change_role_to,
    describe_admin_error,
    get_admin_action_state,
)
from services.admin_service import AdminPermissionError, AdminService, AdminUserRecord

logger = logging.getLogger(__name__)


def _format_dt(value: Optional[str]) -> str:
    """ISO8601 문자열을 사람이 읽기 좋은 형태로 줄인다. 파싱 실패/None이면 안전한 폴백."""
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value)


class OperationsAdminPanel(ctk.CTkToplevel):
    """운영 관리자(app_users.role='admin' + status='approved') 전용 사용자 관리 창."""

    def __init__(
        self,
        parent,
        admin_service: AdminService,
        current_user_id: str,
        on_permission_lost: Optional[Callable[[], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self._admin = admin_service
        self._current_user_id = current_user_id
        self._on_permission_lost = on_permission_lost or (lambda: None)
        self._log = log_callback or (lambda msg: None)

        self._users_seq = AdminRequestSequencer()
        self._audit_seq = AdminRequestSequencer()

        self._users: list[AdminUserRecord] = []
        self._selected_user_id: Optional[str] = None
        self._users_offset = 0
        self._audit_offset = 0
        self._audit_target_filter: Optional[str] = None
        self._audit_target_label: Optional[str] = None

        self.title("운영 관리자")
        self.geometry("1000x700")
        self.minsize(860, 580)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        # 창을 그리자마자(다음 이벤트 루프 턴에) 최초 사용자 목록을 조회한다 —
        # __init__ 도중에는 아직 위젯 레이아웃이 완전히 확정되지 않았을 수 있어
        # 한 박자 늦춰 호출한다(MainWindow의 기존 self.after(100, ...) 관례와 동일).
        self.after(50, lambda: self._search_users(reset_offset=True))

    # ===== UI 구성 =====

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.tab_view = ctk.CTkTabview(self)
        self.tab_view.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        tab_users = self.tab_view.add("사용자 관리")
        tab_audit = self.tab_view.add("감사 로그")

        self._build_users_tab(tab_users)
        self._build_audit_tab(tab_audit)

    def _build_users_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        # ----- 검색/필터 바 -----
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self._search_var = tk.StringVar()
        search_entry = ctk.CTkEntry(
            bar, textvariable=self._search_var, width=220,
            placeholder_text="이메일 또는 이름 검색",
        )
        search_entry.grid(row=0, column=0, padx=(0, 6))
        search_entry.bind("<Return>", lambda e: self._search_users(reset_offset=True))

        ctk.CTkLabel(bar, text="상태").grid(row=0, column=1, padx=(8, 2))
        self._status_var = tk.StringVar(value="전체")
        ctk.CTkOptionMenu(
            bar, variable=self._status_var, values=list(STATUS_FILTER_OPTIONS), width=100,
            command=lambda _v: self._search_users(reset_offset=True),
        ).grid(row=0, column=2)

        ctk.CTkLabel(bar, text="역할").grid(row=0, column=3, padx=(8, 2))
        self._role_var = tk.StringVar(value="전체")
        ctk.CTkOptionMenu(
            bar, variable=self._role_var, values=list(ROLE_FILTER_OPTIONS), width=100,
            command=lambda _v: self._search_users(reset_offset=True),
        ).grid(row=0, column=4)

        self._search_btn = ctk.CTkButton(
            bar, text="조회", width=70, command=lambda: self._search_users(reset_offset=True)
        )
        self._search_btn.grid(row=0, column=5, padx=(8, 4))
        self._refresh_btn = ctk.CTkButton(
            bar, text="새로고침", width=80, command=lambda: self._search_users(reset_offset=False)
        )
        self._refresh_btn.grid(row=0, column=6)

        self._users_status_label = ctk.CTkLabel(bar, text="", text_color="gray60")
        self._users_status_label.grid(row=0, column=7, padx=(12, 0))

        # ----- 사용자 목록 -----
        columns = ("display_name", "email", "status", "role", "created_at", "approved_at", "updated_at")
        headers = {
            "display_name": "이름", "email": "이메일", "status": "상태", "role": "역할",
            "created_at": "가입일", "approved_at": "승인일", "updated_at": "수정일",
        }
        tree_frame = ctk.CTkFrame(tab)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)

        self._tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            self._tree.heading(col, text=headers[col])
            width = 220 if col == "email" else (140 if col == "display_name" else 110)
            self._tree.column(col, width=width, anchor="w", stretch=(col == "email"))
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.bind("<<TreeviewSelect>>", self._on_row_selected)

        # ----- 페이지네이션 -----
        page_bar = ctk.CTkFrame(tab, fg_color="transparent")
        page_bar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self._prev_btn = ctk.CTkButton(page_bar, text="이전", width=70, command=self._go_prev_page)
        self._prev_btn.grid(row=0, column=0)
        self._page_label = ctk.CTkLabel(page_bar, text="")
        self._page_label.grid(row=0, column=1, padx=8)
        self._next_btn = ctk.CTkButton(page_bar, text="다음", width=70, command=self._go_next_page)
        self._next_btn.grid(row=0, column=2)

        # ----- 상세 + 액션 -----
        detail = ctk.CTkFrame(tab)
        detail.grid(row=3, column=0, sticky="ew")

        self._detail_label = ctk.CTkLabel(
            detail, text="사용자를 선택하세요.", justify="left", anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        )
        self._detail_label.grid(row=0, column=0, columnspan=6, sticky="w", padx=10, pady=(10, 6))

        self._approve_btn = ctk.CTkButton(detail, text="승인", width=90, command=self._on_approve_click)
        self._approve_btn.grid(row=1, column=0, padx=(10, 4), pady=(0, 10))
        self._role_btn = ctk.CTkButton(detail, text="역할 변경", width=90, command=self._on_role_change_click)
        self._role_btn.grid(row=1, column=1, padx=4, pady=(0, 10))
        self._block_btn = ctk.CTkButton(
            detail, text="차단", width=90, fg_color="#B71C1C", hover_color="#7F0000",
            command=self._on_block_click,
        )
        self._block_btn.grid(row=1, column=2, padx=4, pady=(0, 10))
        self._unblock_btn = ctk.CTkButton(detail, text="차단 해제", width=90, command=self._on_unblock_click)
        self._unblock_btn.grid(row=1, column=3, padx=4, pady=(0, 10))
        self._audit_for_user_btn = ctk.CTkButton(
            detail, text="이 사용자의 감사로그", width=150, command=self._show_audit_for_selected
        )
        self._audit_for_user_btn.grid(row=1, column=4, padx=4, pady=(0, 10))

        self._apply_action_state(get_admin_action_state(self._current_user_id, None))

    def _build_audit_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(bar, text="작업 유형").grid(row=0, column=0, padx=(0, 4))
        self._audit_action_var = tk.StringVar(value="전체")
        action_values = ["전체"] + list(action_label_ko(a) for a in (
            "user_approved", "user_blocked", "user_unblocked", "user_role_changed", "user_profile_updated"
        ))
        self._audit_action_display_to_raw = {"전체": None, **{
            action_label_ko(a): a for a in (
                "user_approved", "user_blocked", "user_unblocked", "user_role_changed", "user_profile_updated"
            )
        }}
        ctk.CTkOptionMenu(
            bar, variable=self._audit_action_var, values=action_values, width=130,
            command=lambda _v: self._search_audit_logs(reset_offset=True),
        ).grid(row=0, column=1)

        self._audit_filter_label = ctk.CTkLabel(bar, text="", text_color="gray60")
        self._audit_filter_label.grid(row=0, column=2, padx=(12, 4))
        self._audit_clear_filter_btn = ctk.CTkButton(
            bar, text="필터 해제", width=80, command=self._clear_audit_target_filter
        )
        self._audit_clear_filter_btn.grid(row=0, column=3, padx=(0, 8))

        ctk.CTkButton(
            bar, text="새로고침", width=80, command=lambda: self._search_audit_logs(reset_offset=False)
        ).grid(row=0, column=4)

        self._audit_status_label = ctk.CTkLabel(bar, text="", text_color="gray60")
        self._audit_status_label.grid(row=0, column=5, padx=(12, 0))

        columns = ("created_at", "actor_email", "target_email", "action", "old_status", "new_status",
                   "old_role", "new_role", "reason")
        headers = {
            "created_at": "일시", "actor_email": "실행자", "target_email": "대상", "action": "작업",
            "old_status": "이전 상태", "new_status": "새 상태", "old_role": "이전 역할",
            "new_role": "새 역할", "reason": "사유",
        }
        tree_frame = ctk.CTkFrame(tab)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)

        self._audit_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            self._audit_tree.heading(col, text=headers[col])
            width = 220 if col in ("actor_email", "target_email") else (150 if col == "reason" else 110)
            self._audit_tree.column(col, width=width, anchor="w")
        self._audit_tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._audit_tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self._audit_tree.configure(yscrollcommand=vsb.set)

        page_bar = ctk.CTkFrame(tab, fg_color="transparent")
        page_bar.grid(row=2, column=0, sticky="ew")
        self._audit_prev_btn = ctk.CTkButton(page_bar, text="이전", width=70, command=self._audit_prev_page)
        self._audit_prev_btn.grid(row=0, column=0)
        self._audit_page_label = ctk.CTkLabel(page_bar, text="")
        self._audit_page_label.grid(row=0, column=1, padx=8)
        self._audit_next_btn = ctk.CTkButton(page_bar, text="다음", width=70, command=self._audit_next_page)
        self._audit_next_btn.grid(row=0, column=2)

    # ===== 공통 헬퍼 =====

    def _run_in_thread(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _apply_action_state(self, state) -> None:
        self._approve_btn.configure(state="normal" if state.can_approve else "disabled")
        self._block_btn.configure(state="normal" if state.can_block else "disabled")
        self._unblock_btn.configure(state="normal" if state.can_unblock else "disabled")
        self._role_btn.configure(state="normal" if state.can_change_role else "disabled")

    def _selected_user(self) -> Optional[AdminUserRecord]:
        if self._selected_user_id is None:
            return None
        for u in self._users:
            if u.id == self._selected_user_id:
                return u
        return None

    def _handle_admin_error(self, exc: Exception) -> None:
        message = describe_admin_error(exc)
        if isinstance(exc, AdminPermissionError):
            self._log("[경고] 운영 관리자 권한이 없어졌거나 세션이 만료되었습니다 — 창을 닫습니다.")
            messagebox.showerror("권한 오류", message, parent=self)
            self._on_permission_lost()
            self.destroy()
            return
        messagebox.showerror("오류", message, parent=self)

    # ===== 사용자 목록 조회 =====

    def _search_users(self, reset_offset: bool) -> None:
        if self._users_seq.in_flight:
            return  # 새로고침/조회 연타로 인한 중복 실행 방지
        if reset_offset:
            self._users_offset = 0

        params = build_list_users_params(
            search_text=self._search_var.get(),
            status_filter=self._status_var.get(),
            role_filter=self._role_var.get(),
            limit=DEFAULT_PAGE_SIZE,
            offset=self._users_offset,
        )

        gen = self._users_seq.start()
        self._search_btn.configure(state="disabled")
        self._refresh_btn.configure(state="disabled")
        self._users_status_label.configure(text="조회 중...")

        def worker():
            try:
                users = self._admin.list_users(**params)
                self.after(0, lambda: self._apply_search_result(gen, users, None))
            except Exception as e:  # AdminService가 이미 좁혀둔 예외 계층 + 방어적 catch-all
                self.after(0, lambda: self._apply_search_result(gen, None, e))

        self._run_in_thread(worker)

    def _apply_search_result(self, gen: int, users, error: Optional[Exception]) -> None:
        try:
            if not self._users_seq.accept(gen):
                return  # 더 최신 조회가 이미 진행 중 — 이 응답은 버린다(오래된 응답 방지)

            self._search_btn.configure(state="normal")
            self._refresh_btn.configure(state="normal")

            if error is not None:
                self._users_status_label.configure(text="")
                self._handle_admin_error(error)
                return

            self._users = users
            self._users_status_label.configure(text=f"{len(users)}건" if users else "결과 없음")
            self._populate_users_tree(users)
            self._page_label.configure(
                text=f"{self._users_offset + 1}~{self._users_offset + len(users)}"
                if users else f"{self._users_offset}~{self._users_offset}"
            )
            self._prev_btn.configure(state="normal" if self._users_offset > 0 else "disabled")
            self._next_btn.configure(state="normal" if len(users) == DEFAULT_PAGE_SIZE else "disabled")
        finally:
            self._users_seq.finish(gen)

    def _populate_users_tree(self, users: list) -> None:
        prev_selected = self._selected_user_id
        self._tree.delete(*self._tree.get_children())
        for u in users:
            self._tree.insert("", "end", iid=u.id, values=(
                u.display_name or "-", u.email or "-", u.status, u.role,
                _format_dt(u.created_at), _format_dt(u.approved_at), _format_dt(u.updated_at),
            ))

        # 이전 선택 복원 시도 — 대상이 새 목록에 없으면 선택 해제
        if prev_selected and any(u.id == prev_selected for u in users):
            self._tree.selection_set(prev_selected)
            self._selected_user_id = prev_selected
        else:
            self._selected_user_id = None
        self._refresh_detail_and_actions()

    def _on_row_selected(self, _event=None) -> None:
        selection = self._tree.selection()
        self._selected_user_id = selection[0] if selection else None
        self._refresh_detail_and_actions()

    def _refresh_detail_and_actions(self) -> None:
        user = self._selected_user()
        if user is None:
            self._detail_label.configure(text="사용자를 선택하세요.")
        else:
            self._detail_label.configure(text=(
                f"{user.display_name or '(이름 없음)'}  |  {user.email}\n"
                f"상태: {user.status}   역할: {user.role}   승인일: {_format_dt(user.approved_at)}"
            ))
        self._apply_action_state(get_admin_action_state(self._current_user_id, user))

    def _go_prev_page(self) -> None:
        if self._users_offset <= 0:
            return
        self._users_offset = max(0, self._users_offset - DEFAULT_PAGE_SIZE)
        self._search_users(reset_offset=False)

    def _go_next_page(self) -> None:
        self._users_offset += DEFAULT_PAGE_SIZE
        self._search_users(reset_offset=False)

    # ===== 승인 =====

    def _on_approve_click(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        dialog = _ApproveDialog(self, user)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        role, reason = dialog.result
        payload = build_approve_payload(user.id, role, reason)
        self._run_mutation(
            lambda: self._admin.approve_user(**payload),
            success_message=f"사용자를 {role} 권한으로 승인했습니다.",
        )

    # ===== 역할 변경 =====

    def _on_role_change_click(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        dialog = _RoleChangeDialog(self, user, self._current_user_id)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        new_role, reason = dialog.result

        if new_role == "admin":
            if not messagebox.askyesno(
                "관리자 권한 부여 확인",
                "이 사용자는 다른 사용자를 승인·차단하고 역할을 변경할 수 있게 됩니다.\n계속하시겠습니까?",
                parent=self,
            ):
                return

        payload = build_role_change_payload(user.id, new_role, reason)
        self._run_mutation(
            lambda: self._admin.update_user_role(**payload),
            success_message=f"사용자 역할을 {new_role}(으)로 변경했습니다.",
        )

    # ===== 차단 =====

    def _on_block_click(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        if not messagebox.askyesno(
            "사용자 차단",
            "선택한 사용자를 차단하시겠습니까?\n"
            "차단된 사용자는 승인된 역할이 있더라도 서비스를 사용할 수 없습니다.",
            parent=self,
        ):
            return
        reason = _ask_reason(self, "차단 사유(선택, 운영 추적을 위해 입력을 권장합니다)")
        if reason is None:
            return
        payload = build_block_payload(user.id, reason)
        self._run_mutation(
            lambda: self._admin.block_user(**payload),
            success_message="사용자를 차단했습니다.",
        )

    # ===== 차단 해제 =====

    def _on_unblock_click(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        dialog = _UnblockDialog(self, user)
        self.wait_window(dialog)
        if dialog.result is None:
            return
        restore_status, reason = dialog.result
        payload = build_unblock_payload(user.id, restore_status, reason)
        self._run_mutation(
            lambda: self._admin.unblock_user(**payload),
            success_message="사용자 차단을 해제했습니다.",
        )

    # ===== 변경 작업 공통 실행 =====

    def _run_mutation(self, call, success_message: str) -> None:
        """승인/역할변경/차단/차단해제 공통 실행 — 완료 후 현재 필터로 목록을 재조회한다."""
        self._set_all_action_buttons_busy(True)

        def worker():
            try:
                call()
                self.after(0, lambda: self._on_mutation_success(success_message))
            except Exception as e:
                self.after(0, lambda: self._on_mutation_error(e))

        self._run_in_thread(worker)

    def _on_mutation_success(self, message: str) -> None:
        self._set_all_action_buttons_busy(False)
        messagebox.showinfo("완료", message, parent=self)
        self._log(f"[INFO] 운영 관리자 작업 완료 — {message}")
        # 일관성을 위해 변경 성공 후 현재 목록을 재조회한다(권장 정책).
        self._search_users(reset_offset=False)
        if self._audit_seq is not None:
            self._search_audit_logs(reset_offset=False)

    def _on_mutation_error(self, exc: Exception) -> None:
        self._set_all_action_buttons_busy(False)
        self._handle_admin_error(exc)

    def _set_all_action_buttons_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for btn in (self._approve_btn, self._role_btn, self._block_btn, self._unblock_btn):
            btn.configure(state=state)
        if not busy:
            # 실제 활성화 여부는 선택된 사용자 상태에 따라 다시 판정한다.
            self._refresh_detail_and_actions()

    # ===== 감사로그 =====

    def _show_audit_for_selected(self) -> None:
        user = self._selected_user()
        if user is None:
            return
        self._audit_target_filter = user.id
        self._audit_target_label = user.email
        self._audit_filter_label.configure(text=f"대상: {user.email}")
        self.tab_view.set("감사 로그")
        self._search_audit_logs(reset_offset=True)

    def _clear_audit_target_filter(self) -> None:
        self._audit_target_filter = None
        self._audit_target_label = None
        self._audit_filter_label.configure(text="")
        self._search_audit_logs(reset_offset=True)

    def _search_audit_logs(self, reset_offset: bool) -> None:
        if self._audit_seq.in_flight:
            return
        if reset_offset:
            self._audit_offset = 0

        action_display = self._audit_action_var.get()
        action_raw = self._audit_action_display_to_raw.get(action_display, None)
        params = build_audit_log_params(
            target_user_id=self._audit_target_filter,
            action_filter=(action_raw or "전체"),
            limit=DEFAULT_PAGE_SIZE,
            offset=self._audit_offset,
        )

        gen = self._audit_seq.start()
        self._audit_status_label.configure(text="조회 중...")

        def worker():
            try:
                logs = self._admin.list_audit_logs(**params)
                self.after(0, lambda: self._apply_audit_result(gen, logs, None))
            except Exception as e:
                self.after(0, lambda: self._apply_audit_result(gen, None, e))

        self._run_in_thread(worker)

    def _apply_audit_result(self, gen: int, logs, error: Optional[Exception]) -> None:
        try:
            if not self._audit_seq.accept(gen):
                return

            if error is not None:
                self._audit_status_label.configure(text="")
                self._handle_admin_error(error)
                return

            self._audit_status_label.configure(text=f"{len(logs)}건" if logs else "결과 없음")
            self._audit_tree.delete(*self._audit_tree.get_children())
            for log in logs:
                self._audit_tree.insert("", "end", iid=log.id, values=(
                    _format_dt(log.created_at), log.actor_email or "-", log.target_email or "-",
                    action_label_ko(log.action), log.old_status or "-", log.new_status or "-",
                    log.old_role or "-", log.new_role or "-", log.reason or "-",
                ))
            self._audit_page_label.configure(
                text=f"{self._audit_offset + 1}~{self._audit_offset + len(logs)}"
                if logs else f"{self._audit_offset}~{self._audit_offset}"
            )
            self._audit_prev_btn.configure(state="normal" if self._audit_offset > 0 else "disabled")
            self._audit_next_btn.configure(state="normal" if len(logs) == DEFAULT_PAGE_SIZE else "disabled")
        finally:
            self._audit_seq.finish(gen)

    def _audit_prev_page(self) -> None:
        if self._audit_offset <= 0:
            return
        self._audit_offset = max(0, self._audit_offset - DEFAULT_PAGE_SIZE)
        self._search_audit_logs(reset_offset=False)

    def _audit_next_page(self) -> None:
        self._audit_offset += DEFAULT_PAGE_SIZE
        self._search_audit_logs(reset_offset=False)

    # ===== 종료/권한 상실 처리 =====

    def _on_close(self) -> None:
        self.dispose()
        self.destroy()

    def dispose(self) -> None:
        """창이 실제로 파괴되기 전에 호출 — 이후 도착하는 비동기 응답 콜백이
        이미 사라진 위젯을 건드리지 않도록 시퀀서를 폐기 표시한다. 로그아웃이나
        권한 상실 시 main_window가 창을 닫기 전에도 이 메서드를 호출할 수 있다."""
        self._users_seq.dispose()
        self._audit_seq.dispose()


# ============================================================
# 작업 다이얼로그
# ============================================================

def _ask_reason(parent, prompt: str) -> Optional[str]:
    """사유 입력 전용 간단 다이얼로그. 취소 시 None, 입력(빈 문자열 포함) 시 문자열."""
    dialog = _ReasonDialog(parent, prompt)
    parent.wait_window(dialog)
    return dialog.result


class _ReasonDialog(ctk.CTkToplevel):
    def __init__(self, parent, prompt: str):
        super().__init__(parent)
        self.result: Optional[str] = None
        self.title("사유 입력")
        self.geometry("360x160")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(self, text=prompt, justify="left", wraplength=320).pack(padx=16, pady=(16, 8), anchor="w")
        self._entry = ctk.CTkEntry(self)
        self._entry.pack(padx=16, pady=(0, 16), fill="x")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(0, 16))
        ctk.CTkButton(btns, text="확인", command=self._confirm).grid(row=0, column=0, padx=6)
        ctk.CTkButton(btns, text="취소", fg_color="gray40", command=self.destroy).grid(row=0, column=1, padx=6)

    def _confirm(self) -> None:
        self.result = self._entry.get()[:500]
        self.destroy()


class _ApproveDialog(ctk.CTkToplevel):
    def __init__(self, parent, user: AdminUserRecord):
        super().__init__(parent)
        self.result = None
        self.title("사용자 승인")
        self.geometry("380x280")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(
            self, text=f"{user.display_name or '(이름 없음)'}\n{user.email}", justify="left",
        ).pack(padx=16, pady=(16, 10), anchor="w")

        ctk.CTkLabel(self, text="승인 역할").pack(padx=16, anchor="w")
        self._role_var = tk.StringVar(value="viewer")
        ctk.CTkOptionMenu(self, variable=self._role_var, values=list(APPROVE_DIALOG_ROLES)).pack(
            padx=16, pady=(0, 10), fill="x"
        )

        ctk.CTkLabel(self, text="사유(선택, 최대 500자)").pack(padx=16, anchor="w")
        self._reason_entry = ctk.CTkEntry(self)
        self._reason_entry.pack(padx=16, pady=(0, 16), fill="x")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(0, 16))
        ctk.CTkButton(btns, text="승인", command=self._confirm).grid(row=0, column=0, padx=6)
        ctk.CTkButton(btns, text="취소", fg_color="gray40", command=self.destroy).grid(row=0, column=1, padx=6)

    def _confirm(self) -> None:
        self.result = (self._role_var.get(), self._reason_entry.get()[:500])
        self.destroy()


class _RoleChangeDialog(ctk.CTkToplevel):
    def __init__(self, parent, user: AdminUserRecord, current_user_id: str):
        super().__init__(parent)
        self.result = None
        self._user = user
        self._current_user_id = current_user_id
        self.title("역할 변경")
        self.geometry("380x280")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(
            self, text=f"{user.display_name or '(이름 없음)'}\n{user.email}\n현재 역할: {user.role}",
            justify="left",
        ).pack(padx=16, pady=(16, 10), anchor="w")

        ctk.CTkLabel(self, text="새 역할").pack(padx=16, anchor="w")
        self._role_var = tk.StringVar(value=user.role)
        self._role_menu = ctk.CTkOptionMenu(
            self, variable=self._role_var, values=list(ROLE_CHANGE_DIALOG_ROLES),
            command=self._on_role_selected,
        )
        self._role_menu.pack(padx=16, pady=(0, 10), fill="x")

        ctk.CTkLabel(self, text="사유(선택, 최대 500자)").pack(padx=16, anchor="w")
        self._reason_entry = ctk.CTkEntry(self)
        self._reason_entry.pack(padx=16, pady=(0, 16), fill="x")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(0, 16))
        self._confirm_btn = ctk.CTkButton(btns, text="변경", command=self._confirm)
        self._confirm_btn.grid(row=0, column=0, padx=6)
        ctk.CTkButton(btns, text="취소", fg_color="gray40", command=self.destroy).grid(row=0, column=1, padx=6)

        self._on_role_selected(user.role)

    def _on_role_selected(self, new_role: str) -> None:
        allowed = can_change_role_to(self._current_user_id, self._user, new_role)
        self._confirm_btn.configure(state="normal" if allowed else "disabled")

    def _confirm(self) -> None:
        self.result = (self._role_var.get(), self._reason_entry.get()[:500])
        self.destroy()


class _UnblockDialog(ctk.CTkToplevel):
    def __init__(self, parent, user: AdminUserRecord):
        super().__init__(parent)
        self.result = None
        self.title("차단 해제")
        self.geometry("380x300")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(
            self, text=f"{user.display_name or '(이름 없음)'}\n{user.email}", justify="left",
        ).pack(padx=16, pady=(16, 10), anchor="w")

        ctk.CTkLabel(self, text="복원 상태").pack(padx=16, anchor="w")
        self._status_var = tk.StringVar(value="approved")
        ctk.CTkOptionMenu(self, variable=self._status_var, values=list(RESTORE_STATUS_OPTIONS)).pack(
            padx=16, pady=(0, 4), fill="x"
        )
        ctk.CTkLabel(
            self,
            text="approved: 기존 역할을 유지한 채 즉시 다시 사용 가능\npending: 다시 승인 대기 상태로 전환",
            justify="left", text_color="gray60", font=ctk.CTkFont(size=11),
        ).pack(padx=16, pady=(0, 10), anchor="w")

        ctk.CTkLabel(self, text="사유(선택, 최대 500자)").pack(padx=16, anchor="w")
        self._reason_entry = ctk.CTkEntry(self)
        self._reason_entry.pack(padx=16, pady=(0, 16), fill="x")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(0, 16))
        ctk.CTkButton(btns, text="차단 해제", command=self._confirm).grid(row=0, column=0, padx=6)
        ctk.CTkButton(btns, text="취소", fg_color="gray40", command=self.destroy).grid(row=0, column=1, padx=6)

    def _confirm(self) -> None:
        self.result = (self._status_var.get(), self._reason_entry.get()[:500])
        self.destroy()
