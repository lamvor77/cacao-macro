# 하단 로그 패널
# 프로그램 동작 상태를 실시간으로 표시하는 읽기 전용 텍스트 영역이다.

from datetime import datetime
import customtkinter as ctk
from config.settings import (
    LOG_PANEL_HEIGHT, LOG_MAX_LINES,
    FONT_FAMILY, FONT_FAMILY_MONO
)


class LogPanel(ctk.CTkFrame):
    """실시간 로그를 표시하는 하단 패널"""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, height=LOG_PANEL_HEIGHT, **kwargs)
        self.grid_propagate(False)  # 높이 고정

        self._line_count: int = 0
        self._create_layout()

    def _create_layout(self) -> None:
        """레이아웃 생성"""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)  # 헤더 행 고정
        self.grid_rowconfigure(1, weight=1)  # 텍스트박스 확장

        self._create_header()
        self._create_textbox()

    def _create_header(self) -> None:
        """헤더: 제목 레이블 + 지우기 버튼"""
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="로그",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            header,
            text="지우기",
            width=58,
            height=26,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            command=self.clear,
        ).grid(row=0, column=1, sticky="e")

    def _create_textbox(self) -> None:
        """로그 출력 텍스트박스 (읽기 전용)"""
        self.log_textbox = ctk.CTkTextbox(
            self,
            font=ctk.CTkFont(family=FONT_FAMILY_MONO, size=11),
            wrap="word",
            state="disabled",
        )
        self.log_textbox.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 6))

    # ===== 공개 메서드 =====

    def log(self, message: str) -> None:
        """타임스탬프와 함께 로그 메시지를 추가한다."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"

        self.log_textbox.configure(state="normal")
        self.log_textbox.insert("end", line)
        self._line_count += 1

        # 최대 줄 수 초과 시 가장 오래된 줄 삭제
        if self._line_count > LOG_MAX_LINES:
            self.log_textbox.delete("1.0", "2.0")
            self._line_count -= 1

        self.log_textbox.configure(state="disabled")
        self.log_textbox.see("end")  # 최신 로그로 자동 스크롤

    def clear(self) -> None:
        """로그 내용을 모두 지운다."""
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")
        self._line_count = 0
