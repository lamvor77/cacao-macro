# 발송 직전 메시지 검증 — Production Stabilization Sprint
#
# core/scheduler.py가 그룹 발송을 시작하기 직전, 그 그룹에 실제로 필요한
# message_no 각각에 대해 이 모듈의 verify_message_before_send()를 호출한다.
# 전체 12건을 조회하지 않는다 — 발송 대상 번호만, 한 건씩 조회한다.
#
# 이 모듈은 Tkinter/GUI를 전혀 참조하지 않는다 — 순수 로직 + 주입받은
# fetch_fn(네트워크 호출)만 사용한다(core/shared_message_coordinator.py와 동일한
# 설계 원칙). fetch_fn은 services.shared_message_service.SharedMessageService.
# get_message과 같은 시그니처(message_no -> SharedMessageRecord | None, 실패 시
# SharedMessageError 계열 예외)를 가진 콜러블이면 무엇이든 된다 — 테스트에서는
# fake를 주입한다.

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class OfflineSendPolicy(Enum):
    """서버 확인에 실패했을 때의 정책. 기본값은 BLOCK(안전 우선)이다."""

    BLOCK = "block"
    CACHED = "cached"

    @classmethod
    def from_str(cls, value: str) -> "OfflineSendPolicy":
        try:
            return cls(value.strip().lower())
        except ValueError:
            logger.warning(f"알 수 없는 MESSAGE_SEND_OFFLINE_POLICY 값({value!r}) — block으로 처리합니다.")
            return cls.BLOCK


class VerificationSource(Enum):
    SERVER = "server"
    LOCAL_CACHE = "local_cache"
    UNAVAILABLE = "unavailable"


class VerificationErrorCode(Enum):
    NETWORK_ERROR = "NETWORK_ERROR"
    AUTH_ERROR = "AUTH_ERROR"
    MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    REVISION_ERROR = "REVISION_ERROR"
    SERVICE_DISABLED = "SERVICE_DISABLED"
    # legacy messages 발송 직전 검증 전용(core/legacy_send_verification.py) —
    # shared_messages 쪽은 revision 비교만으로 충분해 이 두 값을 쓰지 않는다.
    CONFLICT = "CONFLICT"  # 로컬 미저장 변경과 원격이 실제로 다른 값으로 갈라짐
    EDIT_IN_PROGRESS = "EDIT_IN_PROGRESS"  # 원격이 더 최신인데 사용자가 편집 중이라 적용 보류


@dataclass
class SendMessageVerificationResult:
    """발송 직전 검증 결과 — bool 하나가 아니라 발송 로직/UI가 필요로 하는
    모든 정보를 담는다."""

    allowed: bool
    message_no: int
    content: str
    local_revision: int
    server_revision: Optional[int]
    source: VerificationSource
    used_cached_content: bool
    error_code: Optional[VerificationErrorCode] = None
    error_message: Optional[str] = None
    verified_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _classify_error(exc: Exception) -> tuple:
    """services.shared_message_service의 예외 계층을 오류 코드로 변환한다.
    이 모듈이 그 서비스 모듈을 무조건 import하지 않도록(계층 분리, 테스트에서
    fetch_fn에 순수 예외만 던지는 fake를 쓸 수 있도록) 함수 안에서 지연 import한다."""
    try:
        from services.shared_message_service import (
            SharedMessageError, SharedMessagePermissionError, SharedMessageValidationError,
        )
    except ImportError:
        return VerificationErrorCode.NETWORK_ERROR, str(exc)

    if isinstance(exc, SharedMessagePermissionError):
        return VerificationErrorCode.AUTH_ERROR, str(exc)
    if isinstance(exc, SharedMessageValidationError):
        return VerificationErrorCode.REVISION_ERROR, str(exc)
    if isinstance(exc, SharedMessageError):
        return VerificationErrorCode.NETWORK_ERROR, str(exc)
    return VerificationErrorCode.NETWORK_ERROR, str(exc)


def verify_message_before_send(
    message_no: int,
    local_content: str,
    local_revision: int,
    fetch_fn: Callable[[int], object],
    policy: OfflineSendPolicy,
    timeout_seconds: float,
    retry_count: int,
    service_enabled: bool,
) -> SendMessageVerificationResult:
    """발송 흐름 1~7단계(요구사항 4절)를 수행한다.

    1. message_no 확인(호출부가 이미 결정해 인자로 넘김)
    2. fetch_fn(message_no)로 서버 조회
    3~4. server_revision > local_revision이면 서버 content 사용
    5. 반환된 content가 실제 발송에 쓰일 최종 content
    6. revision이 같으면(서버도 로컬과 일치) 그대로 서버 content 사용 — 사실상 캐시와 동일값
    7. 조회 실패 시 policy에 따라 발송 보류(BLOCK) 또는 캐시 발송(CACHED)

    타임아웃(timeout_seconds)은 fetch_fn 자체의 라이브러리 타임아웃 설정과
    무관하게, 이 함수가 별도 스레드에서 fetch_fn을 실행해 강제한다 — 어떤
    이유로든 fetch_fn이 너무 오래 걸리면(네트워크 스택 문제 등) 정해진 시간
    안에 반드시 TIMEOUT으로 판정한다.
    """
    if not service_enabled:
        return SendMessageVerificationResult(
            allowed=True, message_no=message_no, content=local_content,
            local_revision=local_revision, server_revision=None,
            source=VerificationSource.LOCAL_CACHE, used_cached_content=True,
            error_code=VerificationErrorCode.SERVICE_DISABLED,
            error_message="shared_messages 동기화가 비활성화되어 있습니다.",
            verified_at=_now_iso(),
        )

    last_error_code = VerificationErrorCode.NETWORK_ERROR
    last_error_message = ""
    attempts = max(1, retry_count + 1)

    for attempt in range(1, attempts + 1):
        # ThreadPoolExecutor를 with 블록으로 감싸면 __exit__이 기본적으로
        # shutdown(wait=True)를 호출해, fetch_fn이 timeout_seconds를 넘겨도
        # "그 스레드가 실제로 끝날 때까지" 이 함수가 계속 블록된다 — 우리가
        # 원하는 타임아웃 의미(정해진 시간 안에 반드시 판정)와 정반대다. 그래서
        # with를 쓰지 않고, 타임아웃/오류가 나면 shutdown(wait=False)로 이
        # 함수 자체는 즉시 반환하게 한다(내부 스레드는 fetch_fn이 스스로
        # 끝나거나 실패할 때까지 백그라운드에 남을 수 있음 — Python 스레드는
        # 강제 종료할 수 없다는 근본적 한계다. 실제 fetch_fn은 httpx 자체
        # 타임아웃을 갖고 있어 무한정 남지는 않는다).
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(fetch_fn, message_no)
            record = future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            last_error_code = VerificationErrorCode.TIMEOUT
            last_error_message = f"서버 확인 시간 초과({timeout_seconds}초, 시도 {attempt}/{attempts})"
            logger.warning(f"발송 직전 검증 시간 초과 — message_no={message_no}, 시도 {attempt}/{attempts}")
            pool.shutdown(wait=False)
            continue
        except Exception as e:
            last_error_code, last_error_message = _classify_error(e)
            logger.warning(
                f"발송 직전 검증 실패 — message_no={message_no}, 시도 {attempt}/{attempts}, "
                f"오류 유형={type(e).__name__}"
            )
            pool.shutdown(wait=False)
            continue
        else:
            pool.shutdown(wait=False)

        if record is None:
            last_error_code = VerificationErrorCode.MESSAGE_NOT_FOUND
            last_error_message = f"message_no={message_no}를 서버에서 찾을 수 없습니다."
            logger.warning(f"발송 직전 검증 — message_no={message_no} 서버에 존재하지 않음")
            continue

        return SendMessageVerificationResult(
            allowed=True, message_no=message_no, content=record.content,
            local_revision=local_revision, server_revision=record.revision,
            source=VerificationSource.SERVER, used_cached_content=False,
            verified_at=_now_iso(),
        )

    # 모든 시도 실패 — 정책에 따라 처리(요구사항 5절).
    if policy == OfflineSendPolicy.CACHED:
        logger.warning(
            f"발송 직전 검증 실패 — cached 정책으로 캐시 내용 사용 — message_no={message_no}, "
            f"오류={last_error_code.value}"
        )
        return SendMessageVerificationResult(
            allowed=True, message_no=message_no, content=local_content,
            local_revision=local_revision, server_revision=None,
            source=VerificationSource.LOCAL_CACHE, used_cached_content=True,
            error_code=last_error_code, error_message=last_error_message,
            verified_at=_now_iso(),
        )

    logger.warning(
        f"발송 직전 검증 실패 — block 정책으로 발송 보류 — message_no={message_no}, "
        f"오류={last_error_code.value}"
    )
    return SendMessageVerificationResult(
        allowed=False, message_no=message_no, content=local_content,
        local_revision=local_revision, server_revision=None,
        source=VerificationSource.UNAVAILABLE, used_cached_content=False,
        error_code=last_error_code, error_message=last_error_message,
        verified_at=_now_iso(),
    )
