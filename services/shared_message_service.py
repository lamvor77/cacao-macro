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
from typing import Callable, Optional

from services.supabase_client import SupabaseClientManager
from services.supabase_error_utils import (
    ApiErrorFields, extract_api_error_fields, project_ref_from_url, short_user_id,
)

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


class SharedMessageAuthError(SharedMessageError):
    """인증/권한 관련 실패(만료된 JWT, anon 권한으로 나간 요청 등) — 네트워크
    오류나 일반 RPC 오류와 구분해서 다룬다(요구사항 6절: 발송 직전 검증 로그에서
    "인증 실패"를 "네트워크 실패"와 구분해야 함). 로그인이 안 된 상태에서 호출됐거나
    (ensure_logged_in_fn), PostgREST가 JWT 만료/권한 부족(PGRST301, 42501 등)을
    반환한 경우 이 예외로 통일한다."""


_ERROR_CODE_MAP: dict[str, type] = {
    "PERMISSION_DENIED": SharedMessagePermissionError,
    "INVALID_MESSAGE_NO": SharedMessageValidationError,
    "INVALID_UPDATE_SOURCE": SharedMessageValidationError,
    "REVISION_CONFLICT": SharedMessageConflictError,
    "MESSAGE_NOT_FOUND": SharedMessageNotFoundError,
}


_AUTH_ERROR_CODES = ("PGRST301", "PGRST302", "42501")


def _looks_like_auth_error(fields: ApiErrorFields) -> bool:
    """PostgREST/Postgres 오류가 인증/권한 문제(JWT 만료, anon 권한으로 나간
    요청 등)로 보이는지 판별한다 — 네트워크 오류(타임아웃/연결 끊김)와
    구분하기 위함이다(요구사항 6절).

    PGRST301=JWT 만료, PGRST302=JWT를 찾을 수 없음, 42501=권한 부족(우리
    RPC는 anon의 EXECUTE 권한을 명시적으로 회수해뒀으므로, 세션이 아직 공유
    Supabase Client에 반영되지 않은 상태로 호출되면 이 코드가 나기 쉽다).
    코드가 없는 경우(라이브러리가 못 채운 경우)에는 메시지 텍스트에서
    "jwt"/"permission denied"를 보조적으로 확인한다."""
    if fields.code in _AUTH_ERROR_CODES:
        return True
    message = (fields.message or "").lower()
    return "jwt" in message or "permission denied" in message


def _log_context(
    *, operation: str, rpc: Optional[str], message_no: Optional[int], revision: Optional[int],
    user_id: Optional[str], project_ref: str,
) -> str:
    """로그 문자열에 공통으로 붙이는 안전한 호출 컨텍스트.

    message_no/revision은 슬롯 번호·버전 숫자일 뿐 메시지 본문이 아니므로
    로그에 남겨도 안전하다 — title/content(실제 메시지 텍스트)는 이 함수도,
    아래 _translate_rpc_error/_log_and_classify_query_error도 어디에도 인자로 받지 않는다
    (요구사항 — 메시지 본문 전체를 로그에 남기지 않음)."""
    return (
        f"operation={operation}, rpc={rpc}, message_no={message_no}, revision={revision}, "
        f"user={short_user_id(user_id)}, project={project_ref}"
    )


def _translate_rpc_error(
    exc: Exception, *, operation: str = "unknown", rpc: Optional[str] = None,
    message_no: Optional[int] = None, revision: Optional[int] = None,
    user_id: Optional[str] = None, project_ref: str = "unknown",
) -> SharedMessageError:
    """docs/sql/shared_messages_realtime.sql의 'CODE: 메시지' RAISE EXCEPTION을 파싱한다
    (services/admin_service.py의 _translate_rpc_error와 동일한 컨벤션 —
    APIError 필드 추출 자체는 services/supabase_error_utils.py 공용 함수를 쓴다).

    지금까지는 예상 밖의 오류(권한 부족, 타임아웃, 함수/테이블 없음 등)가 나면
    "APIError"라는 예외 타입 이름만 로그에 남아 shared_messages 저장/조회 실패의
    실제 원인(code/message/details/hint)을 알 수 없었다 — 이번에 이를 전부
    남기도록 고쳤다. 'CODE: 메시지' 규약에 맞지 않는 오류 중 인증/권한 문제로
    보이는 것은 SharedMessageAuthError로(네트워크/기타 오류와 구분), 그 외는
    기존처럼 일반 SharedMessageError로 매핑한다."""
    fields = extract_api_error_fields(exc)
    context = _log_context(
        operation=operation, rpc=rpc, message_no=message_no, revision=revision,
        user_id=user_id, project_ref=project_ref,
    )

    for code, exc_cls in _ERROR_CODE_MAP.items():
        prefix = f"{code}:"
        if fields.message.startswith(prefix):
            detail = fields.message[len(prefix):].strip()
            logger.debug(
                "SharedMessageService: RPC 오류 — %s, code=%s, postgrest_message=%s, details=%s, hint=%s",
                context, fields.code, fields.message, fields.details, fields.hint,
            )
            return exc_cls(detail or fields.message)

    logger.error(
        "SharedMessageService: 알 수 없는 RPC 오류 유형 (%s) — %s, code=%s, postgrest_message=%s, "
        "details=%s, hint=%s",
        type(exc).__name__, context, fields.code, fields.message, fields.details, fields.hint,
    )
    if _looks_like_auth_error(fields):
        return SharedMessageAuthError("인증이 만료되었거나 권한이 없습니다 — 다시 로그인해 주세요.")
    return SharedMessageError("메시지 저장 중 알 수 없는 오류가 발생했습니다.")


def _log_and_classify_query_error(
    exc: Exception, *, operation: str, message_no: Optional[int],
    user_id: Optional[str], project_ref: str,
) -> type:
    """RPC(CODE: 메시지 규약)가 아닌 일반 조회(list_messages/get_message/
    list_history)에서 발생한 오류를 로그로 남기고, 호출부가 어떤 예외
    클래스로 감쌀지 반환한다(SharedMessageAuthError 또는 SharedMessageError).

    APIError가 아니면(네트워크 오류/타임아웃 등) code/details/hint 없이
    예외 타입만 남기고 항상 SharedMessageError로 분류한다 — "네트워크 실패"와
    "인증 실패"를 구분하는 것이 이 함수의 핵심 목적이다(요구사항 6절)."""
    context = _log_context(
        operation=operation, rpc=None, message_no=message_no, revision=None,
        user_id=user_id, project_ref=project_ref,
    )
    if PostgrestAPIError is not None and isinstance(exc, PostgrestAPIError):
        fields = extract_api_error_fields(exc)
        logger.error(
            "SharedMessageService: 조회 오류 — %s, code=%s, postgrest_message=%s, details=%s, hint=%s",
            context, fields.code, fields.message, fields.details, fields.hint,
        )
        return SharedMessageAuthError if _looks_like_auth_error(fields) else SharedMessageError

    logger.error("SharedMessageService: 조회 오류 — %s, 예외 유형=%s", context, type(exc).__name__)
    return SharedMessageError


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

    def __init__(
        self,
        client_manager: SupabaseClientManager,
        user_id_fn: Optional[Callable[[], Optional[str]]] = None,
        ensure_logged_in_fn: Optional[Callable[[], bool]] = None,
    ):
        """
        Args:
            user_id_fn: 로그용으로만 쓰는, 현재 로그인 사용자 id를 반환하는
                콜백(없으면 None) — 오류 로그에 "누구의 요청이었는지" 남기기
                위함이다(요구사항: 사용자 ID 앞 8자). AuthService.load_session()처럼
                로컬 파일만 읽는(네트워크 없는) 함수를 넘겨야 한다 — 여기서
                네트워크 호출이나 세션 갱신을 유발하면 안 된다(services/
                auth_service.py의 세션 갱신 경쟁 조사에서 확인된 원칙과 동일).
            ensure_logged_in_fn: 요청을 보내기 전에 "지금 로그인 상태인가"를
                확인하는 콜백(없으면 None — 검사하지 않음). 보통
                AuthService.is_logged_in을 넘긴다 — 세션이 만료돼 있으면 내부적으로
                갱신을 시도한다(락으로 직렬화됨, services/auth_service.py 참고).
                이 콜백이 False를 반환하면 네트워크 요청 자체를 시도하지 않고
                즉시 SharedMessageAuthError를 던진다 — 공유 Supabase Client에
                아직 로그인 세션이 반영되지 않은 상태(앱 시작 직후 등)에서
                shared_messages 요청이 anon 권한으로 나가 원인 불명의 APIError가
                되는 경로를 사전에 차단하기 위한 최소한의 방어선이다. 이 콜백이
                True를 반환해도 그 사이 세션이 만료될 수 있으므로, 이것만으로
                인증 실패가 100% 사라지는 것은 아니다 — 그래도 실패하면
                _translate_rpc_error/_log_and_classify_query_error가
                SharedMessageAuthError로 분류한다.
        """
        self._client_mgr = client_manager
        self._user_id_fn = user_id_fn
        self._ensure_logged_in_fn = ensure_logged_in_fn

    def _current_user_id(self) -> Optional[str]:
        """로그 전용 — 실패해도(콜백 없음/예외) 오류 로그 자체를 막지 않는다."""
        if self._user_id_fn is None:
            return None
        try:
            return self._user_id_fn()
        except Exception:
            return None

    def _project_ref(self) -> str:
        """로그 전용 — config 접근 자체가 실패해도(테스트 fake 등) 오류 로그를 막지 않는다."""
        try:
            return project_ref_from_url(self._client_mgr.config.url)
        except AttributeError:
            return "unknown"

    def _require_logged_in(self, *, operation: str) -> None:
        """ensure_logged_in_fn이 주어졌고 False를 반환하면, 네트워크 요청 없이
        즉시 인증 오류로 실패시킨다(위 __init__ 문서 참고)."""
        if self._ensure_logged_in_fn is None:
            return
        try:
            logged_in = self._ensure_logged_in_fn()
        except Exception:
            logged_in = False
        if not logged_in:
            context = _log_context(
                operation=operation, rpc=None, message_no=None, revision=None,
                user_id=self._current_user_id(), project_ref=self._project_ref(),
            )
            logger.warning("SharedMessageService: 로그인 필요 — %s, 요청을 보내지 않음", context)
            raise SharedMessageAuthError("로그인이 필요합니다 — 다시 로그인해 주세요.")

    # ===== 조회 =====

    def list_messages(self) -> list:
        """1~12번 전체를 message_no 순으로 가져온다(완료 기준 5/15 — 수동 새로고침,
        재연결 직후 정합성 복구에 사용)."""
        self._require_logged_in(operation="list_messages")
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
            exc_cls = _log_and_classify_query_error(
                e, operation="list_messages", message_no=None,
                user_id=self._current_user_id(), project_ref=self._project_ref(),
            )
            raise exc_cls("메시지 목록을 불러오지 못했습니다.") from e

        return [SharedMessageRecord.from_row(row) for row in (response.data or [])]

    def get_message(self, message_no: int) -> Optional[SharedMessageRecord]:
        """단일 message_no 조회 — 발송 직전 검증(요구사항 9)에 사용."""
        validate_message_no(message_no)
        self._require_logged_in(operation="get_message")
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
            exc_cls = _log_and_classify_query_error(
                e, operation="get_message", message_no=message_no,
                user_id=self._current_user_id(), project_ref=self._project_ref(),
            )
            raise exc_cls("메시지를 불러오지 못했습니다.") from e

        rows = response.data or []
        return SharedMessageRecord.from_row(rows[0]) if rows else None

    def list_history(self, message_no: Optional[int] = None, limit: int = 50, offset: int = 0) -> list:
        """이력 조회(관리자 화면용). message_no를 지정하면 해당 번호만 필터링."""
        if message_no is not None:
            validate_message_no(message_no)
        self._require_logged_in(operation="list_history")
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
            exc_cls = _log_and_classify_query_error(
                e, operation="list_history", message_no=message_no,
                user_id=self._current_user_id(), project_ref=self._project_ref(),
            )
            raise exc_cls("이력을 불러오지 못했습니다.") from e

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
        self._require_logged_in(operation="update_message")

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
            user_id = self._current_user_id()
            project_ref = self._project_ref()
            if PostgrestAPIError is not None and isinstance(e, PostgrestAPIError):
                raise _translate_rpc_error(
                    e, operation="update_message", rpc=_RPC_UPDATE, message_no=message_no,
                    revision=base_revision, user_id=user_id, project_ref=project_ref,
                ) from e
            exc_cls = _log_and_classify_query_error(
                e, operation="update_message", message_no=message_no,
                user_id=user_id, project_ref=project_ref,
            )
            raise exc_cls("메시지 저장 중 오류가 발생했습니다.") from e

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
        self._require_logged_in(operation="force_update_message")

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
            user_id = self._current_user_id()
            project_ref = self._project_ref()
            if PostgrestAPIError is not None and isinstance(e, PostgrestAPIError):
                raise _translate_rpc_error(
                    e, operation="force_update_message", rpc=_RPC_FORCE_UPDATE, message_no=message_no,
                    user_id=user_id, project_ref=project_ref,
                ) from e
            exc_cls = _log_and_classify_query_error(
                e, operation="force_update_message", message_no=message_no,
                user_id=user_id, project_ref=project_ref,
            )
            raise exc_cls("메시지 강제 저장 중 오류가 발생했습니다.") from e

        return SharedMessageRecord.from_row(response.data)


def is_untouched_seed(record: "SharedMessageRecord") -> bool:
    """13절 초기 마이그레이션 판단 기준 — "한 번도 실제로 수정된 적 없는" 상태인지.

    revision=1이고 update_source='system'이면 SQL 마이그레이션이 만든 시드 행
    그대로라는 뜻이다(shared_messages_realtime.sql 1절 참고).
    """
    return record.revision == 1 and record.update_source == "system"
