# 오른쪽 제어 패널
# 상단에 동작 버튼 3개, 하단에 메시지 1~12 입력 영역(스크롤 가능)으로 구성된다.
# 버튼의 실제 동작은 MainWindow에서 콜백으로 주입받는다.

import contextlib
from typing import Callable, Optional
import customtkinter as ctk
from config.settings import (
    MESSAGE_COUNT, MESSAGE_GROUPS,
    MESSAGE_INPUT_HEIGHT, BUTTON_HEIGHT,
    FONT_FAMILY,
)


class ControlPanel(ctk.CTkFrame):
    """버튼 3개 + 메시지 1~12 입력창으로 구성된 오른쪽 패널"""

    def __init__(
        self,
        parent,
        on_get_active_rooms: Optional[Callable] = None,
        on_save_list: Optional[Callable] = None,
        on_open_saved_rooms: Optional[Callable] = None,
        on_save_messages: Optional[Callable] = None,
        on_open_messages: Optional[Callable] = None,
        on_messages_changed: Optional[Callable[[], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        **kwargs,
    ):
        super().__init__(parent, **kwargs)

        self._cb_get_active_rooms = on_get_active_rooms or (lambda: None)
        self._cb_save_list = on_save_list or (lambda: None)
        self._cb_open_saved_rooms = on_open_saved_rooms or (lambda: None)
        self._cb_save_messages = on_save_messages or (lambda: None)
        self._cb_open_messages = on_open_messages or (lambda: None)
        # 입력창이 사용자에 의해 실제로 바뀔 때만 호출된다(로컬 자동 저장 트리거용) —
        # load_messages()로 프로그램이 값을 넣는 경우는 suppress_change_notifications()로
        # 억제되어 여기로 도달하지 않는다.
        self._cb_messages_changed = on_messages_changed or (lambda: None)
        self._log = log_callback or (lambda msg: None)

        self._message_textboxes: dict[int, ctk.CTkTextbox] = {}
        # load_messages() 등 프로그램이 값을 채워 넣는 동안 True — 이 동안에는
        # KeyRelease가 아닌 경로로 텍스트가 바뀌므로 원래 발생하지 않지만, 방어적으로
        # 한 번 더 억제한다(초기 로드/클라우드 반영/파일 불러오기 도중 자동저장 오발동 방지).
        self._suppress_change_events = False

        self._create_layout()

    def _create_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=1)

        self._create_button_area()
        self._create_message_header()
        self._create_message_area()

    # ===== 버튼 영역 =====

    def _create_button_area(self) -> None:
        """상단 버튼 3개 영역"""
        btn_frame = ctk.CTkFrame(self, corner_radius=8)
        btn_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        btn_frame.grid_columnconfigure((0, 1, 2), weight=1)

        buttons = [
            ("활성화된 카톡방 가져오기", self._cb_get_active_rooms),
            ("목록 저장",               self._cb_save_list),
            ("저장된 카톡방 켜기",       self._cb_open_saved_rooms),
        ]

        for col, (text, cmd) in enumerate(buttons):
            ctk.CTkButton(
                btn_frame,
                text=text,
                font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                height=BUTTON_HEIGHT,
                command=cmd,
            ).grid(row=0, column=col, padx=8, pady=10, sticky="ew")

    # ===== 메시지 영역 =====

    def _create_message_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 2))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="메시지 설정",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=2)

        btn_frame = ctk.CTkFrame(header, fg_color="transparent")
        btn_frame.grid(row=0, column=1, sticky="e")

        ctk.CTkButton(
            btn_frame,
            text="메시지 저장",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            height=26,
            width=88,
            command=self._cb_save_messages,
        ).grid(row=0, column=0, padx=(0, 6))

        ctk.CTkButton(
            btn_frame,
            text="메시지 불러오기",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            height=26,
            width=100,
            command=self._cb_open_messages,
        ).grid(row=0, column=1)

    def _create_message_area(self) -> None:
        self.msg_scroll = ctk.CTkScrollableFrame(self, corner_radius=8)
        self.msg_scroll.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.msg_scroll.grid_columnconfigure(0, weight=1)

        self._setup_mousewheel(self.msg_scroll)

        row_idx = 0
        for group_info in MESSAGE_GROUPS.values():
            row_idx = self._create_group_section(group_info, row_idx)

        ctk.CTkLabel(self.msg_scroll, text="", height=16).grid(row=row_idx, column=0)

    def _create_group_section(self, group_info: dict, start_row: int) -> int:
        row = start_row

        ctk.CTkLabel(
            self.msg_scroll,
            text=f"◆ {group_info['label']}  ─  매시간 {group_info['minute']}분 발송",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            text_color="#5BA4E8",
        ).grid(row=row, column=0, sticky="w", padx=12, pady=(18, 4))
        row += 1

        ctk.CTkFrame(self.msg_scroll, height=1, fg_color="gray30").grid(
            row=row, column=0, sticky="ew", padx=12, pady=(0, 8)
        )
        row += 1

        for msg_num in group_info["messages"]:
            ctk.CTkLabel(
                self.msg_scroll,
                text=f"메시지 {msg_num}",
                font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            ).grid(row=row, column=0, sticky="w", padx=12, pady=(0, 2))
            row += 1

            textbox = ctk.CTkTextbox(
                self.msg_scroll,
                height=MESSAGE_INPUT_HEIGHT,
                font=ctk.CTkFont(family=FONT_FAMILY, size=11),
                wrap="word",
            )
            textbox.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 10))
            # KeyRelease는 실제 키보드 입력에서만 발생한다(.insert()/.delete() 같은
            # 프로그램적 값 변경으로는 발생하지 않음) — StringVar.trace_add를 쓸 수
            # 없는 CTkTextbox(내부적으로 StringVar가 없는 tkinter Text 위젯)에서
            # "사용자가 실제로 입력했는지"를 감지하는 가장 안전한 방법이다.
            textbox.bind("<KeyRelease>", self._on_textbox_key_release)
            self._message_textboxes[msg_num] = textbox
            row += 1

        return row

    def _setup_mousewheel(self, scroll_frame: ctk.CTkScrollableFrame) -> None:
        def on_enter(_e):
            self.winfo_toplevel().bind(
                "<MouseWheel>",
                lambda e: scroll_frame._parent_canvas.yview_scroll(
                    int(-1 * (e.delta / 120)), "units"
                ),
            )
        def on_leave(_e):
            self.winfo_toplevel().unbind("<MouseWheel>")
        scroll_frame.bind("<Enter>", on_enter)
        scroll_frame.bind("<Leave>", on_leave)

    # ===== 변경 감지 =====

    def _on_textbox_key_release(self, event=None) -> None:
        if self._suppress_change_events:
            return
        self._cb_messages_changed()

    @contextlib.contextmanager
    def suppress_change_notifications(self):
        """이 블록 안에서의 메시지 값 변경은 on_messages_changed를 호출하지 않는다.

        프로그램이 값을 직접 채워 넣는 모든 경로(초기 로드, 클라우드 pull 반영,
        파일 불러오기)에서 사용해야 한다 — 그렇지 않으면 클라우드 값을 UI에
        반영 → 자동저장 트리거 → 클라우드에 다시 업로드하는 무한 루프가 생길 수 있다.
        """
        prev = self._suppress_change_events
        self._suppress_change_events = True
        try:
            yield
        finally:
            self._suppress_change_events = prev

    # ===== 공개 메서드 =====

    def get_message(self, msg_num: int) -> str:
        tb = self._message_textboxes.get(msg_num)
        return tb.get("1.0", "end-1c") if tb else ""

    def set_message(self, msg_num: int, text: str) -> None:
        tb = self._message_textboxes.get(msg_num)
        if tb:
            tb.delete("1.0", "end")
            tb.insert("1.0", text)

    def get_all_messages(self) -> dict[int, str]:
        return {n: self.get_message(n) for n in range(1, MESSAGE_COUNT + 1)}

    def load_messages(self, messages: dict[int, str]) -> None:
        """프로그램이 메시지 값을 채워 넣는 유일한 진입점 — 항상 변경 알림을
        억제한 채로 실행된다(초기 로드/클라우드 pull 반영/파일 불러오기 공용)."""
        with self.suppress_change_notifications():
            for num, text in messages.items():
                self.set_message(num, text)
