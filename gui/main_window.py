# 메인 윈도우
# 전체 레이아웃을 구성하고 각 패널을 조합한다.
# 버튼 이벤트를 받아 카카오톡 제어 / 저장 / 자동 발송 로직을 스레드로 실행한다.

import logging
import os
import threading
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
from datetime import datetime
from typing import Optional

import customtkinter as ctk

from config.cloud_settings import get_message_sync_config
from config.version import APP_VERSION, version_summary
from config.settings import (
    BOTTOM_BAR_HEIGHT, BUTTON_HEIGHT, FONT_FAMILY,
    IS_TEST_ENVIRONMENT, LICENSE_ADMIN_UI_ENABLED,
    OPERATION_END_HOUR, OPERATION_START_HOUR,
    TAB_ACTIVE_ROOMS, TAB_BULK_MESSAGE,
    WINDOW_DEFAULT_HEIGHT, WINDOW_DEFAULT_WIDTH,
    WINDOW_MIN_HEIGHT, WINDOW_MIN_WIDTH, WINDOW_TITLE,
)
from core.kakao_controller import KakaoController, KakaoNotRunningError
from core.license_manager import LicenseManager
from core.scheduler import AutoScheduler
from core.legacy_send_verification import verify_legacy_message_before_send
from core.send_verification import (
    OfflineSendPolicy, SendMessageVerificationResult, VerificationErrorCode,
    VerificationSource, verify_message_before_send,
)
from core.shared_message_coordinator import (
    MessageSyncStatus, RemoteMessageSnapshot, SharedMessageCoordinator,
)
from storage.data_manager import DataManager, default_room_list_filename
from gui.panels.admin_panel import open_admin_panel
from gui.panels.admin_ui_state import is_operations_admin_menu_visible
from gui.panels.backup_panel import BackupPanel
from gui.panels.control_panel import ControlPanel
from gui.panels.diagnostics_panel import DiagnosticsPanel
from gui.panels.log_panel import LogPanel
from gui.panels.operations_admin_panel import OperationsAdminPanel
from gui.panels.room_list_panel import RoomListPanel
from services.admin_service import AdminService
from services.auth_service import AppUserProfile, AuthResult, AuthService
from services.backup_service import BackupError, BackupService
from services.cloud_state import CloudState, CloudStatusInfo
from services.cloud_sync_coordinator import CloudSyncCoordinator
from services.diagnostics_service import DiagnosticsService
from services.realtime_message_sync_service import (
    MessageChangeEvent, RealtimeConnectionState, RealtimeMessageSyncService,
)
from services.shared_message_service import (
    MAX_MESSAGE_NO, MIN_MESSAGE_NO,
    SharedMessageConflictError, SharedMessageError, SharedMessageRecord,
    SharedMessageService, is_untouched_seed,
)
from utils.logger_setup import UILogHandler

logger = logging.getLogger(__name__)

# ===== Production Stabilization Sprint 11/12절: 메시지 출처 우선순위 =====
# 온라인: shared_messages 서버 최신값 > shared_messages 로컬 캐시(revision 비교로
#   구분하지 않고 "shared_messages 계열"로 통합 표시한다 — 이 앱은 별도의
#   shared_messages 전용 로컬 캐시 파일을 두지 않는다. 대신 revision 기반 상태
#   자체가 core.shared_message_coordinator에 항상 남아있으므로 "서버/캐시"
#   구분보다 "shared_messages 계열이냐 아니냐"가 더 의미 있는 구분이다) >
#   레거시 로컬 메시지 > 프로그램 기본 메시지(빈 문자열).
# 오프라인: shared_messages 마지막 캐시(=coordinator에 남은 마지막 값) > 레거시
#   로컬 메시지 > 프로그램 기본 메시지.
# 실제로는 "무엇을 우선 적용하느냐"가 아니라 "누가 마지막으로 텍스트를 썼는가"로
# 자연스럽게 우선순위가 반영된다 — shared_messages 쪽 콜백은 항상 legacy보다
# 늦게(네트워크 연결 이후) 도착하므로, 결과적으로 shared_messages가 나중에
# 덮어써서 우선순위가 성립한다. docs/legacy_messages_migration_plan.md 참고.
MESSAGE_SOURCE_SHARED_SERVER = "shared_messages_server"
MESSAGE_SOURCE_LEGACY_LOCAL = "legacy_local"
MESSAGE_SOURCE_MANUAL_FILE = "manual_file"
MESSAGE_SOURCE_DEFAULT = "default"


def build_window_title(is_test_environment: bool) -> str:
    """메인 윈도우 창 제목을 만든다 — 버전은 항상 표시하고, TEST ENVIRONMENT
    표시는 .env의 APP_ENV/SUPABASE_ENVIRONMENT가 "test"일 때만 덧붙인다
    (config/settings.py::IS_TEST_ENVIRONMENT). MainWindow를 인스턴스화하지
    않고도 두 환경의 제목 문자열을 그대로 단위 테스트할 수 있도록 분리했다."""
    suffix = " — TEST ENVIRONMENT" if is_test_environment else ""
    return f"{WINDOW_TITLE} — v{APP_VERSION}{suffix}"


def _snapshot_from_record(record: SharedMessageRecord) -> RemoteMessageSnapshot:
    """SharedMessageRecord -> RemoteMessageSnapshot 변환(core.shared_message_coordinator는
    services.shared_message_service를 import하지 않으므로 이 경계에서 변환한다)."""
    return RemoteMessageSnapshot(
        message_no=record.message_no,
        content=record.content,
        revision=record.revision,
        title=record.title,
        updated_by_name=record.updated_by_name or "",
        updated_at=record.updated_at,
        update_source=record.update_source,
    )


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
            verify_message_fn=self._verify_message_before_send,
            get_local_revision_fn=lambda n: self._shared_coordinator.get_state(n).revision,
        )

        # ===== Release Candidate Sprint 1: 자동/수동 백업 =====
        # DataManager.get_storage_dir을 그대로 넘긴다 — 경로 계산 로직을
        # 중복 구현하지 않는다.
        self._backup_service = BackupService(
            storage_dir_fn=self._data.get_storage_dir, app_version=APP_VERSION,
        )
        self._diagnostics_panel: Optional[DiagnosticsPanel] = None
        self._backup_panel: Optional[BackupPanel] = None

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
        # 운영 관리자 서비스(Phase 4-2) — AuthService와 같은 client_manager를 공유해야
        # 로그인 세션이 admin_* RPC 호출에도 반영된다(CloudSyncService를 만들 때와
        # 동일한 이유 — services/cloud_sync_coordinator.py 주석 참고). 새 Supabase
        # client를 만들지 않는다.
        self._admin_service = AdminService(client_manager=self._auth.client_manager)
        self._operations_admin_panel: Optional[OperationsAdminPanel] = None
        self._current_profile: Optional[AppUserProfile] = None

        # ===== Mobile 실시간 동기화 스프린트: shared_messages =====
        # 같은 client_manager를 공유한다(AdminService와 동일한 이유 — 로그인 세션이
        # RPC의 auth.uid()에 반영되어야 한다). 레거시 CloudSyncCoordinator/messages
        # 테이블과는 완전히 별개로 동작한다(요구사항 12 — 기존 기능 유지).
        self._shared_msg_service = SharedMessageService(client_manager=self._auth.client_manager)
        self._shared_coordinator = SharedMessageCoordinator()
        self._realtime_sync: Optional[RealtimeMessageSyncService] = None
        self._migration_prompted = False
        self._last_shared_sync_at: Optional[datetime] = None
        self._last_shared_sync_text: str = ""
        # 요구사항 12절 — 메시지 출처 우선순위 추적(진단정보 화면/로그용).
        self._message_content_source: dict = {
            n: MESSAGE_SOURCE_DEFAULT for n in range(MIN_MESSAGE_NO, MAX_MESSAGE_NO + 1)
        }

        self._cloud = CloudSyncCoordinator(
            get_messages_fn=self._get_messages,
            apply_messages_fn=self._apply_legacy_messages,
            log_fn=self._on_log,
            status_fn=self._on_cloud_status,
            notify_scheduler_fn=self._scheduler.notify_cloud_update,
            auth_service=self._auth,
        )
        self._diagnostics_service = DiagnosticsService(
            auth_service=self._auth,
            cloud_coordinator=self._cloud,
            license_manager=self._license_mgr,
            data_manager=self._data,
            current_profile_fn=lambda: self._current_profile,
            backup_service=self._backup_service,
            message_source_fn=lambda: self._message_content_source,
        )
        self._start_cloud_sync()
        # _cloud.start()와 같은 이유로 self.after()를 통해 mainloop 진입 이후로 미룬다 —
        # 이 메서드도 내부적으로 백그라운드 스레드를 띄우고 그 스레드가 self.after()를
        # 호출하므로, mainloop 시작 전에 실행되면 동일한 경쟁 상태가 재현된다.
        self.after(100, self._refresh_auth_ui_async)
        # Realtime 구독도 자체 백그라운드 스레드(asyncio 이벤트루프)를 띄우므로
        # 동일한 이유로 mainloop 진입 이후로 미룬다.
        self.after(150, self._start_shared_message_sync)

    # ===== 초기 설정 =====

    def _setup_window(self) -> None:
        self.title(build_window_title(IS_TEST_ENVIRONMENT))
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
        self._on_log(f"[INFO] {version_summary()} — 운영 시간: {OPERATION_START_HOUR:02d}:00 ~ {OPERATION_END_HOUR:02d}:00")
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

        title_center = ctk.CTkFrame(bar, fg_color="transparent")
        title_center.grid(row=0, column=1, pady=12)

        ctk.CTkLabel(
            title_center,
            text=WINDOW_TITLE,
            font=ctk.CTkFont(family=FONT_FAMILY, size=18, weight="bold"),
        ).grid(row=0, column=0)

        # Test Environment Deployment & E2E Validation Sprint 1절 — 운영 창과
        # 육안으로 절대 혼동되지 않도록 제목표시줄 정중앙에 눈에 띄는 배지를 둔다.
        # 운영 환경(IS_TEST_ENVIRONMENT=False)에서는 이 라벨 자체를 만들지 않는다.
        if IS_TEST_ENVIRONMENT:
            ctk.CTkLabel(
                title_center,
                text="⚠ TEST ENVIRONMENT",
                font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
                text_color="#FF3B30",
            ).grid(row=0, column=1, padx=(10, 0))

        right_frame = ctk.CTkFrame(bar, fg_color="transparent")
        right_frame.grid(row=0, column=2, padx=(0, 16), pady=12, sticky="e")

        # 클라우드 동기화 상태(레거시 messages 30초 폴링) + 실시간 동기화 상태
        # (shared_messages Realtime)를 같은 칸에 세로로 쌓는다 — 전체 타이틀바
        # 열 구성을 바꾸지 않기 위해 기존 column=0 자리에 작은 서브프레임을 둔다.
        sync_status_frame = ctk.CTkFrame(right_frame, fg_color="transparent")
        sync_status_frame.grid(row=0, column=0, padx=(0, 10))

        self.cloud_status_label = ctk.CTkLabel(
            sync_status_frame,
            text="☁ 클라우드 미설정",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color="gray60",
        )
        self.cloud_status_label.grid(row=0, column=0, sticky="w")

        # Production Stabilization Sprint: shared_messages Realtime 연결 상태를
        # 기술 용어(enum 값) 대신 사용자 친화적 문구로 별도 표시한다(요구사항 8절).
        self.realtime_status_label = ctk.CTkLabel(
            sync_status_frame,
            text="⚡ 실시간 동기화 비활성화",
            font=ctk.CTkFont(family=FONT_FAMILY, size=10),
            text_color="gray60",
        )
        self.realtime_status_label.grid(row=1, column=0, sticky="w")

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

        # 운영 관리자(app_users.role='admin') 버튼 — Phase 4-2. 항상 위젯 자체는
        # 만들되 기본적으로 숨겨두고, 로그인 후 프로필이 확인될 때마다
        # _apply_operations_admin_visibility()가 노출 여부를 다시 판단한다(로그인
        # 상태가 런타임에 바뀌므로 라이선스 관리자 버튼처럼 시작 시 1회만 판단할
        # 수 없다). 기존 로컬 "라이선스 관리자"와는 완전히 별개의 버튼이다.
        self.operations_admin_button = ctk.CTkButton(
            right_frame,
            text="운영 관리자",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            width=100,
            height=28,
            fg_color="transparent",
            border_width=1,
            command=self._on_open_operations_admin_panel,
        )
        self.operations_admin_button.grid(row=0, column=3, padx=(0, 10))
        self.operations_admin_button.grid_remove()  # 기본 숨김 — 비활성화가 아니라 표시 자체를 안 함

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
            ).grid(row=0, column=4, padx=(0, 10))

        # 진단정보 버튼 — 도움말/설정 화면이 따로 없는 이 프로그램에서 현장
        # 문제 확인용으로 항상 노출한다(권한 게이팅 없음 — Release Candidate
        # Sprint 1). 백업 관리 화면은 이 창에서 "백업 관리..." 버튼으로 연다.
        ctk.CTkButton(
            right_frame,
            text="진단정보",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            width=90,
            height=28,
            fg_color="transparent",
            border_width=1,
            command=self._on_open_diagnostics_panel,
        ).grid(row=0, column=5)

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
            on_message_focus=self._on_message_focus_start,
            on_message_blur=self._on_message_focus_end,
            on_refresh_shared_messages=self._on_manual_refresh_all,
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
        messages = data.get("messages", {})
        self.control_panel.load_messages(messages)
        self._mark_message_source(messages.keys(), MESSAGE_SOURCE_MANUAL_FILE)

    def _apply_legacy_messages(self, messages: dict) -> None:
        """레거시 CloudSyncCoordinator(30초 폴링)가 적용할 때 거치는 경유지 —
        기존 control_panel.load_messages() 호출은 그대로 유지하면서 출처만
        기록한다(요구사항 12절)."""
        self.control_panel.load_messages(messages)
        self._mark_message_source(messages.keys(), MESSAGE_SOURCE_LEGACY_LOCAL)

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
        # Mobile 실시간 동기화 스프린트: 레거시 messages 테이블(위 notify_local_autosave)과
        # 별개로 shared_messages도 같은 디바운스 시점에 저장을 시도한다 — 새 저장
        # 버튼을 만들지 않고 "기존 PC GUI 사용 방식을 최대한 유지"하기 위해, 이미
        # 있는 2초 디바운스를 "저장 실행" 시점으로 재사용한다(요구사항 4절).
        self._run_shared_message_autosave(messages)

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
        self._run_shared_message_autosave(messages)

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
            self._mark_message_source(initial_messages.keys(), MESSAGE_SOURCE_LEGACY_LOCAL)
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

    # ===== Mobile 실시간 동기화 (shared_messages) =====
    #
    # 레거시 CloudSyncCoordinator/messages 테이블(위 섹션)과 완전히 별개로 동작한다
    # (요구사항 12 — 기존 기능 유지). SUPABASE_ENABLED=false면 아무 것도 하지 않는다.

    _SHARED_STATUS_COLORS: dict = {
        MessageSyncStatus.SYNCED: "gray60",
        MessageSyncStatus.SAVING: "#5BA4E8",
        MessageSyncStatus.OFFLINE_PENDING: "#E6A100",
        MessageSyncStatus.CONFLICT: "#B71C1C",
        MessageSyncStatus.RECONNECTING: "#5BA4E8",
        MessageSyncStatus.REMOTE_UPDATED: "#E65100",
    }

    def _start_shared_message_sync(self) -> None:
        config = self._auth.client_manager.config
        if not (config.enabled and config.realtime_enabled):
            self._render_realtime_status_label(RealtimeConnectionState.STOPPED)
            return
        self._realtime_sync = RealtimeMessageSyncService(
            supabase_url=config.url,
            supabase_anon_key=config.anon_key,
            get_session_tokens_fn=self._get_realtime_session_tokens,
            on_change_fn=self._on_shared_message_change,
            on_state_fn=self._on_shared_realtime_state,
            on_reconcile_needed_fn=self._on_shared_reconcile_needed,
            log_fn=self._on_log,
            debug_enabled=self._msg_sync_config.sync_debug_enabled,
            protocol_compat_enabled=self._msg_sync_config.realtime_protocol_compat_enabled,
        )
        self._realtime_sync.start()

    def _restart_shared_message_realtime(self) -> None:
        """로그인/로그아웃 직후 호출 — Realtime 연결이 새 인증 세션(또는 익명 상태)을
        반영하도록 재시작한다. 호출부가 이미 백그라운드 스레드에서 부르므로 여기서
        stop()이 최대 5초 대기해도 UI를 막지 않는다."""
        if self._realtime_sync is not None:
            self._realtime_sync.stop()
            self._realtime_sync = None
        config = self._auth.client_manager.config
        if config.enabled and config.realtime_enabled:
            self.after(0, self._start_shared_message_sync)

    def _get_realtime_session_tokens(self) -> tuple:
        """로컬 저장된 세션 파일만 읽는다(네트워크 없음) — realtime_message_sync_service의
        get_session_tokens_fn 계약과 동일."""
        session = self._auth.load_session()
        if session is None:
            return (None, None)
        return (session.access_token, session.refresh_token)

    # --- ControlPanel 포커스 연동 ---

    def _on_message_focus_start(self, message_no: int) -> None:
        self._shared_coordinator.begin_edit(message_no)
        # legacy 발송 직전 검증(EDIT_IN_PROGRESS 판정)이 참조하는 메시지별 편집
        # 상태 — shared_messages와 별개로 CloudSyncCoordinator가 자체 추적한다.
        self._cloud.begin_edit(message_no)

    def _on_message_focus_end(self, message_no: int) -> None:
        state = self._shared_coordinator.get_state(message_no)
        self._shared_coordinator.end_edit(message_no)
        self._cloud.end_edit(message_no)
        if state.pending_remote is not None:
            self._show_remote_conflict_dialog(message_no)

    # --- 저장 (기존 2초 디바운스 재사용) ---

    def _run_shared_message_autosave(self, messages: dict) -> None:
        if not self._auth.client_manager.config.enabled:
            return
        changed = {}
        for n in range(MIN_MESSAGE_NO, MAX_MESSAGE_NO + 1):
            text = messages.get(n, "")
            state = self._shared_coordinator.get_state(n)
            if text != state.content:
                changed[n] = text
        if not changed:
            return
        self._run_in_thread(lambda: self._do_shared_message_autosave(changed))

    def _do_shared_message_autosave(self, changed: dict) -> None:
        for message_no, text in changed.items():
            state = self._shared_coordinator.get_state(message_no)
            base_revision = state.base_revision if state.base_revision is not None else state.revision
            self.after(0, lambda n=message_no: self._shared_coordinator.mark_saving(n))
            try:
                record = self._shared_msg_service.update_message(
                    message_no, None, text, base_revision=base_revision, update_source="desktop",
                )
            except SharedMessageConflictError:
                logger.warning(f"shared_messages 저장 충돌(message_no={message_no})")
                self._log_masked_save_result(message_no, success=False, reason="conflict")
                self.after(0, lambda n=message_no: self._shared_coordinator.mark_conflict(n))
            except SharedMessageError as e:
                logger.warning(f"shared_messages 저장 실패(message_no={message_no}): {type(e).__name__}")
                self._log_masked_save_result(message_no, success=False, reason=type(e).__name__)
                self.after(0, lambda n=message_no: self._shared_coordinator.mark_offline_pending(n))
            else:
                self._log_masked_save_result(message_no, success=True, reason="")
                self.after(0, lambda n=message_no, r=record: self._apply_saved_message(n, r))

    def _apply_saved_message(self, message_no: int, record: SharedMessageRecord) -> None:
        self._shared_coordinator.mark_saved(message_no, _snapshot_from_record(record))
        self._mark_message_source([message_no], MESSAGE_SOURCE_SHARED_SERVER)
        self._refresh_message_status_label(message_no)

    def _log_masked_save_result(self, message_no: int, success: bool, reason: str) -> None:
        # 요구사항 16절 — 메시지 본문 전체는 로그에 남기지 않는다(번호/성공여부/오류유형만).
        if success:
            self._on_log(f"[INFO] PC 저장 성공 — message_no={message_no}")
        else:
            self._on_log(f"[경고] PC 저장 실패 — message_no={message_no}, 사유={reason}")

    # --- Realtime 콜백 (asyncio 스레드에서 호출됨 — 반드시 self.after()로 메인 스레드에 넘긴다) ---

    def _on_shared_message_change(self, event: MessageChangeEvent) -> None:
        self.after(0, lambda: self._apply_shared_message_change(event))

    def _apply_shared_message_change(self, event: MessageChangeEvent) -> None:
        snap = RemoteMessageSnapshot(
            message_no=event.message_no, content=event.content, revision=event.revision,
            title=event.title, updated_by_name=event.updated_by_name or "", updated_at=event.updated_at,
            update_source=event.update_source,
        )
        applied = self._shared_coordinator.apply_remote_event(snap)
        if applied:
            with self.control_panel.suppress_change_notifications():
                self.control_panel.set_message(event.message_no, event.content)
            self._mark_message_source([event.message_no], MESSAGE_SOURCE_SHARED_SERVER)
        self._refresh_message_status_label(event.message_no)

    _REALTIME_STATE_LABELS_KO: dict = {
        RealtimeConnectionState.STOPPED: "실시간 동기화 비활성화",
        RealtimeConnectionState.STARTING: "연결 중",
        RealtimeConnectionState.CONNECTING: "연결 중",
        RealtimeConnectionState.SUBSCRIBED: "실시간 연결됨",
        RealtimeConnectionState.RECONNECTING: "재연결 중",
        RealtimeConnectionState.FAILED: "오프라인",
        RealtimeConnectionState.STOPPING: "오프라인",
    }
    _REALTIME_STATE_COLORS: dict = {
        RealtimeConnectionState.STOPPED: "gray60",
        RealtimeConnectionState.STARTING: "#5BA4E8",
        RealtimeConnectionState.CONNECTING: "#5BA4E8",
        RealtimeConnectionState.SUBSCRIBED: "#4CAF50",
        RealtimeConnectionState.RECONNECTING: "#E6A100",
        RealtimeConnectionState.FAILED: "#B71C1C",
        RealtimeConnectionState.STOPPING: "gray60",
    }

    def _on_shared_realtime_state(self, state: RealtimeConnectionState) -> None:
        self.after(0, lambda: self._apply_shared_realtime_state(state))

    def _apply_shared_realtime_state(self, state: RealtimeConnectionState) -> None:
        # 사용자에게는 enum 값이나 기술 용어를 그대로 보여주지 않는다(요구사항 8절).
        self._render_realtime_status_label(state)
        if state == RealtimeConnectionState.RECONNECTING:
            self._shared_coordinator.mark_all_reconnecting()
            self._refresh_all_message_status_labels()
        elif state == RealtimeConnectionState.SUBSCRIBED:
            self._refresh_all_message_status_labels()
            if self._msg_sync_config.sync_debug_enabled and self._realtime_sync is not None:
                self._on_log(f"[DEBUG] Realtime 상태 전환 — state={state.value}, 누적 재연결 횟수={self._realtime_sync.reconnect_count}")

    def _render_realtime_status_label(self, state: Optional[RealtimeConnectionState] = None) -> None:
        if state is None:
            state = self._realtime_sync.state if self._realtime_sync is not None else RealtimeConnectionState.STOPPED
        label = self._REALTIME_STATE_LABELS_KO.get(state, "알 수 없음")
        color = self._REALTIME_STATE_COLORS.get(state, "gray60")
        suffix = f" · 마지막 동기화 {self._last_shared_sync_text}" if self._last_shared_sync_text else ""
        self.realtime_status_label.configure(text=f"⚡ {label}{suffix}", text_color=color)

    def _on_shared_reconcile_needed(self) -> None:
        # asyncio 스레드에서 호출된다 — threading.Thread 생성 자체는 Tk를 건드리지
        # 않으므로 어느 스레드에서 불러도 안전하다(MainWindow._run_in_thread 참고).
        self._run_in_thread(lambda: self._do_shared_message_reconcile(is_manual=False))

    def _do_shared_message_reconcile(self, is_manual: bool = False) -> None:
        try:
            records = self._shared_msg_service.list_messages()
        except SharedMessageError as e:
            logger.warning(f"shared_messages 재조회 실패: {type(e).__name__}")
            if is_manual:
                self.after(0, lambda: self._on_log(f"[오류] 공유 메시지(shared_messages) 수동 새로고침 실패: {type(e).__name__}"))
            return
        self.after(0, lambda: self._apply_shared_full_snapshot(records, is_manual=is_manual))

    def _apply_shared_full_snapshot(self, records: list, is_manual: bool = False) -> None:
        snapshots = [_snapshot_from_record(r) for r in records]
        applied = self._shared_coordinator.apply_full_snapshot(snapshots)
        if applied:
            with self.control_panel.suppress_change_notifications():
                for n in applied:
                    self.control_panel.set_message(n, self._shared_coordinator.get_state(n).content)
            self._mark_message_source(applied, MESSAGE_SOURCE_SHARED_SERVER)
        self._last_shared_sync_at = datetime.now()
        self._last_shared_sync_text = self._last_shared_sync_at.strftime("%H:%M:%S")
        self._render_realtime_status_label()
        self._refresh_all_message_status_labels()
        self._maybe_offer_initial_migration(records)

        if is_manual:
            deferred = sum(
                1 for n in range(MIN_MESSAGE_NO, MAX_MESSAGE_NO + 1)
                if self._shared_coordinator.get_state(n).status == MessageSyncStatus.REMOTE_UPDATED
            )
            self._on_log(
                f"[INFO] 공유 메시지(shared_messages) 수동 새로고침 완료 — {len(records)}개 메시지 확인, "
                f"{len(applied)}개 갱신, {deferred}개 편집 중이라 보류, 오류 0개"
            )

    # --- 상태 라벨 갱신 ---

    def _refresh_message_status_label(self, message_no: int) -> None:
        state = self._shared_coordinator.get_state(message_no)
        color = self._SHARED_STATUS_COLORS.get(state.status, "gray60")
        self.control_panel.set_message_status(message_no, state.status_label_ko, color)

    def _refresh_all_message_status_labels(self) -> None:
        for n in range(MIN_MESSAGE_NO, MAX_MESSAGE_NO + 1):
            self._refresh_message_status_label(n)

    def _mark_message_source(self, numbers, source: str) -> None:
        """요구사항 12절 — 어느 시스템이 마지막으로 이 메시지의 화면 표시값을
        썼는지 기록한다(진단정보 화면/발송 로그에서 참고). 본문은 남기지 않는다."""
        for n in numbers:
            self._message_content_source[n] = source

    def _on_manual_refresh_all(self) -> None:
        """수동 새로고침 버튼(요구사항 9절, Test Environment 스프린트 요구사항 2절) —
        shared_messages와 legacy messages 양쪽을 모두 새로고침한다. 두 시스템은
        완전히 분리돼 있어 한쪽이 실패해도 다른 쪽에 영향을 주지 않고, 각자의
        결과를 서로 다른 로그 문구로 구분해 보여준다."""
        self._on_log("[INFO] 메시지 수동 새로고침 요청 (공유 메시지 + 레거시 메시지)")
        self._run_in_thread(lambda: self._do_shared_message_reconcile(is_manual=True))
        self._cloud.request_manual_refresh()

    # --- 편집 중 원격 변경 처리(요구사항 10절) ---

    def _show_remote_conflict_dialog(self, message_no: int) -> None:
        state = self._shared_coordinator.get_state(message_no)
        pending = state.pending_remote
        if pending is None:
            return

        is_admin = self._current_profile is not None and self._current_profile.is_admin

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"메시지 {message_no} — 다른 직원이 이 메시지를 수정했습니다")
        dialog.geometry("420x260" if is_admin else "420x220")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog, text="다른 직원이 이 메시지를 수정했습니다.",
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
        ).pack(pady=(16, 4), padx=16)
        ctk.CTkLabel(
            dialog, text=f"수정자: {pending.updated_by_name or '알 수 없음'}",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11), text_color="gray60",
        ).pack(pady=(0, 12))

        def on_view_latest():
            messagebox.showinfo("최신 내용", pending.content or "(빈 메시지)", parent=dialog)

        def on_keep_local():
            self._shared_coordinator.keep_local_and_discard_remote(message_no)
            self._refresh_message_status_label(message_no)
            dialog.destroy()

        def on_load_latest():
            current_text = self.control_panel.get_message(message_no)
            try:
                self.clipboard_clear()
                self.clipboard_append(current_text)
            except Exception:
                pass
            result = self._shared_coordinator.load_latest_and_discard_edit(message_no)
            if result is not None:
                with self.control_panel.suppress_change_notifications():
                    self.control_panel.set_message(message_no, result.content)
                self._mark_message_source([message_no], MESSAGE_SOURCE_SHARED_SERVER)
            self._refresh_message_status_label(message_no)
            dialog.destroy()

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=8, fill="x", padx=16)
        ctk.CTkButton(btn_frame, text="최신 내용 확인", command=on_view_latest, width=120).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text="현재 내용 유지", command=on_keep_local, width=120).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_frame, text="작성 내용 복사 후 최신 불러오기", command=on_load_latest, width=160,
        ).pack(side="left", padx=4)

        # 요구사항 10절 — 관리자에게만 강제 저장 선택지를 추가로 제공한다. 일반
        # 직원에게는 이 버튼 자체가 존재하지 않는다(위젯을 만들지 않음 — 숨김이
        # 아니라 아예 생성하지 않는다). 최종 방어는 항상 RPC(fn_is_admin())다 —
        # 이 UI 게이팅은 편의 기능일 뿐이다.
        if is_admin:
            def on_force_save():
                confirmed = messagebox.askyesno(
                    "강제 저장",
                    "서버의 최신 메시지를 덮어씁니다.\n다른 직원이 수정한 내용이 사라질 수 있습니다.",
                    parent=dialog,
                )
                if not confirmed:
                    return
                current_text = self.control_panel.get_message(message_no)
                dialog.destroy()
                self._run_in_thread(lambda: self._do_admin_force_save(message_no, current_text))

            ctk.CTkButton(
                dialog, text="내 내용으로 강제 저장", command=on_force_save, width=200,
                fg_color="#B71C1C", hover_color="#7F0000",
            ).pack(pady=(0, 12))

    def _do_admin_force_save(self, message_no: int, content: str) -> None:
        try:
            record = self._shared_msg_service.force_update_message(
                message_no, None, content, update_source="admin_force",
            )
        except SharedMessageError as e:
            logger.warning(f"관리자 강제 저장 실패(message_no={message_no}): {type(e).__name__}")
            self.after(0, lambda: self._on_log(f"[오류] 관리자 강제 저장 실패 — message_no={message_no}: {type(e).__name__}"))
            return
        self._on_log(f"[INFO] 관리자 강제 저장 완료 — message_no={message_no}")
        self.after(0, lambda: self._apply_saved_message(message_no, record))

    # --- 발송 직전 검증(요구사항 4/5절, Production Stabilization Sprint) ---

    def _verify_message_before_send(self, message_no: int, local_content: str, local_revision: int):
        """AutoScheduler가 그룹 발송 시작 직전, message_no 한 건씩, 스케줄러 자신의
        백그라운드 스레드에서 동기 호출한다(Tk 위젯을 직접 건드리지 않음 — 위젯
        반영은 self.after()로 넘김). 반환값은 core.send_verification.
        SendMessageVerificationResult — AutoScheduler는 이 결과의 allowed/content만
        보고 정책 판단 자체는 하지 않는다(정책은 이 메서드 안에서 완결).

        어느 검증 경로를 탈지는 설정값(SHARED_MESSAGES_PRIMARY)만으로 정적으로
        정하지 않는다 — shared 우선이어도 실제로는 legacy로 폴백된 문구가 화면에
        떠 있을 수 있으므로, self._message_content_source(요구사항 12절에서
        이미 매 반영 지점마다 갱신해 온 실제 출처 추적)를 그대로 기준으로 삼는다.
        default/manual_file 출처는 어느 클라우드 시스템에도 속하지 않으므로
        서버 검증 없이 그대로 발송을 허용한다.
        """
        source = self._message_content_source.get(message_no, MESSAGE_SOURCE_DEFAULT)
        if source == MESSAGE_SOURCE_SHARED_SERVER:
            result = self._verify_shared_message_before_send(message_no, local_content, local_revision)
        elif source == MESSAGE_SOURCE_LEGACY_LOCAL:
            result = self._verify_legacy_message_before_send(message_no, local_content, local_revision)
        else:
            result = SendMessageVerificationResult(
                allowed=True, message_no=message_no, content=local_content,
                local_revision=local_revision, server_revision=None,
                source=VerificationSource.LOCAL_CACHE, used_cached_content=False,
            )
        self._log_send_verification_result(result)
        return result

    def _verify_shared_message_before_send(self, message_no: int, local_content: str, local_revision: int):
        """무인 자동 발송(하루 종일 08~19시, 15분마다) 중이므로, 서버 확인이 실패해도
        여기서는 대화상자를 띄우지 않는다 — MESSAGE_SEND_OFFLINE_POLICY(기본 block)에
        따라 발송 보류 또는 캐시 발송으로 정책적으로 처리하고 로그만 남긴다.
        "시작" 버튼을 누른 최초 1회(_verify_before_start_and_launch)만 사용자가
        화면 앞에 있다고 가정해 명시적 확인을 받는다 — CLAUDE.md의 "무인 자동 발송"
        원칙과 "발송 직전 서버 확인" 요구사항이 상충하는 지점을 의도적으로
        절충한 것이다(docs/realtime_sync.md §7에 근거 명시).
        """
        result = verify_message_before_send(
            message_no=message_no,
            local_content=local_content,
            local_revision=local_revision,
            fetch_fn=self._shared_msg_service.get_message,
            policy=OfflineSendPolicy.from_str(self._msg_sync_config.send_offline_policy),
            timeout_seconds=self._msg_sync_config.send_verify_timeout_seconds,
            retry_count=self._msg_sync_config.send_verify_retry_count,
            service_enabled=self._auth.client_manager.config.enabled,
        )
        if result.source == VerificationSource.SERVER and not result.used_cached_content:
            self.after(0, lambda: self._apply_verified_message(message_no, result))
        return result

    def _verify_legacy_message_before_send(self, message_no: int, local_content: str, local_revision: int):
        """legacy messages(30초 상시 polling 제거 이후) 발송 직전 검증 —
        core/legacy_send_verification.py의 classify_message() 판정을 그대로
        재사용한다. CONFLICT/EDIT_IN_PROGRESS는 각각 발송을 중단시킨다(로컬
        미저장 변경을 조용히 덮어쓰지 않는다는 원칙)."""
        dirty, local_version, last_synced_text = self._cloud.get_local_message_state(message_no)
        result = verify_legacy_message_before_send(
            message_no=message_no,
            local_content=local_content,
            is_dirty=dirty,
            local_version=local_version,
            last_synced_text=last_synced_text,
            is_editing=self._cloud.is_editing(message_no),
            fetch_fn=self._cloud.pull_message,
            push_fn=self._cloud.push_single_message_sync,
            policy=OfflineSendPolicy.from_str(self._msg_sync_config.send_offline_policy),
            timeout_seconds=self._msg_sync_config.send_verify_timeout_seconds,
            retry_count=self._msg_sync_config.send_verify_retry_count,
            service_enabled=self._auth.client_manager.config.enabled,
        )
        # content가 local_content와 다를 때만 실제로 반영할 것이 있다(REMOTE_APPLY
        # 성공 케이스뿐 — LOCAL_PENDING/NOOP/IDENTICAL은 이미 화면과 같은 내용이라
        # 다시 그릴 필요가 없다. 편집 중 텍스트박스를 불필요하게 건드리지 않기 위함).
        if (
            result.allowed and result.source == VerificationSource.SERVER
            and not result.used_cached_content and result.content != local_content
        ):
            self.after(0, lambda: self._apply_verified_legacy_message(message_no, result))
        return result

    def _apply_verified_message(self, message_no: int, result) -> None:
        state = self._shared_coordinator.get_state(message_no)
        if state.is_editing:
            return  # 편집 중이면 덮어쓰지 않는다(요구사항 10절과 동일 원칙)
        if result.server_revision is None or result.server_revision <= state.revision:
            return  # 서버가 더 새롭지 않으면 화면을 건드릴 필요 없음
        snap = RemoteMessageSnapshot(
            message_no=message_no, content=result.content, revision=result.server_revision,
            title=state.title, updated_by_name=state.updated_by_name, updated_at="",
            update_source="",
        )
        self._shared_coordinator.mark_saved(message_no, snap)
        with self.control_panel.suppress_change_notifications():
            self.control_panel.set_message(message_no, result.content)
        self._mark_message_source([message_no], MESSAGE_SOURCE_SHARED_SERVER)
        self._refresh_message_status_label(message_no)

    def _apply_verified_legacy_message(self, message_no: int, result) -> None:
        if self._cloud.is_editing(message_no):
            return  # 그 사이 사용자가 편집을 시작했으면 조용히 덮어쓰지 않는다
        with self.control_panel.suppress_change_notifications():
            self.control_panel.set_message(message_no, result.content)
        self._cloud.record_remote_applied(message_no, result.server_revision, result.content)
        self._mark_message_source([message_no], MESSAGE_SOURCE_LEGACY_LOCAL)

    # 요구사항 9절 — 충돌/네트워크실패/타임아웃/편집중 원격변경을 서로 다른
    # 문구로 구분한다(error_code는 이미 서로 다른 값이지만, 사람이 읽는 로그도
    # 한눈에 구분되어야 한다는 요구사항이므로 문구도 분리한다).
    _SEND_BLOCK_MESSAGES_KO = {
        VerificationErrorCode.CONFLICT: "충돌(다른 곳에서 이미 이 메시지를 수정함)",
        VerificationErrorCode.EDIT_IN_PROGRESS: "서버에 최신 변경사항이 있습니다. 편집을 종료하고 새로고침하세요",
        VerificationErrorCode.TIMEOUT: "서버 확인 시간 초과",
        VerificationErrorCode.NETWORK_ERROR: "서버 확인 실패(네트워크 오류)",
        VerificationErrorCode.AUTH_ERROR: "인증 오류",
        VerificationErrorCode.MESSAGE_NOT_FOUND: "서버에서 메시지를 찾을 수 없음",
        VerificationErrorCode.REVISION_ERROR: "버전 정보 오류",
    }

    def _log_send_verification_result(self, result) -> None:
        # 요구사항 16절 — 메시지 본문은 로그에 남기지 않는다(message_no/출처/revision/
        # 오류유형만). MESSAGE_SYNC_DEBUG가 꺼져 있으면(기본값) 성공 건은 남기지
        # 않는다 — 실패/캐시 사용처럼 운영상 주목해야 할 이벤트만 기본 로그에 남긴다.
        if not result.allowed:
            reason = self._SEND_BLOCK_MESSAGES_KO.get(
                result.error_code, result.error_code.value if result.error_code else "알 수 없음"
            )
            self._on_log(f"[경고] 발송 중단/보류 — message_no={result.message_no}, 사유={reason}")
        elif result.used_cached_content:
            self._on_log(f"[경고] 캐시 내용으로 발송 — message_no={result.message_no}")
        elif self._msg_sync_config.sync_debug_enabled:
            self._on_log(
                f"[DEBUG] 발송 직전 검증 성공 — message_no={result.message_no}, "
                f"local_rev={result.local_revision}, server_rev={result.server_revision}"
            )

    # --- 초기 마이그레이션(요구사항 13절, 관리자 전용 1회성) ---

    def _maybe_offer_initial_migration(self, records: list) -> None:
        if self._migration_prompted:
            return
        if self._current_profile is None or not self._current_profile.is_admin:
            return
        if not all(is_untouched_seed(r) for r in records):
            return  # 이미 누군가 실제로 수정한 적이 있으면 마이그레이션 대상 아님

        local_messages = self.control_panel.get_all_messages()
        if not any(v.strip() for v in local_messages.values()):
            return  # 로컬에도 옮길 내용이 없으면 프롬프트하지 않음

        migration_marker = self._shared_migration_marker_path()
        if os.path.exists(migration_marker):
            return

        self._migration_prompted = True
        confirmed = messagebox.askyesno(
            "초기 메시지 이전",
            "서버의 1~12번 메시지가 아직 비어 있습니다.\n"
            "현재 PC에 입력된 메시지를 서버로 이전하시겠습니까?\n"
            "(이전 전 현재 데이터가 자동으로 백업됩니다)",
            parent=self,
        )
        if not confirmed:
            return
        self._run_in_thread(lambda: self._run_shared_message_migration(local_messages))

    def _shared_migration_marker_path(self) -> str:
        return os.path.join(self._data.get_storage_dir(), "cloud_sync", "shared_messages_migration_state.json")

    def _run_shared_message_migration(self, local_messages: dict) -> None:
        try:
            self._backup_service.create_backup(backup_type="manual")
        except BackupError as e:
            logger.warning(f"마이그레이션 전 백업 실패(계속 진행): {e}")
            self._on_log(f"[경고] 마이그레이션 전 백업 실패: {e}")

        migrated = []
        for n, text in local_messages.items():
            if not text.strip():
                continue
            try:
                record = self._shared_msg_service.force_update_message(n, None, text, update_source="migration")
            except SharedMessageError as e:
                logger.warning(f"초기 마이그레이션 실패(message_no={n}): {type(e).__name__}")
                self._on_log(f"[경고] 초기 마이그레이션 실패 — message_no={n}: {type(e).__name__}")
                continue
            migrated.append(n)
            self.after(0, lambda n=n, r=record: self._apply_saved_message(n, r))

        self._on_log(f"[INFO] 초기 마이그레이션 완료 — message_no={migrated}")
        try:
            marker_path = self._shared_migration_marker_path()
            os.makedirs(os.path.dirname(marker_path), exist_ok=True)
            with open(marker_path, "w", encoding="utf-8") as f:
                f.write('{"migrated": true}')
        except OSError as e:
            logger.warning(f"마이그레이션 완료 표시 저장 실패(무시): {e}")

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
        profile: Optional[AppUserProfile] = None
        if logged_in:
            user = self._auth.get_current_user()
            email = user.email if user else ""
            # 운영 관리자 버튼 노출 판정에 필요 — 이미 백그라운드 스레드이므로
            # 여기서 함께 조회해도 UI 스레드를 막지 않는다.
            profile = self._auth.get_app_user_profile()
        self.after(0, lambda: self._apply_auth_ui(logged_in, email, profile))

    def _apply_auth_ui(self, logged_in: bool, email: str, profile: Optional[AppUserProfile] = None) -> None:
        if logged_in:
            self.auth_label.configure(text=email or "로그인됨")
            self.auth_button.configure(text="로그아웃", command=self._on_logout_click)
        else:
            self.auth_label.configure(text="")
            self.auth_button.configure(text="Google로 로그인", command=self._on_login_click)
        self._current_profile = profile
        self._apply_operations_admin_visibility(profile)

    # ===== 운영 관리자 (Phase 4-2) =====

    def _apply_operations_admin_visibility(self, profile: Optional[AppUserProfile]) -> None:
        """UI 노출은 편의 기능일 뿐 보안 경계가 아니다 — 실제 권한은 admin_* RPC가
        매번 다시 확인한다(services/admin_service.py 경유)."""
        if is_operations_admin_menu_visible(profile):
            self.operations_admin_button.grid()
        else:
            self.operations_admin_button.grid_remove()
            # 더 이상 admin이 아니게 됐다면(로그아웃/권한 변경 등) 열려 있던 창도 닫는다.
            self._close_operations_admin_panel_if_open()

    def _on_open_operations_admin_panel(self) -> None:
        """이미 열려 있으면 새로 만들지 않고 기존 창을 앞으로 가져온다(중복 실행 방지)."""
        if self._operations_admin_panel is not None:
            try:
                if self._operations_admin_panel.winfo_exists():
                    self._operations_admin_panel.lift()
                    self._operations_admin_panel.focus_force()
                    return
            except Exception:
                pass
            self._operations_admin_panel = None

        if self._current_profile is None or not self._current_profile.is_admin:
            # 버튼이 숨겨져 있어야 정상이지만, 프로필 갱신 사이의 경합 상태를 방어한다.
            return

        self._operations_admin_panel = OperationsAdminPanel(
            self,
            admin_service=self._admin_service,
            current_user_id=self._current_profile.id,
            on_permission_lost=self._on_operations_admin_permission_lost,
            log_callback=self._on_log,
        )
        # 사용자가 직접 닫기 버튼을 눌러 창이 실제로 파괴될 때 참조를 정리한다.
        self._operations_admin_panel.bind("<Destroy>", self._on_operations_admin_panel_destroyed)

    def _on_operations_admin_panel_destroyed(self, _event=None) -> None:
        self._operations_admin_panel = None

    def _on_operations_admin_permission_lost(self) -> None:
        """OperationsAdminPanel이 AdminPermissionError를 받아 스스로 닫히기 직전 호출한다
        — 현재 프로필을 다시 조회해 운영 관리자 버튼 노출 상태를 갱신한다."""
        self._on_log("[경고] 운영 관리자 권한 재확인 필요 — 프로필을 다시 조회합니다.")
        self._refresh_auth_ui_async()

    def _close_operations_admin_panel_if_open(self) -> None:
        if self._operations_admin_panel is None:
            return
        panel = self._operations_admin_panel
        self._operations_admin_panel = None
        try:
            if panel.winfo_exists():
                panel.dispose()
                panel.destroy()
        except Exception:
            pass

    # ===== 진단정보 / 백업 관리 (Release Candidate Sprint 1) =====

    def _on_open_diagnostics_panel(self) -> None:
        """이미 열려 있으면 새로 만들지 않고 기존 창을 앞으로 가져온다."""
        if self._diagnostics_panel is not None:
            try:
                if self._diagnostics_panel.winfo_exists():
                    self._diagnostics_panel.lift()
                    self._diagnostics_panel.focus_force()
                    return
            except Exception:
                pass
            self._diagnostics_panel = None

        self._diagnostics_panel = DiagnosticsPanel(
            self,
            diagnostics_service=self._diagnostics_service,
            on_open_backup_panel=self._on_open_backup_panel,
            log_callback=self._on_log,
        )
        self._diagnostics_panel.bind("<Destroy>", self._on_diagnostics_panel_destroyed)

    def _on_diagnostics_panel_destroyed(self, _event=None) -> None:
        self._diagnostics_panel = None

    def _on_open_backup_panel(self) -> None:
        if self._backup_panel is not None:
            try:
                if self._backup_panel.winfo_exists():
                    self._backup_panel.lift()
                    self._backup_panel.focus_force()
                    return
            except Exception:
                pass
            self._backup_panel = None

        self._backup_panel = BackupPanel(
            self,
            backup_service=self._backup_service,
            current_profile_fn=lambda: self._current_profile,
            log_callback=self._on_log,
        )
        self._backup_panel.bind("<Destroy>", self._on_backup_panel_destroyed)

    def _on_backup_panel_destroyed(self, _event=None) -> None:
        self._backup_panel = None

    def _close_diagnostics_and_backup_panels_if_open(self) -> None:
        for attr in ("_diagnostics_panel", "_backup_panel"):
            panel = getattr(self, attr)
            if panel is None:
                continue
            setattr(self, attr, None)
            try:
                if panel.winfo_exists():
                    panel.dispose()
                    panel.destroy()
            except Exception:
                pass

    def _run_auto_backup_if_due(self) -> None:
        """종료 직전 호출 — 오늘 auto 백업이 없으면 1개 만든다.

        종료 흐름을 막지 않는다: 실패해도 로그만 남기고 종료를 계속 진행한다
        (원칙 — 백업 실패가 사용자 데이터 원본에 영향을 주거나 종료를 막으면
        안 된다). 이 메서드는 짧은 동기 I/O만 하므로(ZIP 몇 개 파일) 별도
        스레드 없이 종료 경로에서 그대로 호출한다 — 저장 자체와 동일한
        수준의 지연이다.
        """
        try:
            if not self._backup_service.should_create_auto_backup_today():
                return
            record = self._backup_service.create_backup(backup_type="auto")
            self._backup_service.cleanup_old_backups()
            self._on_log(f"[INFO] 자동 백업 완료: {record.filename}")
        except BackupError as e:
            logger.warning(f"자동 백업 실패(종료는 계속 진행): {e}")
            self._on_log(f"[경고] 자동 백업 실패: {e}")
        except Exception as e:
            logger.exception("자동 백업 중 예상치 못한 오류(종료는 계속 진행)")
            self._on_log(f"[경고] 자동 백업 중 예상치 못한 오류: {e}")

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
            self._run_in_thread(self._restart_shared_message_realtime)
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
        self._restart_shared_message_realtime()

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

        # Mobile 실시간 동기화 스프린트(요구사항 9절): 발송을 실제로 시작하기 전
        # shared_messages의 최신 상태를 1회 확인한다. 이 확인은 사용자가 방금
        # "시작" 버튼을 눌러 화면 앞에 있는 유일한 순간이므로, 실패 시 명시적
        # 확인 대화상자를 띄운다(아래 매 그룹 자동 발송 시의 검증은 무인 운영을
        # 방해하지 않도록 로그만 남기고 조용히 캐시로 진행한다 —
        # _verify_latest_before_send 주석 참고, 의도적으로 다른 정책).
        self.btn_start.configure(state="disabled", text="확인 중...")
        self._run_in_thread(self._verify_before_start_and_launch)

    def _verify_before_start_and_launch(self) -> None:
        if not self._auth.client_manager.config.enabled:
            self.after(0, self._launch_scheduler_now)
            return
        try:
            records = self._shared_msg_service.list_messages()
        except SharedMessageError as e:
            logger.warning(f"발송 시작 전 최신 메시지 확인 실패: {type(e).__name__}")
            self.after(0, self._confirm_start_with_stale_cache)
            return
        self.after(0, lambda: self._apply_shared_full_snapshot(records))
        self.after(0, self._launch_scheduler_now)

    def _confirm_start_with_stale_cache(self) -> None:
        self.btn_start.configure(state="normal", text="▶   시작")
        confirmed = messagebox.askyesno(
            "서버 확인 실패",
            "서버에서 최신 메시지를 확인하지 못했습니다.\n마지막 동기화된 내용으로 발송하시겠습니까?",
            parent=self,
        )
        if confirmed:
            self._on_log("[경고] 서버 확인 실패 — 사용자 승인으로 마지막 동기화된 내용으로 발송을 시작합니다.")
            self._launch_scheduler_now()
        else:
            self._on_log("[INFO] 사용자가 발송 시작을 취소했습니다(서버 확인 실패).")

    def _launch_scheduler_now(self) -> None:
        self._scheduler.start()
        self.btn_start.configure(
            state="normal",
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
        """탐색기 저장 대화상자로 경로를 선택해 단톡방 목록+메시지를 저장한다."""
        rooms = self.room_list_panel.get_all_rooms()
        if not rooms:
            self._on_log("[경고] 저장할 단톡방이 없습니다.")
            return

        storage_dir = self._data.get_storage_dir()
        filepath = filedialog.asksaveasfilename(
            title="단톡방 목록 저장",
            initialdir=storage_dir,
            initialfile=default_room_list_filename(),
            defaultextension=".json",
            filetypes=[("JSON 파일", "*.json"), ("모든 파일", "*.*")],
            parent=self,
        )
        if not filepath:
            return

        messages = self.control_panel.get_all_messages()

        def run():
            try:
                self._data.save(rooms, messages, filepath)
                self._cloud.notify_local_save(messages)

                filled = sum(1 for v in messages.values() if v.strip())
                self._on_log(
                    f"[INFO] 저장 완료 — '{filepath}' "
                    f"(단톡방 {len(rooms)}개, 메시지 {filled}개)"
                )
            except PermissionError:
                self._on_log(f"[오류] 파일 저장 실패 — '{filepath}' 파일이 다른 프로그램에서 열려 있습니다.")
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
                self._mark_message_source(messages.keys(), MESSAGE_SOURCE_MANUAL_FILE)

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
        self._close_operations_admin_panel_if_open()
        self._close_diagnostics_and_backup_panels_if_open()
        if self._scheduler.is_running():
            self._scheduler.stop()

        self._flush_local_autosave_now()
        self._on_log("[INFO] 종료 전 로컬 저장 완료")
        self._run_auto_backup_if_due()
        if self._msg_sync_config.cloud_sync_on_exit:
            self._cloud.shutdown_flush(timeout_seconds=self._msg_sync_config.cloud_sync_exit_wait_seconds)

        self._cloud.stop(wait_seconds=2.0)
        if self._realtime_sync is not None:
            self._realtime_sync.stop()  # 구독 정상 해제(요구사항 7절)
        self.after(200, self.destroy)
