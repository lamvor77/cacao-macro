# 왼쪽 단톡방 목록 패널
# 체크박스 형태로 단톡방 목록을 표시하며 발송 대상을 선택하는 영역이다.

import customtkinter as ctk
from config.settings import ROOM_LIST_PANEL_WIDTH, FONT_FAMILY


class RoomListPanel(ctk.CTkFrame):
    """체크박스 형태의 단톡방 목록을 표시하는 왼쪽 패널"""

    def __init__(self, parent, **kwargs):
        super().__init__(parent, width=ROOM_LIST_PANEL_WIDTH, **kwargs)
        self.grid_propagate(False)  # 폭 고정

        # 단톡방 이름 → 체크박스 BooleanVar 매핑
        self._room_vars: dict[str, ctk.BooleanVar] = {}
        self._checkboxes: list[ctk.CTkCheckBox] = []

        self._create_layout()

    def _create_layout(self) -> None:
        """레이아웃 생성"""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)  # 헤더
        self.grid_rowconfigure(1, weight=0)  # 구분선
        self.grid_rowconfigure(2, weight=1)  # 스크롤 영역

        self._create_header()
        self._create_separator()
        self._create_scroll_area()

    def _create_header(self) -> None:
        """헤더 레이블"""
        ctk.CTkLabel(
            self,
            text="단톡방 목록",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))

    def _create_separator(self) -> None:
        """헤더 아래 구분선"""
        ctk.CTkFrame(self, height=1, fg_color="gray30").grid(
            row=1, column=0, sticky="ew", padx=8, pady=(0, 4)
        )

    def _create_scroll_area(self) -> None:
        """체크박스 목록을 담는 스크롤 가능한 프레임"""
        self.scroll_frame = ctk.CTkScrollableFrame(self, corner_radius=6)
        self.scroll_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.scroll_frame.grid_columnconfigure(0, weight=1)

        # 마우스 휠 스크롤 활성화
        self._setup_mousewheel(self.scroll_frame)

        # 초기 빈 상태 안내 메시지
        self.empty_label = ctk.CTkLabel(
            self.scroll_frame,
            text="목록이 비어 있습니다.\n\n'활성화된 카톡방 가져오기' 또는\n'저장된 목록 불러오기'를\n사용하세요.",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color="gray55",
            justify="center",
        )
        self.empty_label.grid(row=0, column=0, padx=10, pady=30)

    def _setup_mousewheel(self, scroll_frame: ctk.CTkScrollableFrame) -> None:
        """마우스가 스크롤 영역 위에 있을 때 휠 스크롤을 활성화한다."""
        def on_enter(_event) -> None:
            self.winfo_toplevel().bind(
                "<MouseWheel>",
                lambda e: scroll_frame._parent_canvas.yview_scroll(
                    int(-1 * (e.delta / 120)), "units"
                ),
            )

        def on_leave(_event) -> None:
            self.winfo_toplevel().unbind("<MouseWheel>")

        scroll_frame.bind("<Enter>", on_enter)
        scroll_frame.bind("<Leave>", on_leave)

    # ===== 공개 메서드 =====

    def add_room(self, room_name: str, checked: bool = True) -> None:
        """단톡방을 목록에 추가한다. 이미 존재하면 무시한다."""
        if room_name in self._room_vars:
            return

        # 안내 메시지 숨기기
        self.empty_label.grid_remove()

        var = ctk.BooleanVar(value=checked)
        self._room_vars[room_name] = var

        checkbox = ctk.CTkCheckBox(
            self.scroll_frame,
            text=room_name,
            variable=var,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        )
        checkbox.grid(
            row=len(self._checkboxes) + 1,
            column=0,
            sticky="w",
            padx=10,
            pady=3,
        )
        self._checkboxes.append(checkbox)

    def clear_rooms(self) -> None:
        """단톡방 목록을 초기화한다."""
        for cb in self._checkboxes:
            cb.destroy()
        self._checkboxes.clear()
        self._room_vars.clear()
        # 안내 메시지 다시 표시
        self.empty_label.grid(row=0, column=0, padx=10, pady=30)

    def load_rooms(self, rooms_data: list[dict]) -> None:
        """저장된 데이터로 단톡방 목록을 복원한다.

        Args:
            rooms_data: [{"name": str, "checked": bool}, ...] 형태의 리스트
        """
        self.clear_rooms()
        for room in rooms_data:
            self.add_room(room["name"], room.get("checked", True))

    def get_checked_rooms(self) -> list[str]:
        """체크된 단톡방 이름 목록을 반환한다."""
        return [name for name, var in self._room_vars.items() if var.get()]

    def get_all_rooms(self) -> dict[str, bool]:
        """모든 단톡방 이름과 체크 상태를 반환한다."""
        return {name: var.get() for name, var in self._room_vars.items()}

    def has_rooms(self) -> bool:
        """단톡방이 하나 이상 등록되어 있으면 True를 반환한다."""
        return len(self._room_vars) > 0
