# 로그 설정 모듈
# Python logging 모듈을 날짜별 로그 파일 + UI LogPanel에 동시 출력하도록 설정한다.

import logging
import os
import sys
import traceback
from datetime import datetime


def get_log_dir() -> str:
    """logs 디렉터리 절대 경로를 반환한다 (EXE/개발 환경 모두 대응)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "logs")
    os.makedirs(path, exist_ok=True)
    return path


def setup_file_logging() -> str:
    """날짜별 로그 파일 핸들러를 설정한다.

    - 파일: logs/YYYY-MM-DD.log (DEBUG 이상 전체 기록)
    - 포맷: [2026-06-29 08:00:05] DEBUG    core.scheduler — 메시지

    Returns:
        생성된 로그 파일의 절대 경로
    """
    log_dir = get_log_dir()
    log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}.log")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # 이미 같은 파일 핸들러가 등록된 경우 중복 방지
    if not any(
        isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == log_file
        for h in root.handlers
    ):
        root.addHandler(file_handler)

    return log_file


class UILogHandler(logging.Handler):
    """WARNING 이상의 Python logging 메시지를 UI LogPanel에 표시하는 핸들러.

    - DEBUG / INFO  → 파일에만 기록 (UI 노출 없음)
    - WARNING 이상  → UI LogPanel에도 출력
    """

    def __init__(self, log_fn):
        """
        Args:
            log_fn: LogPanel.log() 같은 메시지 출력 함수
        """
        super().__init__(level=logging.WARNING)
        self._log_fn = log_fn

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            # 예외 정보가 있으면 한 줄로 요약
            if record.exc_info and record.exc_info[0] is not None:
                exc_summary = traceback.format_exception_only(
                    record.exc_info[0], record.exc_info[1]
                )[-1].strip()
                msg = f"{msg} → {exc_summary}"
            self._log_fn(f"[{record.levelname}] {msg}")
        except Exception:
            self.handleError(record)
