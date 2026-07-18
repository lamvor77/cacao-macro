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
# Phase 2C: CloudSyncCoordinator의 폴링 주기 기본값. 기존 SUPABASE_SYNC_INTERVAL_SECONDS
# 변수를 그대로 재사용한다(새 변수를 만들지 않음) — 기본값만 60→30초로 조정했다.
DEFAULT_SYNC_INTERVAL_SECONDS: int = 30
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


@dataclass(frozen=True)
class CloudConfig:
    """클라우드 동기화 설정값 (환경변수로부터 만들어진 읽기 전용 스냅샷)"""

    enabled: bool
    url: str
    anon_key: str
    sync_interval_seconds: int
    device_id: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    messages_table: str = _MESSAGES_TABLE


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
    sync_interval = _get_int_env("SUPABASE_SYNC_INTERVAL_SECONDS", DEFAULT_SYNC_INTERVAL_SECONDS)
    device_id = os.environ.get("SUPABASE_DEVICE_ID", "").strip() or _default_device_id()

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
        sync_interval_seconds=sync_interval,
        device_id=device_id,
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
    )
