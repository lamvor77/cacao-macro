# 1~12번 공유 메시지(shared_messages) RPC 래퍼 — 모바일 실시간 동기화 스프린트
#
# docs/sql/shared_messages_realtime.sql의 update_shared_message/
# force_update_shared_message RPC와 UI(PC ControlPanel, 모바일 웹) 사이의 얇은
# 계층이다. services/admin_service.py와 동일한 설계 원칙을 따른다:
#   - 실제 권한 검사(fn_can_edit()/fn_is_admin())와 이력 기록은 전부 DB 쪽
#     SECURITY DEFINER 함수 내부에서 이루어진다. 이 클래스는 파싱/검증/예외
#     변환만 한다.
#   - 자체적으로 새 Supabase 세션/클라이언트를 만들지 않는다 — client_manager를
#     주입받아 AuthService/CloudSyncService/AdminService와 공유한다.
#   - service_role key를 쓰지 않는다 — anon key + RLS/RPC 내부 권한 검사만으로 동작한다.
#   - UI 스레드에서 직접 호출되도록 강제하지 않는다 — 호출부가 백그라운드
#     스레드(PC: MainWindow._run_in_thread, 모바일: JS 비동기)에서 불러야 한다.
#
# 레거시 services/cloud_sync_service.py(messages 테이블)는 건드리지 않는다 —
# 이 서비스는 완전히 별도의 shared_messages 테이블만 다룬다.

import logging
from dataclasses import dataclass
from typing import Optional

from services.supabase_client import SupabaseClientManager

logger = logging.getLogger(__name__)

try:
    from postgrest.exceptions import APIError as PostgrestAPIError
except ImportError:  # pragma: no cover - postgrest는 supabase-py의 종속성이라 항상 함께 설치됨
    PostgrestAPIError = None  # type: ignore[assignment, misc]

_TABLE = "shared_messages"
_HISTORY_TABLE = "shared_message_history"
_RPC_UPDATE = "update_shared_message"
_RPC_FORCE_UPDATE = "force_update_shared_message"

# Production Stabilization Sprint: SQL을 강화하며 두 RPC가 허용하는 update_source
# 집합을 분리했다 — update_shared_message(일반 저장, OCC)는 desktop/mobile만,
# force_update_shared_message(관리자 전용, 충돌 무시)는 migration/admin_force만
# 허용한다(docs/sql/shared_messages_realtime.sql 3/4절 참고). 클라이언트에서도
# 서버와 동일한 값만 보내도록 로컬에서 먼저 걸러 불필요한 RPC 왕복을 줄인다.
_ALLOWED_UPDATE_SOURCES = ("desktop", "mobile")
_ALLOWED_FORCE_UPDATE_SOURCES = ("migration", "admin_force")
MIN_MESSAGE_NO = 1
MAX_MESSAGE_NO = 12


# ===== 예외 =====

class SharedMessageError(Exception):
    """SharedMessageService 관련 오류의 최상위 클래스."""


class SharedMessagePermissionError(SharedMessageError):
    """호출자가 편집 권한(승인된 editor/admin)이 없을 때(PERMISSION_DENIED)."""


class SharedMessageValidationError(SharedMessageError):
    """message_no/update_source 등 입력값 검증 실패."""


class SharedMessageConflictError(SharedMessageError):
    """revision 불일치로 저장이 거부됨(REVISION_CONFLICT) — 자동 덮어쓰기 금지."""


class SharedMessageNotFoundError(SharedMessageError):
    """대상 message_no 행을 찾을 수 없음(MESSAGE_NOT_FOUND) — 정상 운영 중엔 발생하지
    않아야 한다(1~12행은 마이그레이션 시드로 항상 존재)."""


_ERROR_CODE_MAP: dict[str, type] = {
    "PERMISSION_DENIED": SharedMessagePermissionError,
    "INVALID_MESSAGE_NO": SharedMessageValidationError,
    "INVALID_UPDATE_SOURCE": SharedMessageValidationError,
    "REVISION_CONFLICT": SharedMessageConflictError,
    "MESSAGE_NOT_FOUND": SharedMessageNotFoundError,
}


def _translate_rpc_error(exc: Exception) -> SharedMessageError:
    """docs/sql/shared_messages_realtime.sql의 'CODE: 메시지' RAISE EXCEPTION을 파싱한다
    (services/admin_service.py의 _translate_rpc_error와 동일한 컨벤션)."""
    message = getattr(exc, "message", None) or str(exc)
    for code, exc_cls in _ERROR_CODE_MAP.items():
        prefix = f"{code}:"
        if message.startswith(prefix):
            detail = message[len(prefix):].strip()
            return exc_cls(detail or message)
    logger.error(f"SharedMessageService: 알 수 없는 RPC 오류 유형 ({type(exc).__name__})")
    return SharedMessageError("메시지 저장 중 알 수 없는 오류가 발생했습니다.")


# ===== 모델 =====

@dataclass
class SharedMessageRecord:
    """shared_messages 1행."""

    id: str
    message_no: int
    title: Optional[str]
    content: str
    revision: int
    is_active: bool
    updated_at: str
    updated_by: Optional[str]
    updated_by_name: Optional[str]
    update_source: str
    created_at: str

    @classmethod
    def from_row(cls, row: dict) -> "SharedMessageRecord":
        return cls(
            id=row["id"],
            message_no=int(row["message_no"]),
            title=row.get("title"),
            content=row.get("content", "") or "",
            revision=int(row.get("revision", 1)),
            is_active=bool(row.get("is_active", True)),
            updated_at=row.get("updated_at", "") or "",
            updated_by=row.get("updated_by"),
            updated_by_name=row.get("updated_by_name"),
            update_source=row.get("update_source", "system"),
            created_at=row.get("created_at", "") or "",
        )


@dataclass
class SharedMessageHistoryRecord:
    """shared_message_history 1행(관리자 이력 조회용)."""

    id: str
    message_no: int
    previous_content: Optional[str]
    new_content: str
    previous_revision: Optional[int]
    new_revision: int
    changed_by: Optional[str]
    changed_by_name: Optional[str]
    changed_from: str
    changed_at: str

    @classmethod
    def from_row(cls, row: dict) -> "SharedMessageHistoryRecord":
        return cls(
            id=row["id"],
            message_no=int(row["message_no"]),
            previous_content=row.get("previous_content"),
            new_content=row.get("new_content", "") or "",
            previous_revision=row.get("previous_revision"),
            new_revision=int(row.get("new_revision", 0)),
            changed_by=row.get("changed_by"),
            changed_by_name=row.get("changed_by_name"),
            changed_from=row.get("changed_from", ""),
            changed_at=row.get("changed_at", "") or "",
        )


def validate_message_no(message_no: int) -> None:
    """message_no가 1~12 범위인지 로컬에서 먼저 검증한다(불필요한 RPC 호출 예방).

    최종 방어는 항상 RPC/RLS다 — 이 함수는 UI에서 뻔히 실패할 요청을 미리
    막아 오류를 사용자 친화적으로 보여주기 위한 것일 뿐이다.
    """
    if not isinstance(message_no, int) or isinstance(message_no, bool):
        raise SharedMessageValidationError("message_no는 정수여야 합니다.")
    if not (MIN_MESSAGE_NO <= message_no <= MAX_MESSAGE_NO):
        raise SharedMessageValidationError(f"message_no는 {MIN_MESSAGE_NO}~{MAX_MESSAGE_NO} 사이여야 합니다.")


class SharedMessageService:
    """shared_messages 조회/저장 서비스. 네트워크 호출은 항상 백그라운드 스레드에서."""

    def __init__(self, client_manager: SupabaseClientManager):
        self._client_mgr = client_manager

    # ===== 조회 =====

    def list_messages(self) -> list:
        """1~12번 전체를 message_no 순으로 가져온다(완료 기준 5/15 — 수동 새로고침,
        재연결 직후 정합성 복구에 사용)."""
        client_result = self._client_mgr.get_client()
        if not client_result.success:
            raise SharedMessageError(client_result.error or "Supabase 클라이언트를 사용할 수 없습니다.")

        try:
            response = (
                client_result.client.table(_TABLE)
                .select("*")
                .order("message_no")
                .execute()
            )
        except Exception as e:
            logger.error(f"shared_messages 목록 조회 오류: {type(e).__name__}")
            raise SharedMessageError("메시지 목록을 불러오지 못했습니다.") from e

        return [SharedMessageRecord.from_row(row) for row in (response.data or [])]

    def get_message(self, message_no: int) -> Optional[SharedMessageRecord]:
        """단일 message_no 조회 — 발송 직전 검증(요구사항 9)에 사용."""
        validate_message_no(message_no)
        client_result = self._client_mgr.get_client()
        if not client_result.success:
            raise SharedMessageError(client_result.error or "Supabase 클라이언트를 사용할 수 없습니다.")

        try:
            response = (
                client_result.client.table(_TABLE)
                .select("*")
                .eq("message_no", message_no)
                .limit(1)
                .execute()
            )
        except Exception as e:
            logger.error(f"shared_messages 단건 조회 오류(message_no={message_no}): {type(e).__name__}")
            raise SharedMessageError("메시지를 불러오지 못했습니다.") from e

        rows = response.data or []
        return SharedMessageRecord.from_row(rows[0]) if rows else None

    def list_history(self, message_no: Optional[int] = None, limit: int = 50, offset: int = 0) -> list:
        """이력 조회(관리자 화면용). message_no를 지정하면 해당 번호만 필터링."""
        if message_no is not None:
            validate_message_no(message_no)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))

        client_result = self._client_mgr.get_client()
        if not client_result.success:
            raise SharedMessageError(client_result.error or "Supabase 클라이언트를 사용할 수 없습니다.")

        query = (
            client_result.client.table(_HISTORY_TABLE)
            .select("*")
            .order("changed_at", desc=True)
            .range(offset, offset + limit - 1)
        )
        if message_no is not None:
            query = query.eq("message_no", message_no)

        try:
            response = query.execute()
        except Exception as e:
            logger.error(f"shared_message_history 조회 오류: {type(e).__name__}")
            raise SharedMessageError("이력을 불러오지 못했습니다.") from e

        return [SharedMessageHistoryRecord.from_row(row) for row in (response.data or [])]

    # ===== 저장 =====

    def update_message(
        self, message_no: int, title: Optional[str], content: str,
        base_revision: int, update_source: str,
    ) -> "SharedMessageRecord":
        """일반 저장 — base_revision이 서버와 다르면 SharedMessageConflictError."""
        validate_message_no(message_no)
        if update_source not in _ALLOWED_UPDATE_SOURCES:
            raise SharedMessageValidationError(f"update_source는 {_ALLOWED_UPDATE_SOURCES} 중 하나여야 합니다.")

        client_result = self._client_mgr.get_client()
        if not client_result.success:
            raise SharedMessageError(client_result.error or "Supabase 클라이언트를 사용할 수 없습니다.")

        try:
            response = client_result.client.rpc(_RPC_UPDATE, {
                "p_message_no": message_no,
                "p_title": title,
                "p_content": content,
                "p_base_revision": base_revision,
                "p_update_source": update_source,
            }).execute()
        except Exception as e:
            if PostgrestAPIError is not None and isinstance(e, PostgrestAPIError):
                raise _translate_rpc_error(e) from e
            logger.error(f"shared_messages 저장 오류(message_no={message_no}): {type(e).__name__}")
            raise SharedMessageError("메시지 저장 중 오류가 발생했습니다.") from e

        return SharedMessageRecord.from_row(response.data)

    def force_update_message(
        self, message_no: int, title: Optional[str], content: str, update_source: str,
    ) -> "SharedMessageRecord":
        """관리자 전용 강제 덮어쓰기 — base_revision 비교 없이 항상 성공(서버가 admin 여부를
        재검증한다). 요구사항 8절 "관리자 권한일 경우에만 강제 덮어쓰기" 및 13절 초기
        마이그레이션(update_source='migration')에 사용."""
        validate_message_no(message_no)
        if update_source not in _ALLOWED_FORCE_UPDATE_SOURCES:
            raise SharedMessageValidationError(f"update_source는 {_ALLOWED_FORCE_UPDATE_SOURCES} 중 하나여야 합니다.")

        client_result = self._client_mgr.get_client()
        if not client_result.success:
            raise SharedMessageError(client_result.error or "Supabase 클라이언트를 사용할 수 없습니다.")

        try:
            response = client_result.client.rpc(_RPC_FORCE_UPDATE, {
                "p_message_no": message_no,
                "p_title": title,
                "p_content": content,
                "p_update_source": update_source,
            }).execute()
        except Exception as e:
            if PostgrestAPIError is not None and isinstance(e, PostgrestAPIError):
                raise _translate_rpc_error(e) from e
            logger.error(f"shared_messages 강제 저장 오류(message_no={message_no}): {type(e).__name__}")
            raise SharedMessageError("메시지 강제 저장 중 오류가 발생했습니다.") from e

        return SharedMessageRecord.from_row(response.data)


def is_untouched_seed(record: "SharedMessageRecord") -> bool:
    """13절 초기 마이그레이션 판단 기준 — "한 번도 실제로 수정된 적 없는" 상태인지.

    revision=1이고 update_source='system'이면 SQL 마이그레이션이 만든 시드 행
    그대로라는 뜻이다(shared_messages_realtime.sql 1절 참고).
    """
    return record.revision == 1 and record.update_source == "system"
