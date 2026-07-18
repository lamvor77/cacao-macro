# 메인 윈도우
# 전체 레이아웃을 구성하고 각 패널을 조합한다.
# 버튼 이벤트를 받아 카카오톡 제어 / 저장 / 자동 발송 로직을 스레드로 실행한다.

import logging
import os
import threading
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
import tkinter.simpledialog as simpledialog
from datetime import datetime
from typing import Optional

import customtkinter as ctk

from config.cloud_settings import get_message_sync_config
from config.settings import (
    BOTTOM_BAR_HEIGHT, BUTTON_HEIGHT, FONT_FAMILY,
    LICENSE_ADMIN_UI_ENABLED,
    OPERATION_END_HOUR, OPERATION_START_HOUR,
    TAB_ACTIVE_ROOMS, TAB_BULK_MESSAGE,
    WINDOW_DEFAULT_HEIGHT, WINDOW_DEFAULT_WIDTH,
    WINDOW_MIN_HEIGHT, WINDOW_MIN_WIDTH, WINDOW_TITLE,
)
from core.kakao_controller import KakaoController, KakaoNotRunningError
from core.license_manager import LicenseManager
from core.scheduler import AutoScheduler
from storage.data_manager import DataManager
from gui.panels.admin_panel import open_admin_panel
from gui.panels.control_panel import ControlPanel
from gui.panels.log_panel import LogPanel
from gui.panels.room_list_panel import RoomListPanel
from services.auth_service import AuthResult, AuthService
from services.cloud_state import CloudState, CloudStatusInfo
from services.cloud_sync_coordinator import CloudSyncCoordinator
from utils.logger_setup import UILogHandler

logger = logging.getLogger(__name__)


class MainWindow(ctk.CTk):
    """프로그램의 메인 윈도우"""

    def __init__(self, log_file: str = ""):
        super().__init__()

        self._log_file = log_file
        self._kakao = KakaoController()
        self._data = DataManager()
        self._license_mgr = LicenseManager()
        self._scheduler = AutoScheduler(
            get_rooms_fn=self._get_rooms,
            get_messages_fn=self._get_messages,
            log_fn=self._on_log,
        )

        # ===== Phase 2E: 로컬 자동 저장 =====
        self._msg_sync_config = get_message_sync_config()
        self._local_autosave_job: Optional[str] = None  # self.after()가 반환한 예약 ID
        self._closing = False  # 종료 처리 시작 후에는 새 자동저장 예약을 하지 않는다

        self._setup_window()
        self._create_layout()
        self._attach_ui_log_handler()
        self._show_startup_info()

        # ===== Phase 2C/2D: 클라우드 동기화 + 로그인 연결 =====
        # ControlPanel이 만들어진 뒤에만 구성할 수 있으므로 _create_layout() 이후에 둔다.
        # CloudSyncCoordinator는 SUPABASE_ENABLED=false(기본값)면 아무 네트워크 시도도
        # 하지 않으므로, 이 블록은 기존 로컬 전용 동작에 전혀 영향을 주지 않는다.
        # AuthService는 MainWindow와 CloudSyncCoordinator가 하나의 인스턴스를 공유한다
        # (로그인/로그아웃 상태를 한 곳에서만 관리하기 위함).
        self._auth = AuthService()
        self._cloud = CloudSyncCoordinator(
            get_messages_fn=self._get_messages,
            apply_messages_fn=self.control_panel.load_messages,
            log_fn=self._on_log,
            status_fn=self._on_cloud_status,
            notify_scheduler_fn=self._scheduler.notify_cloud_update,
            auth_service=self._auth,
        )
        self._start_cloud_sync()
        # _cloud.start()와 같은 이유로 self.after()를 통해 mainloop 진입 이후로 미룬다 —
        # 이 메서드도 내부적으로 백그라운드 스레드를 띄우고 그 스레드가 self.after()를
        # 호출하므로, mainloop 시작 전에 실행되면 동일한 경쟁 상태가 재현된다.
        self.after(100, self._refresh_auth_ui_async)

    # ===== 초기 설정 =====

    def _setup_window(self) -> None:
        self.title(WINDOW_TITLE)
        self.geometry(f"{WINDOW_DEFAULT_WIDTH}x{WINDOW_DEFAULT_HEIGHT}")
        self.minsize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.protocol("WM_DELETE_WINDOW", self._on_exit)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self.grid_rowconfigure(3, weight=0)

    def _attach_ui_log_handler(self) -> None:
        """WARNING 이상 Python logging 메시지를 UI 로그창에도 출력한다."""
        ui_handler = UILogHandler(self._on_log)
        logging.getLogger().addHandler(ui_handler)

    def _show_startup_info(self) -> None:
        """시작 시 운영 정보를 로그창에 출력한다."""
        self._on_log(f"[INFO] 프로그램 시작 — 운영 시간: {OPERATION_START_HOUR:02d}:00 ~ {OPERATION_END_HOUR:02d}:00")
        if self._log_file:
            self._on_log(f"[INFO] 로그 파일: {self._log_file}")

    # ===== 레이아웃 =====

    def _create_layout(self) -> None:
        self._create_title_bar()
        self._create_tab_view()
        self._create_log_panel()
        self._create_bottom_bar()

    def _create_title_bar(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0, height=50)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)  # 좌측 여백 (제목 중앙 정렬용)
        bar.grid_columnconfigure(1, weight=0)  # 제목 라벨
        bar.grid_columnconfigure(2, weight=1)  # 우측 여백 + 관리자 버튼

        ctk.CTkLabel(
            bar,
            text=WINDOW_TITLE,
            font=ctk.CTkFont(family=FONT_FAMILY, size=18, weight="bold"),
        ).grid(row=0, column=1, pady=12)

        right_frame = ctk.CTkFrame(bar, fg_color="transparent")
        right_frame.grid(row=0, column=2, padx=(0, 16), pady=12, sticky="e")

        # 클라우드 동기화 상태 — 작은 라벨 하나만 사용한다(팝업/다이얼로그 없음).
        self.cloud_status_label = ctk.CTkLabel(
            right_frame,
            text="☁ 클라우드 미설정",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color="gray60",
        )
        self.cloud_status_label.grid(row=0, column=0, padx=(0, 10))

        # 로그인 사용자 표시(로그인 안 됐으면 빈 문자열) — 별도 팝업 없이 라벨만 갱신.
        self.auth_label = ctk.CTkLabel(
            right_frame,
            text="",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color="gray60",
        )
        self.auth_label.grid(row=0, column=1, padx=(0, 8))

        self.auth_button = ctk.CTkButton(
            right_frame,
            text="Google로 로그인",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            width=110,
            height=28,
            fg_color="transparent",
            border_width=1,
            command=self._on_login_click,
        )
        self.auth_button.grid(row=0, column=2, padx=(0, 10))

        # 라이선스 관리자(빌드용 라이선스 발급) 버튼 — 개발/빌드 담당자 환경에서만
        # 노출한다(Phase 3-2). LICENSE_ADMIN_UI_ENABLED=true이고 LICENSE_ADMIN_PASSWORD/
        # LICENSE_SECRET_KEY가 모두 실제 값으로 설정되어 있을 때만 버튼 자체를
        # 만든다 — 일반 사용자 배포본(기본값 false)에는 아예 표시되지 않는다.
        if LICENSE_ADMIN_UI_ENABLED and self._license_mgr.is_fully_configured():
            ctk.CTkButton(
                right_frame,
                text="라이선스 관리자",
                font=ctk.CTkFont(family=FONT_FAMILY, size=12),
                width=100,
                height=28,
                fg_color="transparent",
                border_width=1,
                command=self._on_open_admin_panel,
            ).grid(row=0, column=3)

    def _create_tab_view(self) -> None:
        self.tab_view = ctk.CTkTabview(self)
        self.tab_view.grid(row=1, column=0, sticky="nsew", padx=10, pady=(6, 4))

        tab_active = self.tab_view.add(TAB_ACTIVE_ROOMS)
        tab_bulk = self.tab_view.add(TAB_BULK_MESSAGE)

        self._setup_active_rooms_tab(tab_active)
        self._setup_bulk_message_tab(tab_bulk)

    def _setup_active_rooms_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=0)
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        self.room_list_panel = RoomListPanel(tab)
        self.room_list_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        self.control_panel = ControlPanel(
            tab,
            on_get_active_rooms=self._on_get_active_rooms,
            on_save_list=self._on_save_list,
            on_open_saved_rooms=self._on_open_saved_rooms,
            on_save_messages=self._on_save_messages,
            on_open_messages=self._on_open_messages,
            on_messages_changed=self._on_messages_changed,
            log_callback=self._on_log,
        )
        self.control_panel.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

    def _setup_bulk_message_tab(self, tab: ctk.CTkFrame) -> None:
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        ctk.CTkLabel(
            tab,
            text="대량 메시지 전송 기능은 추후 구현 예정입니다.",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            text_color="gray55",
        ).grid(row=0, column=0)

    def _create_log_panel(self) -> None:
        self.log_panel = LogPanel(self)
        self.log_panel.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))

    def _create_bottom_bar(self) -> None:
        bar = ctk.CTkFrame(self, corner_radius=0, height=BOTTOM_BAR_HEIGHT)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)
        bar.grid_columnconfigure(1, weight=1)

        self.btn_start = ctk.CTkButton(
            bar,
            text="▶   시작",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            height=BUTTON_HEIGHT,
            fg_color="#2E7D32",
            hover_color="#1B5E20",
            command=self._on_toggle_start,
        )
        self.btn_start.grid(row=0, column=0, padx=(20, 8), pady=12, sticky="ew")

        self.btn_exit = ctk.CTkButton(
            bar,
            text="■   종료",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
            height=BUTTON_HEIGHT,
            fg_color="#B71C1C",
            hover_color="#7F0000",
            command=self._on_exit,
        )
        self.btn_exit.grid(row=0, column=1, padx=(8, 20), pady=12, sticky="ew")

    # ===== 공통 헬퍼 =====

    def _on_log(self, message: str) -> None:
        """스레드에서 호출해도 안전한 로그 출력 (UI + 파일)."""
        self.after(0, lambda: self.log_panel.log(message))

    def _apply_loaded_data(self, data: dict) -> None:
        self.room_list_panel.load_rooms(data.get("rooms", []))
        self.control_panel.load_messages(data.get("messages", {}))

    def _run_in_thread(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _get_rooms(self) -> dict[str, bool]:
        return self.room_list_panel.get_all_rooms()

    def _get_messages(self) -> dict[int, str]:
        return self.control_panel.get_all_messages()

    # ===== 로컬 자동 저장 (Phase 2E) =====

    def _on_messages_changed(self) -> None:
        """ControlPanel이 실제 키 입력을 감지했을 때 호출한다(메인 스레드).

        연속 입력 시 마지막 입력만 저장되도록 이전 예약을 취소하고 다시 건다
        (디바운스). 디바운스가 아직 끝나지 않은 구간에는 폴링이 클라우드 값을
        조용히 덮어쓰지 않도록 coordinator에 알린다.
        """
        if self._closing or not self._msg_sync_config.local_autosave_enabled:
            return
        self._cloud.set_input_in_progress(True)
        if self._local_autosave_job is not None:
            self.after_cancel(self._local_autosave_job)
        self._local_autosave_job = self.after(
            self._msg_sync_config.local_autosave_delay_ms, self._trigger_local_autosave
        )

    def _trigger_local_autosave(self) -> None:
        """디바운스 종료 후(메인 스레드) 호출된다 — 실제 파일 I/O는 스레드로 넘긴다."""
        self._local_autosave_job = None
        messages = self.control_panel.get_all_messages()
        self._run_in_thread(lambda: self._run_local_autosave(messages))

    def _run_local_autosave(self, messages: dict) -> None:
        self._cloud.notify_local_autosave(messages)
        self.after(0, lambda: self._cloud.set_input_in_progress(False))

    def _flush_local_autosave_now(self) -> None:
        """대기 중인 디바운스를 취소하고 즉시(동기적으로) 로컬 저장을 완료한다.

        발송 시작 직전/프로그램 종료 직전처럼 "저장이 반드시 끝난 뒤"에만
        다음 단계로 진행해야 하는 지점에서 사용한다. 파일 쓰기 자체는 짧은
        atomic write라 메인 스레드에서 동기 호출해도 체감 지연이 없다.
        """
        if self._local_autosave_job is not None:
            self.after_cancel(self._local_autosave_job)
            self._local_autosave_job = None
        messages = self.control_panel.get_all_messages()
        self._cloud.notify_local_autosave(messages)
        self._cloud.set_input_in_progress(False)

    # ===== 클라우드 동기화 (Phase 2C) =====

    def _start_cloud_sync(self) -> None:
        """로컬 캐시를 즉시 ControlPanel에 반영한 뒤, 클라우드 동기화를 백그라운드로 시작한다.

        load_local_and_apply()는 파일 I/O만 하는 동기 호출이라 빠르다 — 네트워크
        요청(초기 동기화, polling)은 coordinator.start()가 내부적으로 별도
        스레드에서 수행하므로 이 메서드도, 프로그램 시작 자체도 지연되지 않는다.

        coordinator.start() 자체는 self.after()로 한 박자 늦춰 호출한다 — __init__이
        실행되는 시점에는 아직 self.mainloop()가 시작되지 않았는데, 그 상태에서
        백그라운드 스레드가 곧바로 self.after()(상태 통지용)를 호출하면 Tkinter가
        "main thread is not in main loop" 오류를 낸다(다른 스레드가 mainloop 시작
        전에 Tk를 건드리는 것을 막는 안전장치). after()로 예약해두면 mainloop 진입
        직후에만 스레드가 시작되므로 이 경쟁 상태가 사라진다.
        """
        initial_messages = self._cloud.load_local_and_apply()
        if initial_messages:
            self.control_panel.load_messages(initial_messages)
        self.after(100, self._cloud.start)

    def _on_cloud_status(self, info: CloudStatusInfo) -> None:
        """CloudSyncCoordinator가 백그라운드 스레드에서 호출한다 — 메인 스레드로 넘긴다."""
        self.after(0, lambda: self._apply_cloud_status(info))

    def _apply_cloud_status(self, info: CloudStatusInfo) -> None:
        """상태 라벨 문구/색만 갱신한다 — 팝업이나 다이얼로그는 띄우지 않는다."""
        color_by_state = {
            CloudState.CONNECTED: "#4CAF50",
            CloudState.CONNECTED_READ_ONLY: "#4CAF50",
            CloudState.SYNCING: "#5BA4E8",
            CloudState.CONNECTING: "#5BA4E8",
            CloudState.OFFLINE: "gray60",
            CloudState.SYNC_FAILED: "#E65100",
            CloudState.CONFLICT: "#E65100",
            CloudState.LOGIN_REQUIRED: "gray60",
            CloudState.NOT_CONFIGURED: "gray60",
            CloudState.APPROVAL_PENDING: "#E6A100",
            CloudState.BLOCKED: "#B71C1C",
        }
        self.cloud_status_label.configure(
            text=f"☁ {info.label_ko}",
            text_color=color_by_state.get(info.state, "gray60"),
        )

    # ===== Google 로그인 / 로그아웃 (Phase 2D) =====

    def _refresh_auth_ui_async(self) -> None:
        """백그라운드 스레드에서 로그인 상태를 확인한 뒤 UI를 갱신한다.

        is_logged_in()/get_current_user()는 세션이 만료돼 있으면 내부적으로
        refresh를 시도하는 등 네트워크 호출을 포함할 수 있으므로, 반드시
        별도 스레드에서 실행하고 위젯 갱신만 self.after()로 메인 스레드에 넘긴다.
        """
        self._run_in_thread(self._check_and_apply_auth_ui)

    def _check_and_apply_auth_ui(self) -> None:
        logged_in = self._auth.is_logged_in()
        email = ""
        if logged_in:
            user = self._auth.get_current_user()
            email = user.email if user else ""
        self.after(0, lambda: self._apply_auth_ui(logged_in, email))

    def _apply_auth_ui(self, logged_in: bool, email: str) -> None:
        if logged_in:
            self.auth_label.configure(text=email or "로그인됨")
            self.auth_button.configure(text="로그아웃", command=self._on_logout_click)
        else:
            self.auth_label.configure(text="")
            self.auth_button.configure(text="Google로 로그인", command=self._on_login_click)

    def _on_login_click(self) -> None:
        self.auth_button.configure(state="disabled", text="로그인 중...")
        self._run_in_thread(self._do_login)

    def _do_login(self) -> None:
        result = self._auth.login_with_google()
        self.after(0, lambda: self._on_login_result(result))

    def _on_login_result(self, result: AuthResult) -> None:
        self.auth_button.configure(state="normal")
        if result.success:
            self._on_log("[INFO] Google 로그인 성공")
            self._cloud.on_login_success()
        else:
            self._on_log(f"[오류] Google 로그인 실패: {result.error}")
            # 로그인 버튼 클릭의 결과이므로(치명적 오류) 1회만 팝업으로 알린다 —
            # 반복 팝업을 띄우지 않는다는 원칙과 배치되지 않는다.
            messagebox.showerror("로그인 실패", result.error or "알 수 없는 오류가 발생했습니다.")
        self._refresh_auth_ui_async()

    def _on_logout_click(self) -> None:
        self.auth_button.configure(state="disabled")
        self._run_in_thread(self._do_logout)

    def _do_logout(self) -> None:
        self._auth.logout()
        self._cloud.on_logout()
        self._on_log("[INFO] 로그아웃 완료")
        self.after(0, self._refresh_auth_ui_async)

    # ===== 시작/중지 토글 =====

    def _on_toggle_start(self) -> None:
        if not self._scheduler.is_running():
            self._start_scheduler()
        else:
            self._stop_scheduler()

    def _start_scheduler(self) -> None:
        """자동 발송을 시작한다. 사전 조건을 확인하고 운영 시간 외 경고를 표시한다."""
        # 체크된 단톡방 확인
        if not self.room_list_panel.get_checked_rooms():
            self._on_log("[경고] 체크된 단톡방이 없습니다. 단톡방을 선택한 후 시작하세요.")
            return

        # 메시지 입력 여부 확인
        messages = self.control_panel.get_all_messages()
        if not any(v.strip() for v in messages.values()):
            self._on_log("[경고] 입력된 메시지가 없습니다. 메시지를 입력한 후 시작하세요.")
            return

        # 운영 시간 외 경고 (시작은 허용 — 운영 시간이 되면 자동 발송)
        now = datetime.now()
        if not (OPERATION_START_HOUR <= now.hour < OPERATION_END_HOUR):
            self._on_log(
                f"[경고] 현재 운영 시간({OPERATION_START_HOUR:02d}:00~{OPERATION_END_HOUR:02d}:00) 외입니다. "
                "운영 시간이 되면 자동으로 발송을 시작합니다."
            )

        # Phase 2E: 발송 시작 직전 — 로컬 dirty면 동기적으로 저장(1), cloud_dirty이면
        # 비동기로 즉시 업로드 요청(3)한다. 업로드 완료는 기다리지 않는다(4) — 아래
        # scheduler.start()는 이 호출과 무관하게 곧바로 진행된다. 실제 발송에 쓰이는
        # 메시지 스냅샷은 AutoScheduler._send_group()이 그룹 시작 시점에 별도로 뜬다(5).
        self._flush_local_autosave_now()
        if self._msg_sync_config.cloud_sync_on_send_start:
            self._cloud.request_push(messages=dict(messages), reason="send_start")
            self._on_log("[INFO] 발송 시작 전 클라우드 동기화 요청")

        self._scheduler.start()
        self.btn_start.configure(
            text="■   중지",
            fg_color="#E65100",
            hover_color="#BF360C",
        )

    def _stop_scheduler(self) -> None:
        self._scheduler.stop()
        self.btn_start.configure(
            text="▶   시작",
            fg_color="#2E7D32",
            hover_color="#1B5E20",
        )

    # ===== 버튼 핸들러 =====

    def _on_open_admin_panel(self) -> None:
        """관리자 모드 버튼 — 비밀번호 확인 후 라이선스 발급 패널을 연다."""
        open_admin_panel(self, self._license_mgr, self._on_log)

    def _on_get_active_rooms(self) -> None:
        """열려있는 카카오톡 채팅방 창 목록을 가져와 왼쪽 목록에 추가한다."""
        def run():
            try:
                self._on_log("[INFO] 카카오톡 열린 채팅방 목록 가져오는 중...")

                if not self._kakao.is_running():
                    self._on_log("[오류] 카카오톡이 실행 중이지 않습니다.")
                    return

                rooms = self._kakao.get_open_chat_rooms()
                if not rooms:
                    self._on_log("[INFO] 현재 열린 채팅방 창이 없습니다.")
                    return

                def update_ui():
                    self.room_list_panel.clear_rooms()
                    for name in rooms:
                        self.room_list_panel.add_room(name, checked=True)
                    self._on_log(f"[INFO] {len(rooms)}개 채팅방 가져오기 완료: {', '.join(rooms)}")

                self.after(0, update_ui)

            except KakaoNotRunningError as e:
                self._on_log(f"[오류] {e}")
            except Exception as e:
                logger.exception("활성화된 카톡방 가져오기 오류")
                self._on_log(f"[오류] 예상치 못한 오류: {e}")

        self._run_in_thread(run)

    def _on_save_list(self) -> None:
        """파일명 입력 다이얼로그 → storage/{이름}.json 으로 저장한다."""
        filename = simpledialog.askstring(
            "목록 저장",
            "저장할 파일 이름을 입력하세요\n(확장자 제외):",
            parent=self,
        )
        if not filename or not filename.strip():
            return

        filename = filename.strip()

        def run():
            try:
                rooms = self.room_list_panel.get_all_rooms()
                messages = self.control_panel.get_all_messages()

                if not rooms:
                    self._on_log("[경고] 저장할 단톡방이 없습니다.")
                    return

                filepath = self._data.make_filepath(filename)
                self._data.save(rooms, messages, filepath)
                self._cloud.notify_local_save(messages)

                filled = sum(1 for v in messages.values() if v.strip())
                self._on_log(
                    f"[INFO] 저장 완료 — '{filename}.json' "
                    f"(단톡방 {len(rooms)}개, 메시지 {filled}개)"
                )
            except PermissionError:
                self._on_log(f"[오류] 파일 저장 실패 — '{filename}.json' 파일이 다른 프로그램에서 열려 있습니다.")
            except Exception as e:
                logger.exception("목록 저장 오류")
                self._on_log(f"[오류] 저장 실패: {e}")

        self._run_in_thread(run)

    def _on_open_saved_rooms(self) -> None:
        """저장된 파일 선택 → 단톡방 목록 로드 → 카카오톡에서 자동으로 열기."""
        storage_dir = self._data.get_storage_dir()
        filepath = filedialog.askopenfilename(
            title="저장된 카톡방 목록 선택",
            initialdir=storage_dir,
            filetypes=[("JSON 파일", "*.json"), ("모든 파일", "*.*")],
            parent=self,
        )
        if not filepath:
            return

        # 새 목록을 불러오기 전, 기존에 표시되어 있던 단톡방 목록을 먼저 초기화한다.
        self.room_list_panel.clear_rooms()

        def run():
            try:
                # 파일 로드
                data = self._data.load(filepath)
                if data is None:
                    self._on_log(f"[오류] 파일을 불러올 수 없습니다: {os.path.basename(filepath)}")
                    return

                rooms_data = data.get("rooms", [])
                if not rooms_data:
                    self._on_log("[경고] 파일에 저장된 단톡방이 없습니다.")
                    return

                filename = os.path.basename(filepath)
                self._on_log(f"[INFO] '{filename}' 로드 완료 — 단톡방 {len(rooms_data)}개")

                # UI 업데이트
                self.after(0, lambda: self._apply_loaded_data(data))

                # 카카오톡 실행 확인
                if not self._kakao.is_running():
                    self._on_log("[오류] 카카오톡이 실행 중이지 않습니다. 단톡방 목록만 불러왔습니다.")
                    return

                # 검색창 상태 초기화 (이전 세션의 Ctrl+F 상태 리셋)
                self._kakao.reset_search_state()

                # 이미 열린 채팅방 확인 후 순서대로 열기
                import time
                already_open = self._kakao.get_open_chat_rooms()
                self._on_log(f"[INFO] 카톡방 열기 시작 — 총 {len(rooms_data)}개")

                for room in rooms_data:
                    room_name = room["name"]

                    if room_name in already_open:
                        self._on_log(f"  [SKIP] 이미 열려있음: {room_name}")
                        continue

                    self._on_log(f"  [열기] {room_name}")
                    success = self._kakao.open_room(room_name)
                    self._on_log(f"  [{'완료' if success else '실패'}] {room_name}")
                    time.sleep(1.0)

                self._on_log("[INFO] 카톡방 열기 완료")

            except KakaoNotRunningError as e:
                self._on_log(f"[오류] {e}")
            except Exception as e:
                logger.exception("저장된 카톡방 켜기 오류")
                self._on_log(f"[오류] 예상치 못한 오류: {e}")

        self._run_in_thread(run)

    def _on_save_messages(self) -> None:
        """탐색기 저장 다이얼로그로 경로를 선택해 현재 입력된 메시지만 저장한다."""
        storage_dir = self._data.get_storage_dir()
        filepath = filedialog.asksaveasfilename(
            title="메시지 저장",
            initialdir=storage_dir,
            defaultextension=".json",
            filetypes=[("JSON 파일", "*.json"), ("모든 파일", "*.*")],
            parent=self,
        )
        if not filepath:
            return

        def run():
            try:
                messages = self.control_panel.get_all_messages()
                self._data.save_messages(messages, filepath)
                self._cloud.notify_local_save(messages)

                filled = sum(1 for v in messages.values() if v.strip())
                self._on_log(
                    f"[INFO] 메시지 저장 완료 — '{os.path.basename(filepath)}' (메시지 {filled}개)"
                )
            except PermissionError:
                self._on_log(
                    f"[오류] 메시지 저장 실패 — '{os.path.basename(filepath)}' 파일이 다른 프로그램에서 열려 있습니다."
                )
            except Exception as e:
                logger.exception("메시지 저장 오류")
                self._on_log(f"[오류] 메시지 저장 실패: {e}")

        self._run_in_thread(run)

    def _on_open_messages(self) -> None:
        """탐색기 열기 다이얼로그로 파일을 선택해 메시지를 불러온다."""
        storage_dir = self._data.get_storage_dir()
        filepath = filedialog.askopenfilename(
            title="메시지 불러오기",
            initialdir=storage_dir,
            filetypes=[("JSON 파일", "*.json"), ("모든 파일", "*.*")],
            parent=self,
        )
        if not filepath:
            return

        def run():
            try:
                data = self._data.load(filepath)
                if data is None:
                    self._on_log(f"[오류] 파일을 불러올 수 없습니다: {os.path.basename(filepath)}")
                    return

                messages = data.get("messages", {})
                if not messages:
                    self._on_log("[경고] 파일에 저장된 메시지가 없습니다.")
                    return

                self.after(0, lambda: self.control_panel.load_messages(messages))

                filled = sum(1 for v in messages.values() if v.strip())
                self._on_log(
                    f"[INFO] 메시지 불러오기 완료 — '{os.path.basename(filepath)}' (메시지 {filled}개)"
                )
            except Exception as e:
                logger.exception("메시지 불러오기 오류")
                self._on_log(f"[오류] 메시지 불러오기 실패: {e}")

        self._run_in_thread(run)

    def _on_exit(self) -> None:
        """스케줄러와 클라우드 동기화를 중지하고 프로그램을 종료한다.

        Phase 2E: 종료 전 로컬 dirty를 동기적으로 저장하고(반드시 보장), cloud_dirty이면
        제한된 시간(MESSAGE_CLOUD_SYNC_EXIT_WAIT_SECONDS) 안에서만 업로드를 시도한다.
        강제 종료/전원 차단까지 보장하지는 않는다 — 정상 종료 경로에 한한다.
        """
        self._on_log("[INFO] 프로그램 종료 처리 시작")
        self._closing = True  # 이후 새 자동저장 예약을 막는다
        if self._scheduler.is_running():
            self._scheduler.stop()

        self._flush_local_autosave_now()
        self._on_log("[INFO] 종료 전 로컬 저장 완료")
        if self._msg_sync_config.cloud_sync_on_exit:
            self._cloud.shutdown_flush(timeout_seconds=self._msg_sync_config.cloud_sync_exit_wait_seconds)

        self._cloud.stop(wait_seconds=2.0)
        self.after(200, self.destroy)
