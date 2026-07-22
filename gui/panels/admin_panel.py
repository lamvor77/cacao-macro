# 관리자 패널 (빌드용 라이선스 발급)
# MainWindow 제목표시줄의 "관리자 모드" 버튼으로 열리며, 열 때마다 비밀번호를 재확인한다.
# 여기서 사용 기간을 정해 license_build.json을 생성하면, 배포용 exe와 같은
# 폴더에 그 파일을 배치해야 한다 — exe 내부에 포함되지 않으며, 재빌드 없이
# 이 파일만 교체해도 라이선스 갱신이 반영된다 (core/license_manager.py의
# get_external_license_path() 참고).

import logging
import tkinter.messagebox as messagebox
import tkinter.simpledialog as simpledialog
from datetime import datetime, timedelta
from typing import Callable

import customtkinter as ctk

from config.settings import FONT_FAMILY
from core.license_manager import LicenseManager

logger = logging.getLogger(__name__)


class AdminPanel(ctk.CTkToplevel):
    """관리자용 빌드 라이선스 발급 팝업 (비밀번호 확인 후 생성됨)"""

    def __init__(self, parent, license_mgr: LicenseManager, log_callback: Callable[[str], None]):
        super().__init__(parent)

        self._mgr = license_mgr
        self._log = log_callback

        self.title("관리자 모드 - 빌드용 라이선스 발급")
        self.geometry("420x300")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._build_ui()

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text="빌드용 라이선스 발급",
            font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(20, 4), sticky="w")

        ctk.CTkLabel(
            self,
            text="여기서 정한 기간이 다음 pyinstaller 빌드에 그대로 포함됩니다.",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color="gray60",
            justify="left",
        ).grid(row=1, column=0, padx=20, pady=(0, 16), sticky="w")

        today = datetime.now().date()
        default_end = today + timedelta(days=30)

        ctk.CTkLabel(
            self,
            text="시작일 (YYYY-MM-DD)",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        ).grid(row=2, column=0, padx=20, pady=(0, 4), sticky="w")

        self._start_entry = ctk.CTkEntry(self, font=ctk.CTkFont(family=FONT_FAMILY, size=13))
        self._start_entry.insert(0, today.strftime("%Y-%m-%d"))
        self._start_entry.grid(row=3, column=0, padx=20, pady=(0, 12), sticky="ew")

        ctk.CTkLabel(
            self,
            text="종료일 (YYYY-MM-DD)",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        ).grid(row=4, column=0, padx=20, pady=(0, 4), sticky="w")

        self._end_entry = ctk.CTkEntry(self, font=ctk.CTkFont(family=FONT_FAMILY, size=13))
        self._end_entry.insert(0, default_end.strftime("%Y-%m-%d"))
        self._end_entry.grid(row=5, column=0, padx=20, pady=(0, 24), sticky="ew")

        ctk.CTkButton(
            self,
            text="빌드용 파일 생성",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            height=40,
            command=self._on_generate,
        ).grid(row=6, column=0, padx=20, pady=(0, 20), sticky="ew")

    # ===== 버튼 핸들러 =====

    def _on_generate(self) -> None:
        start_str = self._start_entry.get().strip()
        end_str = self._end_entry.get().strip()
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d").date()
            end = datetime.strptime(end_str, "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror("오류", "날짜 형식이 올바르지 않습니다 (YYYY-MM-DD).", parent=self)
            return

        if end < start:
            messagebox.showerror("오류", "종료일이 시작일보다 빠릅니다.", parent=self)
            return

        license_data = self._mgr.generate_build_license(start_str, end_str)

        try:
            filepath = self._mgr.save_build_license(license_data)
        except OSError as e:
            messagebox.showerror("오류", f"빌드용 라이선스 파일 저장에 실패했습니다: {e}", parent=self)
            return

        messagebox.showinfo(
            "발급 완료",
            f"빌드용 라이선스 파일이 생성되었습니다:\n{filepath}\n\n"
            "이 상태로 pyinstaller를 실행하면 이 기간이 exe에 포함됩니다.",
            parent=self,
        )
        self._log(f"[INFO] 빌드용 라이선스 발급 완료 — {start_str} ~ {end_str}")


def open_admin_panel(parent, license_mgr: LicenseManager, log_callback: Callable[[str], None]) -> None:
    """관리자 비밀번호를 확인한 뒤, 성공한 경우에만 AdminPanel을 연다.

    LICENSE_ADMIN_PASSWORD 또는 LICENSE_SECRET_KEY가 설정되지 않았으면 비밀번호
    입력창 자체를 띄우지 않는다(원칙: 설정 누락 시 인증 성공으로 간주하지
    않으며, 애초에 시도조차 허용하지 않는다 — fail-closed).
    """
    if not license_mgr.is_fully_configured():
        logger.warning("라이선스 관리자 설정 누락 — 관리자 인증창을 열지 않습니다.")
        messagebox.showerror(
            "사용 불가",
            "이 기능은 현재 사용할 수 없습니다.\n관리자 설정을 확인해 주세요.",
            parent=parent,
        )
        log_callback("[경고] 라이선스 관리자 설정이 완료되지 않아 관리자 모드를 열 수 없습니다.")
        return

    password = simpledialog.askstring(
        "관리자 인증", "관리자 비밀번호를 입력하세요:", show="*", parent=parent
    )
    if password is None:
        return

    if not license_mgr.verify_admin_password(password):
        messagebox.showerror("오류", "비밀번호가 올바르지 않습니다.", parent=parent)
        return

    AdminPanel(parent, license_mgr, log_callback)
