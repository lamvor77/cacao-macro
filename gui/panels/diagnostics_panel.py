# 진단정보 화면 (Release Candidate Sprint 1)
#
# 상태 조회는 전부 services/diagnostics_service.py에 있다 — 이 파일은 결과를
# 그리기만 한다(UI/로직 분리 원칙). 비동기 처리는 Phase 4-2의
# AdminRequestSequencer를 그대로 재사용한다(오래된 새로고침 응답이 최신
# 응답을 덮어쓰지 않도록).

import logging
import os
import threading
import tkinter.messagebox as messagebox
from typing import Callable, Optional

import customtkinter as ctk

from config.settings import FONT_FAMILY, FONT_FAMILY_MONO
from gui.panels.admin_ui_state import AdminRequestSequencer
from services.diagnostics_service import DiagnosticsService, DiagnosticsSnapshot

logger = logging.getLogger(__name__)


def _bool_ko(value) -> str:
    if value is None:
        return "확인 불가"
    return "예" if value else "아니오"


def _open_folder(path: str, log_fn: Callable[[str], None]) -> None:
    """탐색기로 폴더를 연다. 폴더가 없으면 조용히 실패하지 않고 로그를 남긴다."""
    try:
        if not path or not os.path.isdir(path):
            log_fn(f"[경고] 폴더를 찾을 수 없습니다: {path}")
            return
        os.startfile(path)  # Windows 전용(이 프로젝트의 전체 대상 플랫폼)
    except OSError as e:
        log_fn(f"[오류] 폴더 열기 실패: {e}")


class DiagnosticsPanel(ctk.CTkToplevel):
    """진단정보 화면 — 도움말/설정 위치에 준하는 별도 창으로 연다."""

    def __init__(
        self,
        parent,
        diagnostics_service: DiagnosticsService,
        on_open_backup_panel: Optional[Callable[[], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self._diag = diagnostics_service
        self._on_open_backup_panel = on_open_backup_panel
        self._log = log_callback or (lambda msg: None)
        self._seq = AdminRequestSequencer()
        self._last_snapshot: Optional[DiagnosticsSnapshot] = None

        self.title("진단정보")
        self.geometry("640x720")
        self.minsize(560, 480)
        self.transient(parent)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._create_toolbar()
        self._create_text_area()
        self._refresh()

    # ===== 레이아웃 =====

    def _create_toolbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))

        ctk.CTkButton(
            bar, text="새로고침", width=90, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            command=self._refresh,
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            bar, text="진단정보 복사", width=110, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            command=self._on_copy,
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            bar, text="로그 폴더 열기", width=110, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            command=self._on_open_log_folder,
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            bar, text="백업 폴더 열기", width=110, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            command=self._on_open_backup_folder,
        ).pack(side="left", padx=(0, 6))

        if self._on_open_backup_panel is not None:
            ctk.CTkButton(
                bar, text="백업 관리...", width=100, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                command=self._on_open_backup_panel,
            ).pack(side="left")

    def _create_text_area(self) -> None:
        self._text = ctk.CTkTextbox(
            self, font=ctk.CTkFont(family=FONT_FAMILY_MONO, size=12), wrap="word",
        )
        self._text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self._text.configure(state="disabled")

    # ===== 새로고침 (비동기) =====

    def _refresh(self) -> None:
        gen = self._seq.start()
        self._set_text("조회 중...")
        threading.Thread(target=lambda: self._collect_and_apply(gen), daemon=True).start()

    def _collect_and_apply(self, gen: int) -> None:
        try:
            snapshot = self._diag.collect()
        except Exception as e:
            logger.exception("진단정보 수집 중 예상치 못한 오류")
            self.after(0, lambda: self._apply_error(gen, e))
            return
        self.after(0, lambda: self._apply_snapshot(gen, snapshot))

    def _apply_error(self, gen: int, exc: Exception) -> None:
        if not self._seq.accept(gen):
            return
        self._seq.finish(gen)
        self._set_text(f"진단정보를 조회하지 못했습니다: {type(exc).__name__}")

    def _apply_snapshot(self, gen: int, snapshot: DiagnosticsSnapshot) -> None:
        if not self._seq.accept(gen):
            self._seq.finish(gen)
            return
        self._seq.finish(gen)
        self._last_snapshot = snapshot
        self._set_text(self._render(snapshot))

    def _set_text(self, text: str) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", text)
        self._text.configure(state="disabled")

    # ===== 렌더링 =====

    def _render(self, s: DiagnosticsSnapshot) -> str:
        env_label = "*** TEST ENVIRONMENT ***" if s.app.is_test_environment else "운영(production)"
        lines = [
            f"환경: {env_label}",
            f"조회 시각: {s.collected_at}",
            "",
            "===== 애플리케이션 =====",
            f"이름: {s.app.app_name}",
            f"버전: {s.app.version}",
            f"빌드 채널: {s.app.build_channel}",
            f"빌드 날짜: {s.app.build_date}",
            f"Git commit: {s.app.git_commit_short}",
            f"실행 모드: {s.app.run_mode}",
            f"실행 파일 경로: {s.app.executable_path}",
            f"작업 디렉터리: {s.app.current_working_dir}",
            "",
            "===== 운영체제 =====",
            f"Windows: {s.os_info.windows_version}",
            f"Python 런타임: {s.os_info.python_version}",
            f"아키텍처: {s.os_info.architecture}",
            f"사용자 데이터 폴더: {s.os_info.user_data_dir}",
            "",
            "===== 인증 =====",
            f"Google 로그인 상태: {'로그인됨' if s.auth.logged_in else '로그인 안 됨'} ({s.auth.note})",
            f"이메일: {s.auth.email or '-'}",
            f"관리자 여부: {_bool_ko(s.auth.is_admin)}",
            f"OAuth 설정: {_bool_ko(s.auth.oauth_configured)}",
            f"토큰 파일 존재: {_bool_ko(s.auth.token_file_exists)}",
        ]
        if s.auth.error:
            lines.append(f"(수집 오류: {s.auth.error})")
        lines += [
            "",
            "===== Supabase =====",
            f"설정 존재: {_bool_ko(s.supabase.configured)}",
            f"클라이언트 초기화: {_bool_ko(s.supabase.client_initialized)}",
            f"호스트(마스킹): {s.supabase.masked_host or '-'}",
            f"네트워크 상태: {s.supabase.network_status}",
        ]
        if s.supabase.error:
            lines.append(f"(수집 오류: {s.supabase.error})")
        lines += [
            "",
            "===== 동기화 =====",
            f"Offline-first: 항상 활성(로컬 우선 적용 후 백그라운드 동기화)",
            f"Pending 항목 수: {s.sync.pending_count}",
            f"Conflict 항목 수: {s.sync.conflict_count}",
            f"현재 상태: {s.sync.last_sync_state or '-'}",
            f"상세: {s.sync.last_sync_detail or '-'}",
            f"시작 동기화: {s.sync.startup_sync_note}",
        ]
        if s.sync.error:
            lines.append(f"(수집 오류: {s.sync.error})")
        lines += [
            "",
            "===== 저장소 =====",
            f"경로: {s.storage.storage_path}",
            f"저장된 파일 수: {s.storage.data_file_count}",
            f"전체 크기: {s.storage.total_size_bytes:,} bytes",
            f"마지막 수정: {s.storage.last_modified or '-'}",
            f"읽기 가능: {_bool_ko(s.storage.readable)}",
            f"쓰기 가능: {_bool_ko(s.storage.writable)}",
        ]
        if s.storage.error:
            lines.append(f"(수집 오류: {s.storage.error})")
        lines += [
            "",
            "===== 로그 =====",
            f"경로: {s.logs.log_dir}",
            f"최신 로그 파일: {s.logs.latest_log_filename or '-'}",
            f"최신 로그 수정 시각: {s.logs.latest_log_modified or '-'}",
        ]
        if s.logs.error:
            lines.append(f"(수집 오류: {s.logs.error})")
        lines += [
            "",
            "===== 라이선스 =====",
            f"파일 존재: {_bool_ko(s.license.file_exists)}",
            f"검증 상태: {'정상' if s.license.valid else (s.license.reason or '확인 불가')}",
            f"관리자 UI 활성화: {_bool_ko(s.license.admin_ui_enabled)}",
        ]
        if s.license.error:
            lines.append(f"(수집 오류: {s.license.error})")
        lines += [
            "",
            "===== 백업 =====",
            f"최신 백업 시각: {s.backup.latest_backup_at or '-'}",
            f"백업 개수: {s.backup.backup_count}",
            f"최신 백업 검증: {_bool_ko(s.backup.latest_backup_valid)}",
        ]
        if s.backup.error:
            lines.append(f"(수집 오류: {s.backup.error})")
        lines += [
            "",
            "===== 메시지 출처 (레거시/shared_messages 이원 체계) =====",
            f"Message source: {'shared_messages' if s.message_source.shared_messages_primary else 'legacy'}",
            f"shared_messages 사용: {_bool_ko(s.message_source.shared_messages_enabled)}",
            f"Legacy sync: {'enabled' if s.message_source.legacy_sync_enabled else 'disabled'}",
            f"Fallback cache: {s.message_source.fallback_cache_path or '-'}",
        ]
        if s.message_source.error:
            lines.append(f"(수집 오류: {s.message_source.error})")
        return "\n".join(lines)

    # ===== 버튼 핸들러 =====

    def _on_copy(self) -> None:
        if self._last_snapshot is None:
            return
        text = self._diag.to_copy_text(self._last_snapshot)
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("복사 완료", "진단정보를 클립보드에 복사했습니다.\n(비밀정보는 포함되지 않습니다.)", parent=self)

    def _on_open_log_folder(self) -> None:
        path = self._last_snapshot.logs.log_dir if self._last_snapshot else ""
        _open_folder(path, self._log)

    def _on_open_backup_folder(self) -> None:
        path = self._last_snapshot.backup.backup_dir if self._last_snapshot else ""
        _open_folder(path, self._log)

    def dispose(self) -> None:
        self._seq.dispose()
