# legacy messages(구 30초 폴링 시스템) 발송 직전 검증
#
# CloudSyncCoordinator의 상시 30초 pull polling을 제거하면서, 그 폴링이
# 담당하던 "메시지별 로컬/원격 상태 분류"(REMOTE_APPLY/PUSH/IDENTICAL/
# LOCAL_PENDING/CONFLICT) 판정 로직 자체는 옮기지 않고 그대로 재사용한다 —
# classify_message()가 기존 CloudSyncCoordinator._reconcile()의 판정 규칙과
# 완전히 동일하다(로직 변경 없음, 호출 빈도만 "30초마다 12건 전부"에서
# "필요한 시점에 그 메시지 1건만"으로 바뀐다).
#
# 이 모듈은 core/send_verification.py(shared_messages용)와 같은 설계 원칙을
# 따른다 — Tkinter/GUI를 참조하지 않고, 네트워크는 주입받은 fetch_fn/push_fn을
# 통해서만 수행하며, 결과는 예외가 아니라 core.send_verification의
# SendMessageVerificationResult로 반환한다(타입을 새로 만들지 않고 재사용 —
# AutoScheduler/core/scheduler.py가 이미 이 타입만 알고 있으므로 그쪽은 전혀
# 수정하지 않아도 된다).

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from core.send_verification import (
    OfflineSendPolicy,
    SendMessageVerificationResult,
    VerificationErrorCode,
    VerificationSource,
)

logger = logging.getLogger(__name__)


class MessageClassification(Enum):
    """CloudSyncCoordinator._reconcile()의 6가지 판정 규칙을 그대로 옮긴 것 —
    로직은 이전과 동일하고, 호출 위치와 빈도만 바뀐다."""

    PUSH = "push"                    # 클라우드에 아직 없음(cloud_version=None) — 로컬 내용을 올려야 함
    NOOP = "noop"                    # 할 일 없음(변경 없음, 또는 클라우드가 로컬보다 새롭지 않음)
    REMOTE_APPLY = "remote_apply"    # dirty 아님 + 원격이 더 최신 → 원격을 적용
    IDENTICAL = "identical"          # dirty지만 이미 원격과 같은 내용으로 수렴함
    LOCAL_PENDING = "local_pending"  # dirty + 원격이 그대로(=내 이전 push가 아직 반영 안 됐을 뿐)
    CONFLICT = "conflict"            # dirty + 원격이 실제로 다른 값으로 바뀜


def classify_message(
    dirty: bool,
    local_version: int,
    last_synced_text: str,
    local_text: str,
    cloud_version: Optional[int],
    cloud_text: Optional[str],
) -> MessageClassification:
    """순수 함수 — CloudSyncCoordinator._local_state와 CloudSyncService의
    MessageRecord를 그대로 전달하지 않고 필요한 값만 인자로 받는다(core/가
    services/의 dataclass에 의존하지 않도록 하기 위함, 순환 임포트 방지).

    cloud_version=None은 "이 message_no에 대한 클라우드 행 자체가 없음"을 뜻한다.
    """
    if cloud_version is None:
        return MessageClassification.PUSH if local_text.strip() else MessageClassification.NOOP

    if not dirty:
        return MessageClassification.REMOTE_APPLY if cloud_version > local_version else MessageClassification.NOOP

    if local_text == cloud_text:
        return MessageClassification.IDENTICAL

    if cloud_version <= local_version or cloud_text == last_synced_text:
        return MessageClassification.LOCAL_PENDING

    return MessageClassification.CONFLICT


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fetch_with_retry(
    fetch_fn: Callable[[int], object],
    message_no: int,
    timeout_seconds: float,
    retry_count: int,
) -> tuple:
    """(record_or_None, error_code, error_message) 튜플을 반환한다. 성공하면
    error_code=None. record가 None이면(메시지가 서버에 없음) MESSAGE_NOT_FOUND로
    분류한다 — core/send_verification.py의 동일 패턴을 그대로 따른다(fresh
    ThreadPoolExecutor를 매 시도마다 새로 만들고 wait=False로 종료 — with 블록을
    쓰면 __exit__이 timeout을 무력화하는 버그를 이미 겪었다, 동일하게 회피)."""
    last_error_code = VerificationErrorCode.NETWORK_ERROR
    last_error_message = ""
    attempts = max(1, retry_count + 1)

    for attempt in range(1, attempts + 1):
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(fetch_fn, message_no)
            record = future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            last_error_code = VerificationErrorCode.TIMEOUT
            last_error_message = f"서버 확인 시간 초과({timeout_seconds}초, 시도 {attempt}/{attempts})"
            logger.warning(f"legacy 발송 직전 검증 시간 초과 — message_no={message_no}, 시도 {attempt}/{attempts}")
            pool.shutdown(wait=False)
            continue
        except Exception as e:
            last_error_code = VerificationErrorCode.NETWORK_ERROR
            last_error_message = str(e)
            logger.warning(
                f"legacy 발송 직전 검증 실패 — message_no={message_no}, 시도 {attempt}/{attempts}, "
                f"오류 유형={type(e).__name__}"
            )
            pool.shutdown(wait=False)
            continue
        else:
            pool.shutdown(wait=False)

        if record is None:
            last_error_code = VerificationErrorCode.MESSAGE_NOT_FOUND
            last_error_message = f"message_no={message_no}를 서버에서 찾을 수 없습니다."
            logger.warning(f"legacy 발송 직전 검증 — message_no={message_no} 서버에 존재하지 않음")
            continue

        return record, None, ""

    return None, last_error_code, last_error_message


def _offline_policy_result(
    message_no: int, local_content: str, local_version: int,
    policy: OfflineSendPolicy, error_code: VerificationErrorCode, error_message: str,
) -> SendMessageVerificationResult:
    if policy == OfflineSendPolicy.CACHED:
        logger.warning(
            f"legacy 발송 직전 검증 실패 — cached 정책으로 캐시 내용 사용 — message_no={message_no}, "
            f"오류={error_code.value}"
        )
        return SendMessageVerificationResult(
            allowed=True, message_no=message_no, content=local_content,
            local_revision=local_version, server_revision=None,
            source=VerificationSource.LOCAL_CACHE, used_cached_content=True,
            error_code=error_code, error_message=error_message, verified_at=_now_iso(),
        )
    logger.warning(
        f"legacy 발송 직전 검증 실패 — block 정책으로 발송 보류 — message_no={message_no}, "
        f"오류={error_code.value}"
    )
    return SendMessageVerificationResult(
        allowed=False, message_no=message_no, content=local_content,
        local_revision=local_version, server_revision=None,
        source=VerificationSource.UNAVAILABLE, used_cached_content=False,
        error_code=error_code, error_message=error_message, verified_at=_now_iso(),
    )


def verify_legacy_message_before_send(
    message_no: int,
    local_content: str,
    is_dirty: bool,
    local_version: int,
    last_synced_text: str,
    is_editing: bool,
    fetch_fn: Callable[[int], object],
    push_fn: Callable[[int, str], bool],
    policy: OfflineSendPolicy,
    timeout_seconds: float,
    retry_count: int,
    service_enabled: bool,
) -> SendMessageVerificationResult:
    """legacy messages 1건을 발송 직전에 검증한다.

    판정(요구사항 그대로):
      - local clean + remote newer(REMOTE_APPLY): 편집 중이 아니면 원격 적용 후
        그 내용으로 발송. 편집 중이면 절대 조용히 덮어쓰지 않고 발송 자체를
        중단한다(error_code=EDIT_IN_PROGRESS).
      - local dirty + remote unchanged(LOCAL_PENDING): push_fn으로 로컬 내용을
        먼저 서버에 반영한 뒤, 성공해야만 그 내용으로 발송한다. push 실패 시
        발송을 중단한다(내용이 서버에 안전하게 반영됐다고 보장할 수 없으므로).
      - local dirty + remote changed(CONFLICT): 발송 중단(error_code=CONFLICT).
      - 서버 조회 자체가 실패(네트워크/타임아웃/미존재): policy(기본 block)를
        따른다 — shared_messages와 완전히 동일한 정책 값을 공유해서 쓴다.
    fetch_fn(message_no)는 .version/.text 속성을 가진 객체 또는 None을 반환해야
    한다(services.cloud_sync_service.MessageRecord와 동일한 형태 — 이 모듈은
    그 타입을 직접 import하지 않고 속성만 사용한다, core/가 services/에
    의존하지 않도록 하기 위함).
    push_fn(message_no, content)은 성공하면 True를 반환해야 한다(동기 호출 —
    이 함수 자체가 이미 스케줄러의 백그라운드 스레드에서 호출되므로 블로킹해도
    안전하다).
    """
    if not service_enabled:
        return SendMessageVerificationResult(
            allowed=True, message_no=message_no, content=local_content,
            local_revision=local_version, server_revision=None,
            source=VerificationSource.LOCAL_CACHE, used_cached_content=True,
            error_code=VerificationErrorCode.SERVICE_DISABLED,
            error_message="클라우드 동기화가 비활성화되어 있습니다.",
            verified_at=_now_iso(),
        )

    record, error_code, error_message = _fetch_with_retry(fetch_fn, message_no, timeout_seconds, retry_count)
    if record is None:
        return _offline_policy_result(message_no, local_content, local_version, policy, error_code, error_message)

    classification = classify_message(
        dirty=is_dirty, local_version=local_version, last_synced_text=last_synced_text,
        local_text=local_content, cloud_version=record.version, cloud_text=record.text,
    )

    if classification in (MessageClassification.NOOP, MessageClassification.IDENTICAL):
        return SendMessageVerificationResult(
            allowed=True, message_no=message_no, content=local_content,
            local_revision=local_version, server_revision=record.version,
            source=VerificationSource.SERVER, used_cached_content=False,
            verified_at=_now_iso(),
        )

    if classification == MessageClassification.REMOTE_APPLY:
        if is_editing:
            logger.warning(f"legacy 발송 직전 검증 — message_no={message_no} 편집 중 원격 변경으로 발송 중단")
            return SendMessageVerificationResult(
                allowed=False, message_no=message_no, content=local_content,
                local_revision=local_version, server_revision=record.version,
                source=VerificationSource.UNAVAILABLE, used_cached_content=False,
                error_code=VerificationErrorCode.EDIT_IN_PROGRESS,
                error_message="서버에 최신 변경사항이 있습니다. 편집을 종료하고 새로고침하세요.",
                verified_at=_now_iso(),
            )
        return SendMessageVerificationResult(
            allowed=True, message_no=message_no, content=record.text,
            local_revision=local_version, server_revision=record.version,
            source=VerificationSource.SERVER, used_cached_content=False,
            verified_at=_now_iso(),
        )

    if classification == MessageClassification.LOCAL_PENDING:
        try:
            push_ok = push_fn(message_no, local_content)
        except Exception as e:
            logger.warning(f"legacy 발송 직전 push 실패 — message_no={message_no}: {type(e).__name__}")
            push_ok = False
        if not push_ok:
            return SendMessageVerificationResult(
                allowed=False, message_no=message_no, content=local_content,
                local_revision=local_version, server_revision=record.version,
                source=VerificationSource.UNAVAILABLE, used_cached_content=False,
                error_code=VerificationErrorCode.NETWORK_ERROR,
                error_message="변경사항을 서버에 저장하지 못해 발송을 중단합니다.",
                verified_at=_now_iso(),
            )
        return SendMessageVerificationResult(
            allowed=True, message_no=message_no, content=local_content,
            local_revision=local_version, server_revision=record.version,
            source=VerificationSource.SERVER, used_cached_content=False,
            verified_at=_now_iso(),
        )

    # CONFLICT
    logger.warning(f"legacy 발송 직전 검증 — message_no={message_no} 충돌로 발송 중단")
    return SendMessageVerificationResult(
        allowed=False, message_no=message_no, content=local_content,
        local_revision=local_version, server_revision=record.version,
        source=VerificationSource.UNAVAILABLE, used_cached_content=False,
        error_code=VerificationErrorCode.CONFLICT,
        error_message="다른 곳에서 이미 이 메시지를 수정했습니다 — 발송을 중단합니다. 새로고침 후 확인하세요.",
        verified_at=_now_iso(),
    )
