# 클라우드 동기화(Supabase) 설정
# 환경변수(.env 포함)에서 값을 읽는다. 민감정보(URL, key)를 소스코드에
# 직접 하드코딩하지 않기 위해 config/settings.py와 별도 파일로 분리했다.
#
# SUPABASE_ENABLED가 False이거나 필수 값(URL, ANON_KEY)이 비어있으면
# 클라우드 기능은 전부 비활성화되며, 이 경우 프로그램은 기존 로컬 전용
# 동작과 완전히 동일하게 실행되어야 한다 (Phase 2 핵심 원칙).

import logging
import os
import socket
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# python-dotenv가 설치되어 있으면 .env 파일을 읽어 os.environ에 반영한다.
# 설치되어 있지 않거나 .env 파일이 없어도 프로그램 실행에는 영향이 없다
# (이 경우 시스템 환경변수만 사용되고, 그마저도 없으면 아래 기본값이 적용된다).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    logger.debug("python-dotenv가 설치되어 있지 않습니다 — 시스템 환경변수만 사용합니다.")


# ===== 기본값 =====
DEFAULT_SUPABASE_ENABLED: bool = False
DEFAULT_TIMEOUT_SECONDS: float = 5.0
_MESSAGES_TABLE: str = "messages"

# ===== Phase 2E: 로컬 자동 저장 + 클라우드 조건부 동기화 기본값 =====
DEFAULT_MESSAGE_LOCAL_AUTOSAVE_ENABLED: bool = True
DEFAULT_MESSAGE_LOCAL_AUTOSAVE_DELAY_MS: int = 2000
DEFAULT_MESSAGE_CLOUD_SYNC_INTERVAL_SECONDS: int = 900
DEFAULT_MESSAGE_CLOUD_SYNC_ON_SEND_START: bool = True
DEFAULT_MESSAGE_CLOUD_SYNC_ON_EXIT: bool = True
DEFAULT_MESSAGE_CLOUD_SYNC_EXIT_WAIT_SECONDS: float = 2.0

_MIN_AUTOSAVE_DELAY_MS: int = 300
_MIN_CLOUD_SYNC_INTERVAL_SECONDS: int = 60
_MIN_EXIT_WAIT_SECONDS: float = 0.0
_MAX_EXIT_WAIT_SECONDS: float = 5.0

# ===== Production Stabilization Sprint: 발송 직전 검증 정책 기본값 =====
DEFAULT_SEND_OFFLINE_POLICY: str = "block"
DEFAULT_SEND_VERIFY_TIMEOUT_SECONDS: float = 5.0
DEFAULT_SEND_VERIFY_RETRY_COUNT: int = 2
DEFAULT_MESSAGE_SYNC_DEBUG: bool = False
# Realtime protocol 2.0(배열 메시지) 호환 계층 즉시 비활성화 스위치
# (services/realtime_protocol_compat.py 참고) — 기본 true(호환 계층 사용).
DEFAULT_REALTIME_PROTOCOL_COMPAT: bool = True
_VALID_SEND_OFFLINE_POLICIES = ("block", "cached")
_MIN_SEND_VERIFY_TIMEOUT_SECONDS: float = 1.0
_MAX_SEND_VERIFY_TIMEOUT_SECONDS: float = 30.0
_MIN_SEND_VERIFY_RETRY_COUNT: int = 0
_MAX_SEND_VERIFY_RETRY_COUNT: int = 5


@dataclass(frozen=True)
class CloudConfig:
    """클라우드 동기화 설정값 (환경변수로부터 만들어진 읽기 전용 스냅샷)"""

    enabled: bool
    url: str
    anon_key: str
    device_id: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    messages_table: str = _MESSAGES_TABLE
    # Mobile 실시간 동기화 스프린트: SUPABASE_ENABLED와 별개로 Realtime 구독만
    # 끌 수 있는 독립 스위치(기본 true — enabled=true면 realtime도 기본 동작).
    # legacy messages 동기화(CloudSyncCoordinator)에는 영향을 주지 않는다 —
    # 이 값은 services/realtime_message_sync_service.py 시작 여부만 결정한다.
    realtime_enabled: bool = True
    # ===== Production Stabilization Sprint 11절: 레거시/신규 이원 체계 전환 플래그 =====
    # 지금 당장은 셋 다 사실상 항상 True로 둔다(레거시 유지 + shared_messages 추가) —
    # 실제 전환(레거시 폐기)을 결정할 때 이 플래그들로 단계적으로 끌 수 있도록
    # 미리 자리만 마련해 둔 것이다. docs/legacy_messages_migration_plan.md 참고.
    shared_messages_enabled: bool = True
    legacy_messages_sync_enabled: bool = True
    shared_messages_primary: bool = True


@dataclass(frozen=True)
class MessageSyncConfig:
    """Phase 2E: 메시지 입력창 로컬 자동 저장 + 클라우드 조건부 동기화 설정값.

    SUPABASE_ENABLED=false여도 local_autosave_enabled 자체는 클라우드와 무관하게
    동작한다(로컬 저장은 네트워크 상태와 무관해야 한다는 원칙) — 클라우드 관련
    필드(cloud_sync_interval_seconds 등)는 SUPABASE_ENABLED=false면 애초에
    CloudSyncCoordinator.request_push()가 아무 것도 하지 않으므로 자연히 무효화된다.
    """

    local_autosave_enabled: bool
    local_autosave_delay_ms: int
    cloud_sync_interval_seconds: int
    cloud_sync_on_send_start: bool
    cloud_sync_on_exit: bool
    cloud_sync_exit_wait_seconds: float
    # ===== Production Stabilization Sprint: 발송 직전 검증 정책 =====
    # block: 서버 확인 실패 시 해당 발송 건을 보내지 않는다(기본값, 안전 우선).
    # cached: 서버 확인 실패 시 마지막으로 알려진 캐시 내용으로 발송한다.
    send_offline_policy: str = "block"
    send_verify_timeout_seconds: float = 5.0
    send_verify_retry_count: int = 2
    # MESSAGE_SYNC_DEBUG — true면 상태 전환/message_no/revision/이벤트 종류/재연결
    # 횟수/발송 검증 결과 등 세부 로그를 남긴다. 메시지 본문/토큰/키는 이 값과
    # 무관하게 항상 로그에 남기지 않는다(core/scheduler.py, services/
    # realtime_message_sync_service.py, gui/main_window.py 참고).
    sync_debug_enabled: bool = False
    # Realtime protocol 2.0(배열 메시지) 호환 계층 사용 여부. false로 두면
    # services/realtime_protocol_compat.py의 CompatAsyncRealtimeClient 대신
    # 원본 AsyncRealtimeClient를 그대로 쓴다(즉시 원상복구용 안전장치).
    realtime_protocol_compat_enabled: bool = True


def _get_bool_env(name: str, default: bool) -> bool:
    """환경변수를 불리언으로 해석한다. 값이 없거나 인식할 수 없으면 기본값을 사용한다."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning(f"{name} 값이 정수가 아닙니다 (입력값: {raw!r}) — 기본값 {default}을(를) 사용합니다.")
        return default


def _get_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning(f"{name} 값이 숫자가 아닙니다 (입력값: {raw!r}) — 기본값 {default}을(를) 사용합니다.")
        return default


def _default_device_id() -> str:
    """SUPABASE_DEVICE_ID가 없을 때 PC 이름 기반 기본 식별자를 만든다."""
    try:
        return f"pc-{socket.gethostname()}"
    except OSError:
        return "pc-unknown"


def get_cloud_config() -> CloudConfig:
    """환경변수에서 클라우드 동기화 설정을 읽어 반환한다.

    호출될 때마다 os.environ을 다시 읽으므로, 관리자 패널 등에서 런타임에
    .env를 갱신한 뒤 다시 호출하면 최신 값을 반영할 수 있다.

    SUPABASE_ENABLED=True이지만 URL/ANON_KEY가 비어있는 잘못된 설정인
    경우, 예외를 던지지 않고 enabled=False로 강제 전환한 뒤 경고 로그만
    남긴다 (앱 실행을 막지 않기 위함).
    """
    enabled = _get_bool_env("SUPABASE_ENABLED", DEFAULT_SUPABASE_ENABLED)
    url = os.environ.get("SUPABASE_URL", "").strip()
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if os.environ.get("SUPABASE_SYNC_INTERVAL_SECONDS", "").strip():
        # legacy messages의 상시 30초 pull polling을 제거하면서 이 값은 더
        # 이상 어디에도 쓰이지 않는다(요구사항) — 조용히 무시하지 않고, 설정
        # 파일을 아직 정리하지 않은 운영자가 알아챌 수 있도록 경고만 남긴다.
        logger.warning(
            "SUPABASE_SYNC_INTERVAL_SECONDS는 더 이상 사용되지 않습니다(상시 폴링이 "
            "제거됨) — .env에서 이 값을 지워도 됩니다. 현재는 아무 동작에도 영향을 "
            "주지 않습니다."
        )
    device_id = os.environ.get("SUPABASE_DEVICE_ID", "").strip() or _default_device_id()
    realtime_enabled = _get_bool_env("SUPABASE_REALTIME_ENABLED", True)
    shared_messages_enabled = _get_bool_env("SHARED_MESSAGES_ENABLED", True)
    legacy_messages_sync_enabled = _get_bool_env("LEGACY_MESSAGES_SYNC_ENABLED", True)
    shared_messages_primary = _get_bool_env("SHARED_MESSAGES_PRIMARY", True)

    if enabled and (not url or not anon_key):
        logger.warning(
            "SUPABASE_ENABLED=true이지만 SUPABASE_URL 또는 SUPABASE_ANON_KEY가 "
            "비어있어 클라우드 동기화를 비활성화합니다. 로컬 전용 모드로 동작합니다."
        )
        enabled = False

    return CloudConfig(
        enabled=enabled,
        url=url,
        anon_key=anon_key,
        device_id=device_id,
        realtime_enabled=realtime_enabled,
        shared_messages_enabled=shared_messages_enabled,
        legacy_messages_sync_enabled=legacy_messages_sync_enabled,
        shared_messages_primary=shared_messages_primary,
    )


def get_message_sync_config() -> MessageSyncConfig:
    """환경변수에서 Phase 2E 로컬 자동 저장/클라우드 조건부 동기화 설정을 읽는다.

    잘못된 값(범위 밖)은 예외를 던지지 않고 안전한 기본값으로 대체한 뒤
    경고 로그만 남긴다(앱 실행을 막지 않기 위함 — get_cloud_config()와 동일한 원칙).
    """
    delay_ms = _get_int_env("MESSAGE_LOCAL_AUTOSAVE_DELAY_MS", DEFAULT_MESSAGE_LOCAL_AUTOSAVE_DELAY_MS)
    if delay_ms < _MIN_AUTOSAVE_DELAY_MS:
        logger.warning(
            f"MESSAGE_LOCAL_AUTOSAVE_DELAY_MS 값({delay_ms})이 최소값({_MIN_AUTOSAVE_DELAY_MS}) 미만입니다 — "
            f"기본값 {DEFAULT_MESSAGE_LOCAL_AUTOSAVE_DELAY_MS}을(를) 사용합니다."
        )
        delay_ms = DEFAULT_MESSAGE_LOCAL_AUTOSAVE_DELAY_MS

    interval_seconds = _get_int_env(
        "MESSAGE_CLOUD_SYNC_INTERVAL_SECONDS", DEFAULT_MESSAGE_CLOUD_SYNC_INTERVAL_SECONDS
    )
    if interval_seconds < _MIN_CLOUD_SYNC_INTERVAL_SECONDS:
        logger.warning(
            f"MESSAGE_CLOUD_SYNC_INTERVAL_SECONDS 값({interval_seconds})이 "
            f"최소값({_MIN_CLOUD_SYNC_INTERVAL_SECONDS}) 미만입니다 — "
            f"기본값 {DEFAULT_MESSAGE_CLOUD_SYNC_INTERVAL_SECONDS}을(를) 사용합니다."
        )
        interval_seconds = DEFAULT_MESSAGE_CLOUD_SYNC_INTERVAL_SECONDS

    exit_wait_seconds = _get_float_env(
        "MESSAGE_CLOUD_SYNC_EXIT_WAIT_SECONDS", DEFAULT_MESSAGE_CLOUD_SYNC_EXIT_WAIT_SECONDS
    )
    if not (_MIN_EXIT_WAIT_SECONDS <= exit_wait_seconds <= _MAX_EXIT_WAIT_SECONDS):
        logger.warning(
            f"MESSAGE_CLOUD_SYNC_EXIT_WAIT_SECONDS 값({exit_wait_seconds})이 "
            f"허용 범위({_MIN_EXIT_WAIT_SECONDS}~{_MAX_EXIT_WAIT_SECONDS}) 밖입니다 — "
            f"기본값 {DEFAULT_MESSAGE_CLOUD_SYNC_EXIT_WAIT_SECONDS}을(를) 사용합니다."
        )
        exit_wait_seconds = DEFAULT_MESSAGE_CLOUD_SYNC_EXIT_WAIT_SECONDS

    offline_policy = os.environ.get("MESSAGE_SEND_OFFLINE_POLICY", "").strip().lower() or DEFAULT_SEND_OFFLINE_POLICY
    if offline_policy not in _VALID_SEND_OFFLINE_POLICIES:
        logger.warning(
            f"MESSAGE_SEND_OFFLINE_POLICY 값({offline_policy!r})이 올바르지 않습니다 — "
            f"기본값 {DEFAULT_SEND_OFFLINE_POLICY!r}(안전 우선)을 사용합니다."
        )
        offline_policy = DEFAULT_SEND_OFFLINE_POLICY

    verify_timeout = _get_float_env("MESSAGE_SEND_VERIFY_TIMEOUT_SECONDS", DEFAULT_SEND_VERIFY_TIMEOUT_SECONDS)
    if not (_MIN_SEND_VERIFY_TIMEOUT_SECONDS <= verify_timeout <= _MAX_SEND_VERIFY_TIMEOUT_SECONDS):
        logger.warning(
            f"MESSAGE_SEND_VERIFY_TIMEOUT_SECONDS 값({verify_timeout})이 "
            f"허용 범위({_MIN_SEND_VERIFY_TIMEOUT_SECONDS}~{_MAX_SEND_VERIFY_TIMEOUT_SECONDS}) 밖입니다 — "
            f"기본값 {DEFAULT_SEND_VERIFY_TIMEOUT_SECONDS}을(를) 사용합니다."
        )
        verify_timeout = DEFAULT_SEND_VERIFY_TIMEOUT_SECONDS

    verify_retry_count = _get_int_env("MESSAGE_SEND_VERIFY_RETRY_COUNT", DEFAULT_SEND_VERIFY_RETRY_COUNT)
    if not (_MIN_SEND_VERIFY_RETRY_COUNT <= verify_retry_count <= _MAX_SEND_VERIFY_RETRY_COUNT):
        logger.warning(
            f"MESSAGE_SEND_VERIFY_RETRY_COUNT 값({verify_retry_count})이 "
            f"허용 범위({_MIN_SEND_VERIFY_RETRY_COUNT}~{_MAX_SEND_VERIFY_RETRY_COUNT}) 밖입니다 — "
            f"기본값 {DEFAULT_SEND_VERIFY_RETRY_COUNT}을(를) 사용합니다."
        )
        verify_retry_count = DEFAULT_SEND_VERIFY_RETRY_COUNT

    return MessageSyncConfig(
        local_autosave_enabled=_get_bool_env(
            "MESSAGE_LOCAL_AUTOSAVE_ENABLED", DEFAULT_MESSAGE_LOCAL_AUTOSAVE_ENABLED
        ),
        local_autosave_delay_ms=delay_ms,
        cloud_sync_interval_seconds=interval_seconds,
        cloud_sync_on_send_start=_get_bool_env(
            "MESSAGE_CLOUD_SYNC_ON_SEND_START", DEFAULT_MESSAGE_CLOUD_SYNC_ON_SEND_START
        ),
        cloud_sync_on_exit=_get_bool_env("MESSAGE_CLOUD_SYNC_ON_EXIT", DEFAULT_MESSAGE_CLOUD_SYNC_ON_EXIT),
        cloud_sync_exit_wait_seconds=exit_wait_seconds,
        send_offline_policy=offline_policy,
        send_verify_timeout_seconds=verify_timeout,
        send_verify_retry_count=verify_retry_count,
        sync_debug_enabled=_get_bool_env("MESSAGE_SYNC_DEBUG", DEFAULT_MESSAGE_SYNC_DEBUG),
        realtime_protocol_compat_enabled=_get_bool_env(
            "REALTIME_PROTOCOL_COMPAT", DEFAULT_REALTIME_PROTOCOL_COMPAT
        ),
    )
