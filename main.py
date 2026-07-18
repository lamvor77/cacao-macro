# 프로그램 진입점
# 중복 실행 방지 → 파일 로그 설정 → CustomTkinter 앱 실행

import logging
import socket
import sys
import tkinter as tk
import tkinter.messagebox as messagebox

import customtkinter as ctk

from config.settings import LOCK_PORT
from core.license_manager import LicenseManager
from utils.logger_setup import setup_file_logging
from gui.main_window import MainWindow

logger = logging.getLogger(__name__)


def _acquire_lock() -> socket.socket | None:
    """소켓 포트를 점유해 중복 실행을 방지한다."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", LOCK_PORT))
        return sock
    except OSError:
        sock.close()
        return None


def _show_duplicate_error() -> None:
    """중복 실행 오류 다이얼로그를 표시한다."""
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "실행 오류",
        "프로그램이 이미 실행 중입니다.\n기존 창을 확인해 주세요.",
    )
    root.destroy()


def _show_license_error(reason: str) -> None:
    """라이선스 오류 다이얼로그를 표시한다."""
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("라이선스 오류", reason)
    root.destroy()


def main() -> None:
    # 1. 중복 실행 확인
    lock = _acquire_lock()
    if lock is None:
        _show_duplicate_error()
        sys.exit(1)

    # 2. 파일 로그 설정 (앱 실행 전 가장 먼저)
    log_file = setup_file_logging()
    logger.info("=" * 60)
    logger.info("프로그램 시작")
    logger.info(f"로그 파일: {log_file}")

    try:
        # 3. 라이선스 확인 — 개발 모드(관리자 본인)는 항상 통과, exe로 빌드된
        #    경우에만 빌드 시 포함된 license_build.json의 사용 기간을 검사한다.
        license_mgr = LicenseManager()
        valid, reason = license_mgr.check_build_license()
        if not valid:
            logger.warning(f"라이선스 확인 실패 - 프로그램 종료 (사유: {reason})")
            _show_license_error(reason)
            sys.exit(1)

        # 4. CustomTkinter 전역 설정
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # 5. 메인 윈도우 실행
        app = MainWindow(log_file=log_file)
        app.mainloop()

    except Exception:
        logger.exception("프로그램 실행 중 예외 발생")
        raise

    finally:
        logger.info("프로그램 종료")
        lock.close()


if __name__ == "__main__":
    main()
