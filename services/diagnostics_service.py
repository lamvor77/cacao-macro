# 진단정보 수집 서비스 (Release Candidate Sprint 1)
#
# gui/panels/diagnostics_panel.py가 화면에 그리기만 하도록, 상태 조회는
# 전부 이 모듈에 모은다(UI와 로직 분리 원칙). 각 섹션은 독립적으로
# 수집하고 예외를 개별적으로 잡는다 — 한 섹션(예: Supabase 네트워크 확인)이
# 실패해도 나머지 섹션은 정상 표시된다.
#
# 절대 하지 않는 일:
#   - 진단정보 조회만으로 실제 클라우드 동기화를 실행하지 않는다
#     (CloudSyncCoordinator.get_status()는 이미 진행 중인 상태를 읽기만
#     한다 — 새 동기화를 트리거하지 않는다).
#   - OAuth 브라우저를 열지 않는다(AuthService.load_session()은 로컬 파일만
#     읽는다 — is_logged_in()/get_session()처럼 네트워크 refresh를 시도하지
#     않는다).
#   - Supabase에 쓰지 않는다(check_connection()은 읽기 전용 SELECT 1건).
#   - 토큰/키/서명 값을 그대로 반환하지 않는다 — 존재 여부만 반환한다.
#
# collect()는 파일 I/O(세션 복호화 포함)와 선택적으로 네트워크 확인을
# 포함하므로, 호출부(UI)가 반드시 별도 스레드에서 실행해야 한다(기존
# Admin UI 패턴과 동일 — self._run_in_thread 등).

import logging
import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from config.settings import IS_TEST_ENVIRONMENT, get_runtime_base_dir
from config.version import (
    APP_NAME, APP_VERSION, BUILD_CHANNEL, BUILD_DATE, git_commit_short,
)
from core.license_manager import LicenseManager, get_external_license_path
from services.auth_service import AuthService
from services.cloud_sync_coordinator import CloudSyncCoordinator
from storage.data_manager import DataManager
from utils.logger_setup import get_log_dir

logger = logging.getLogger(__name__)

# 진단정보 텍스트에 남아서는 안 되는 키워드 — copy_text() 생성 후 자체 검사에 사용한다
# (개발 중 실수로 민감 필드를 추가하는 것을 테스트가 잡아내도록 하는 안전망).
FORBIDDEN_KEYWORDS = (
    "secret", "password", "access_token", "refresh_token",
    "anon_key", "client_secret", "signature", "비밀번호",
)


def mask_host(url: str) -> str:
    """URL 전체 대신 호스트만, 그것도 일부만 보여준다.

    예: https://abcdefgh1234.supabase.co -> abcd****.supabase.co
    실제 프로젝트를 특정할 수 없을 만큼만 마스킹하면서, 어떤 Supabase
    프로젝트에 연결되어 있는지 완전히 숨기지도 않는다(운영 시 "설정이 맞게
    들어갔는지" 육안 확인 용도).
    """
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    if not host:
        return ""
    parts = host.split(".", 1)
    prefix = parts[0]
    rest = f".{parts[1]}" if len(parts) > 1 else ""
    if len(prefix) <= 4:
        masked_prefix = prefix[:1] + "*" * max(len(prefix) - 1, 0)
    else:
        masked_prefix = prefix[:4] + "*" * (len(prefix) - 4)
    return f"{masked_prefix}{rest}"


@dataclass
class AppDiagnostics:
    app_name: str = APP_NAME
    version: str = APP_VERSION
    build_channel: str = BUILD_CHANNEL
    build_date: str = ""
    git_commit_short: str = ""
    run_mode: str = ""
    executable_path: str = ""
    current_working_dir: str = ""
    # Test Environment Deployment & E2E Validation Sprint 1절 — APP_ENV/
    # SUPABASE_ENVIRONMENT=test일 때 화면에서 명확히 구분하기 위한 플래그.
    is_test_environment: bool = False


@dataclass
class OSDiagnostics:
    windows_version: str = ""
    python_version: str = ""
    architecture: str = ""
    user_data_dir: str = ""


@dataclass
class AuthDiagnostics:
    logged_in: bool = False
    email: str = ""
    is_admin: Optional[bool] = None
    oauth_configured: bool = False
    token_file_exists: bool = False
    note: str = ""
    error: str = ""


@dataclass
class SupabaseDiagnostics:
    configured: bool = False
    client_initialized: bool = False
    masked_host: str = ""
    network_status: str = "확인 안 함"
    error: str = ""


@dataclass
class SyncDiagnostics:
    offline_first_active: bool = True
    pending_count: int = 0
    conflict_count: int = 0
    last_sync_state: str = ""
    last_sync_detail: str = ""
    startup_sync_note: str = ""
    error: str = ""


@dataclass
class StorageDiagnostics:
    storage_path: str = ""
    data_file_count: int = 0
    total_size_bytes: int = 0
    last_modified: str = ""
    readable: bool = False
    writable: bool = False
    error: str = ""


@dataclass
class LogDiagnostics:
    log_dir: str = ""
    latest_log_filename: str = ""
    latest_log_modified: str = ""
    error: str = ""


@dataclass
class LicenseDiagnostics:
    """빌드용 라이선스 파일(license_build.json)의 형식/서명 상태만 담는다.

    사용기간(start_date/end_date/remaining_days)은 더 이상 실행을 막지
    않으므로 진단 화면에도 표시하지 않는다 — 실행 권한은 Supabase 계정
    인증/승인 상태로 관리한다(services/auth_service.py).
    """

    file_exists: bool = False
    valid: bool = False
    reason: str = ""
    admin_ui_enabled: bool = False
    error: str = ""


@dataclass
class BackupDiagnostics:
    latest_backup_at: str = ""
    backup_count: int = 0
    latest_backup_valid: Optional[bool] = None
    backup_dir: str = ""
    error: str = ""


@dataclass
class MessageSourceDiagnostics:
    """Production Stabilization Sprint 11/12절 — 레거시/신규 메시지 시스템
    이원 체계의 현재 상태를 화면에서 확인할 수 있게 한다(요구사항 11.B —
    "런타임 출처 표시"). docs/legacy_messages_migration_plan.md 참고."""

    shared_messages_enabled: bool = True
    legacy_sync_enabled: bool = True
    shared_messages_primary: bool = True
    fallback_cache_path: str = ""
    per_message_sources: dict = field(default_factory=dict)  # {message_no: source_label}
    error: str = ""


@dataclass
class DiagnosticsSnapshot:
    collected_at: str = ""
    app: AppDiagnostics = field(default_factory=AppDiagnostics)
    os_info: OSDiagnostics = field(default_factory=OSDiagnostics)
    auth: AuthDiagnostics = field(default_factory=AuthDiagnostics)
    supabase: SupabaseDiagnostics = field(default_factory=SupabaseDiagnostics)
    sync: SyncDiagnostics = field(default_factory=SyncDiagnostics)
    storage: StorageDiagnostics = field(default_factory=StorageDiagnostics)
    logs: LogDiagnostics = field(default_factory=LogDiagnostics)
    license: LicenseDiagnostics = field(default_factory=LicenseDiagnostics)
    backup: BackupDiagnostics = field(default_factory=BackupDiagnostics)
    message_source: MessageSourceDiagnostics = field(default_factory=MessageSourceDiagnostics)


class DiagnosticsService:
    """진단정보 스냅샷을 수집한다.

    MainWindow가 이미 만들어 쓰고 있는 서비스 인스턴스를 그대로 주입받는다
    (auth_service/cloud_coordinator는 Supabase Client를 공유해야 하므로 새로
    만들지 않는다 — services/cloud_sync_coordinator.py 상단 주석과 동일한
    이유). backup_service는 선택(없으면 백업 섹션이 "미구성"으로 표시됨).
    current_profile_fn은 MainWindow가 이미 보유한 AppUserProfile을 그대로
    돌려주는 콜백이다 — 진단정보 수집 자체가 새로 프로필을 조회(네트워크)하지
    않는다.
    """

    def __init__(
        self,
        auth_service: AuthService,
        cloud_coordinator: CloudSyncCoordinator,
        license_manager: LicenseManager,
        data_manager: DataManager,
        current_profile_fn,
        backup_service=None,
        check_network: bool = False,
        message_source_fn=None,
    ):
        self._auth = auth_service
        self._cloud = cloud_coordinator
        self._license = license_manager
        self._data = data_manager
        self._current_profile_fn = current_profile_fn
        self._backup = backup_service
        self._check_network = check_network
        # 요구사항 11.B — MainWindow._message_content_source를 그대로 읽어오는
        # 콜백(선택). 없으면 이 섹션은 기본값만 표시한다(collect() 자체는 죽지 않음).
        self._message_source_fn = message_source_fn

    def collect(self) -> DiagnosticsSnapshot:
        """전체 스냅샷을 수집한다. 개별 섹션 실패는 해당 섹션의 error 필드에만 남는다."""
        snapshot = DiagnosticsSnapshot(collected_at=datetime.now().isoformat(timespec="seconds"))
        snapshot.app = self._collect_app()
        snapshot.os_info = self._collect_os()
        snapshot.auth = self._collect_auth()
        snapshot.supabase = self._collect_supabase()
        snapshot.sync = self._collect_sync()
        snapshot.storage = self._collect_storage()
        snapshot.logs = self._collect_logs()
        snapshot.license = self._collect_license()
        snapshot.backup = self._collect_backup()
        snapshot.message_source = self._collect_message_source()
        return snapshot

    # ===== 섹션별 수집 (개별 예외 격리) =====

    def _collect_app(self) -> AppDiagnostics:
        try:
            frozen = getattr(sys, "frozen", False)
            return AppDiagnostics(
                build_date=BUILD_DATE or "알 수 없음",
                git_commit_short=git_commit_short() or "알 수 없음",
                run_mode="EXE" if frozen else "Development",
                executable_path=sys.executable,
                current_working_dir=os.getcwd(),
                is_test_environment=IS_TEST_ENVIRONMENT,
            )
        except Exception as e:
            logger.warning(f"앱 진단정보 수집 실패: {e}")
            return AppDiagnostics()

    def _collect_os(self) -> OSDiagnostics:
        try:
            return OSDiagnostics(
                windows_version=platform.platform(),
                python_version=platform.python_version(),
                architecture=platform.machine(),
                user_data_dir=get_runtime_base_dir(),
            )
        except Exception as e:
            logger.warning(f"OS 진단정보 수집 실패: {e}")
            return OSDiagnostics()

    def _collect_auth(self) -> AuthDiagnostics:
        info = AuthDiagnostics()
        try:
            session = self._auth.load_session()  # 로컬 파일만 읽음 — 네트워크 없음
            info.logged_in = session is not None and not session.is_expired()
            info.email = session.email if session else ""
            info.token_file_exists = session is not None
            info.note = "로컬 세션 기준(네트워크로 재확인하지 않음)"
            profile = self._current_profile_fn()
            info.is_admin = profile.is_admin if profile else None
            client_result = self._auth.client_manager.get_client()
            info.oauth_configured = client_result.error_code not in ("missing_config", "sdk_unavailable")
        except Exception as e:
            logger.warning(f"인증 진단정보 수집 실패: {e}")
            info.error = "조회 실패"
        return info

    def _collect_supabase(self) -> SupabaseDiagnostics:
        info = SupabaseDiagnostics()
        try:
            config = self._auth.client_manager.config
            info.configured = config.enabled
            info.masked_host = mask_host(config.url)
            client_result = self._auth.client_manager.get_client()
            info.client_initialized = client_result.success
            if not info.configured:
                info.network_status = "미설정"
            elif self._check_network and client_result.success:
                check = self._auth.client_manager.check_connection()
                info.network_status = "연결됨" if check.success else "연결 실패"
            else:
                info.network_status = "확인 안 함"
        except Exception as e:
            logger.warning(f"Supabase 진단정보 수집 실패: {e}")
            info.error = "조회 실패"
        return info

    def _collect_sync(self) -> SyncDiagnostics:
        info = SyncDiagnostics()
        try:
            status = self._cloud.get_status()  # 읽기 전용 — 새 동기화를 시작하지 않음
            info.pending_count = status.pending_count
            info.conflict_count = status.conflict_count
            info.last_sync_state = status.state.value
            info.last_sync_detail = status.detail or ""
            info.startup_sync_note = "프로그램 시작 시 로컬을 먼저 적용한 뒤 백그라운드로 동기화(항상 오프라인 우선)"
        except Exception as e:
            logger.warning(f"동기화 진단정보 수집 실패: {e}")
            info.error = "조회 실패"
        return info

    def _collect_storage(self) -> StorageDiagnostics:
        info = StorageDiagnostics()
        try:
            storage_dir = self._data.get_storage_dir()
            info.storage_path = storage_dir
            files = self._data.list_saved_files()
            info.data_file_count = len(files)
            total_size = 0
            latest_mtime = 0.0
            for f in files:
                try:
                    stat = os.stat(f)
                    total_size += stat.st_size
                    latest_mtime = max(latest_mtime, stat.st_mtime)
                except OSError:
                    continue
            info.total_size_bytes = total_size
            info.last_modified = (
                datetime.fromtimestamp(latest_mtime).isoformat(timespec="seconds") if latest_mtime else ""
            )
            info.readable = os.access(storage_dir, os.R_OK)
            info.writable = os.access(storage_dir, os.W_OK)
        except Exception as e:
            logger.warning(f"저장소 진단정보 수집 실패: {e}")
            info.error = "조회 실패"
        return info

    def _collect_logs(self) -> LogDiagnostics:
        info = LogDiagnostics()
        try:
            log_dir = get_log_dir()
            info.log_dir = log_dir
            log_files = [
                os.path.join(log_dir, f) for f in os.listdir(log_dir)
                if f.endswith(".log")
            ]
            if log_files:
                latest = max(log_files, key=os.path.getmtime)
                info.latest_log_filename = os.path.basename(latest)
                info.latest_log_modified = datetime.fromtimestamp(
                    os.path.getmtime(latest)
                ).isoformat(timespec="seconds")
        except Exception as e:
            logger.warning(f"로그 진단정보 수집 실패: {e}")
            info.error = "조회 실패"
        return info

    def _collect_license(self) -> LicenseDiagnostics:
        info = LicenseDiagnostics()
        try:
            from config.settings import LICENSE_ADMIN_UI_ENABLED
            info.admin_ui_enabled = LICENSE_ADMIN_UI_ENABLED
            valid, reason = self._license.verify_build_signature()
            info.valid = valid
            info.reason = "" if valid else reason
            license_path = get_external_license_path()  # 공개 함수 — LicenseManager와 동일한 경로 산출
            info.file_exists = os.path.exists(license_path)
        except Exception as e:
            logger.warning(f"라이선스 진단정보 수집 실패: {e}")
            info.error = "조회 실패"
        return info

    def _collect_backup(self) -> BackupDiagnostics:
        info = BackupDiagnostics()
        if self._backup is None:
            info.error = "백업 서비스 미구성"
            return info
        try:
            info.backup_dir = self._backup.get_backup_dir()
            backups = self._backup.list_backups()
            info.backup_count = len(backups)
            if backups:
                latest = backups[0]  # list_backups()는 최신순 정렬을 보장한다
                info.latest_backup_at = latest.created_at
                info.latest_backup_valid = latest.validated
        except Exception as e:
            logger.warning(f"백업 진단정보 수집 실패: {e}")
            info.error = "조회 실패"
        return info

    def _collect_message_source(self) -> MessageSourceDiagnostics:
        """요구사항 11.B — 지금 화면에 보이는 1~12번 메시지가 어느 시스템에서
        왔는지 보여준다(레거시 messages.json 폴백 vs shared_messages)."""
        info = MessageSourceDiagnostics()
        try:
            config = self._auth.client_manager.config
            info.shared_messages_enabled = config.shared_messages_enabled
            info.legacy_sync_enabled = config.legacy_messages_sync_enabled
            info.shared_messages_primary = config.shared_messages_primary
            info.fallback_cache_path = os.path.join(
                get_runtime_base_dir(), "storage", "cloud_sync", "messages.json",
            )
            if self._message_source_fn is not None:
                info.per_message_sources = dict(self._message_source_fn())
        except Exception as e:
            logger.warning(f"메시지 출처 진단정보 수집 실패: {e}")
            info.error = "조회 실패"
        return info

    # ===== 클립보드 복사용 텍스트 =====

    def to_copy_text(self, snapshot: DiagnosticsSnapshot) -> str:
        """진단정보 복사 버튼이 클립보드에 넣을 텍스트. 비밀정보를 포함하지 않는다."""
        lines = [
            f"환경: {'TEST ENVIRONMENT' if snapshot.app.is_test_environment else '운영(production)'}",
            "",
            "[애플리케이션]",
            f"이름: {snapshot.app.app_name}",
            f"버전: {snapshot.app.version}",
            f"채널: {snapshot.app.build_channel}",
            f"빌드 날짜: {snapshot.app.build_date}",
            f"Git commit: {snapshot.app.git_commit_short}",
            f"실행 모드: {snapshot.app.run_mode}",
            "",
            "[운영체제]",
            f"Windows: {snapshot.os_info.windows_version}",
            f"Python: {snapshot.os_info.python_version}",
            f"아키텍처: {snapshot.os_info.architecture}",
            "",
            "[인증]",
            f"로그인 상태: {'로그인됨' if snapshot.auth.logged_in else '로그인 안 됨'}",
            f"이메일: {snapshot.auth.email}",
            f"관리자 여부: {snapshot.auth.is_admin}",
            "",
            "[Supabase]",
            f"설정 여부: {snapshot.supabase.configured}",
            f"클라이언트 초기화: {snapshot.supabase.client_initialized}",
            f"호스트: {snapshot.supabase.masked_host}",
            f"네트워크 상태: {snapshot.supabase.network_status}",
            "",
            "[동기화]",
            f"Pending: {snapshot.sync.pending_count}",
            f"Conflict: {snapshot.sync.conflict_count}",
            f"상태: {snapshot.sync.last_sync_state}",
            "",
            "[저장소]",
            f"경로: {snapshot.storage.storage_path}",
            f"파일 수: {snapshot.storage.data_file_count}",
            f"전체 크기: {snapshot.storage.total_size_bytes} bytes",
            "",
            "[로그]",
            f"경로: {snapshot.logs.log_dir}",
            f"최신 로그: {snapshot.logs.latest_log_filename}",
            "",
            "[라이선스]",
            f"상태: {'정상' if snapshot.license.valid else (snapshot.license.reason or '확인 불가')}",
            "",
            "[백업]",
            f"최신 백업: {snapshot.backup.latest_backup_at}",
            f"백업 개수: {snapshot.backup.backup_count}",
            "",
            "[메시지 출처]",
            f"Message source: {'shared_messages' if snapshot.message_source.shared_messages_primary else 'legacy'}",
            f"Fallback cache: {snapshot.message_source.fallback_cache_path}",
            f"Legacy sync: {'enabled' if snapshot.message_source.legacy_sync_enabled else 'disabled'}",
        ]
        return "\n".join(lines)
