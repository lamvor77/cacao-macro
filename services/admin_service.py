# 운영 관리자(app_users.role='admin') 전용 사용자 관리 RPC의 Python 래퍼 (Phase 4-1)
#
# 이 모듈은 UI와 docs/sql/phase4_admin_rpc.sql의 admin_* RPC 사이의 얇은 계층이다.
# 실제 권한 검사(fn_is_admin())와 마지막 admin 보호, 감사로그 기록은 전부 DB 쪽
# SECURITY DEFINER 함수 내부에서 이루어진다 — 이 클래스는 그 결과를 파싱하고,
# 호출 전 입력값을 검증하고, 오류를 사용자 친화적인 예외로 변환하는 역할만 한다.
#
# 이 파일이 절대 하지 않는 일:
#   - 자체적으로 새 Supabase 세션/클라이언트를 만들지 않는다 — AuthService/
#     CloudSyncService와 동일하게 client_manager를 주입받아 재사용해야
#     RLS/RPC 내부의 auth.uid()가 올바른 로그인 사용자를 가리킨다
#     (services/cloud_sync_coordinator.py가 CloudSyncService를 만들 때
#     self._auth.client_manager를 공유시키는 것과 동일한 이유).
#   - service_role key를 사용하지 않는다 — anon key + RLS/RPC 내부 권한 검사만으로 동작한다.
#   - UI 스레드에서 직접 호출되도록 강제하지 않는다 — 네트워크 요청(RPC)이므로
#     호출부가 MainWindow._run_in_thread류 패턴으로 백그라운드 스레드에서 불러야 한다.
#   - 관리자 UI를 만들지 않는다 — 이 모듈은 서비스 계층까지만 구현한다(Phase 4-1 범위).

import logging
import re
from dataclasses import dataclass
from typing import Optional

from services.supabase_client import SupabaseClientManager

logger = logging.getLogger(__name__)

try:
    from postgrest.exceptions import APIError as PostgrestAPIError
except ImportError:  # pragma: no cover - postgrest는 supabase-py의 종속성이라 항상 함께 설치됨
    PostgrestAPIError = None  # type: ignore[assignment, misc]

_ALLOWED_ROLES = ("viewer", "editor", "admin")
_ALLOWED_STATUSES = ("pending", "approved", "blocked")
_MIN_LIMIT = 1
_MAX_LIMIT = 200
_MAX_REASON_LENGTH = 500

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# ===== 예외 =====

class AdminServiceError(Exception):
    """AdminService 관련 오류의 최상위 클래스.

    DB 내부 상세정보(SQL 문장, 스택 등)는 로그에만 남기고, 이 예외의 메시지에는
    사용자에게 보여줘도 안전한 문구만 담는다.
    """


class AdminPermissionError(AdminServiceError):
    """호출자가 승인된 관리자가 아닐 때(ADMIN_REQUIRED)."""


class AdminValidationError(AdminServiceError):
    """입력값 검증 실패 — 잘못된 role/status/UUID/limit/offset 등.

    RPC를 호출하기 "전에" 로컬에서 걸러내는 경우와, RPC가 INVALID_ROLE/
    INVALID_STATUS로 거부한 경우 둘 다 이 예외로 매핑된다.
    """


class AdminConflictError(AdminServiceError):
    """상태 충돌 — 자기 자신 차단/강등 금지, 마지막 admin 보호, 대상 없음,
    이미 그 상태(차단 안 된 사용자 차단해제 등) 같은 비즈니스 규칙 위반."""


_ERROR_CODE_MAP: dict[str, type] = {
    "ADMIN_REQUIRED": AdminPermissionError,
    "TARGET_USER_NOT_FOUND": AdminConflictError,
    "INVALID_ROLE": AdminValidationError,
    "INVALID_STATUS": AdminValidationError,
    "SELF_BLOCK_FORBIDDEN": AdminConflictError,
    "SELF_DEMOTION_FORBIDDEN": AdminConflictError,
    "LAST_ADMIN_PROTECTED": AdminConflictError,
    "TARGET_BLOCKED": AdminConflictError,
    "USER_NOT_BLOCKED": AdminConflictError,
}


def _translate_rpc_error(exc: Exception) -> AdminServiceError:
    """docs/sql/phase4_admin_rpc.sql의 'CODE: 메시지' 형식 RAISE EXCEPTION을 파싱한다.

    ERRCODE(SQLSTATE)가 아니라 메시지 앞부분의 코드 문자열로 구분하는 이유는
    RAISE EXCEPTION의 메시지 규약 설계(15절)와 동일 — SQL 쪽에서 별도
    USING ERRCODE 등록 없이도 안정적으로 매핑할 수 있다.
    """
    message = getattr(exc, "message", None) or str(exc)
    for code, exc_cls in _ERROR_CODE_MAP.items():
        prefix = f"{code}:"
        if message.startswith(prefix):
            detail = message[len(prefix):].strip()
            return exc_cls(detail or message)
    logger.error(f"AdminService: 알 수 없는 RPC 오류 유형 ({type(exc).__name__})")
    return AdminServiceError("관리자 작업 중 알 수 없는 오류가 발생했습니다.")


# ===== 모델 =====

@dataclass
class AdminUserRecord:
    """admin_list_users/admin_approve_user 등이 반환하는 app_users 행(허용된 컬럼만)."""

    id: str
    email: str
    display_name: Optional[str]
    role: str
    status: str
    approved_at: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]
    updated_by: Optional[str]

    @classmethod
    def from_row(cls, row: dict) -> "AdminUserRecord":
        return cls(
            id=row["id"],
            email=row.get("email", ""),
            display_name=row.get("display_name"),
            role=row["role"],
            status=row["status"],
            approved_at=row.get("approved_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            updated_by=row.get("updated_by"),
        )


@dataclass
class AdminAuditLogRecord:
    """admin_list_audit_logs가 반환하는 감사로그 1건."""

    id: str
    actor_user_id: str
    actor_email: Optional[str]
    target_user_id: Optional[str]
    target_email: Optional[str]
    action: str
    old_role: Optional[str]
    new_role: Optional[str]
    old_status: Optional[str]
    new_status: Optional[str]
    reason: Optional[str]
    metadata: dict
    created_at: str

    @classmethod
    def from_row(cls, row: dict) -> "AdminAuditLogRecord":
        return cls(
            id=row["id"],
            actor_user_id=row["actor_user_id"],
            actor_email=row.get("actor_email"),
            target_user_id=row.get("target_user_id"),
            target_email=row.get("target_email"),
            action=row["action"],
            old_role=row.get("old_role"),
            new_role=row.get("new_role"),
            old_status=row.get("old_status"),
            new_status=row.get("new_status"),
            reason=row.get("reason"),
            metadata=row.get("metadata") or {},
            created_at=row["created_at"],
        )


# ===== 입력 검증 헬퍼 =====

def _validate_uuid(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.match(value.strip()):
        raise AdminValidationError(f"{field_name}가 올바른 UUID 형식이 아닙니다.")
    return value.strip()


def _validate_role(value: Optional[str], field_name: str, allow_none: bool = False) -> Optional[str]:
    if value is None:
        if allow_none:
            return None
        raise AdminValidationError(f"{field_name}이(가) 필요합니다.")
    if value not in _ALLOWED_ROLES:
        raise AdminValidationError(f"{field_name} 값이 올바르지 않습니다: {value}")
    return value


def _validate_status(value: Optional[str], field_name: str, allow_none: bool = False) -> Optional[str]:
    if value is None:
        if allow_none:
            return None
        raise AdminValidationError(f"{field_name}이(가) 필요합니다.")
    if value not in _ALLOWED_STATUSES:
        raise AdminValidationError(f"{field_name} 값이 올바르지 않습니다: {value}")
    return value


def _validate_pagination(limit: int, offset: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < _MIN_LIMIT or limit > _MAX_LIMIT:
        raise AdminValidationError(f"limit은 {_MIN_LIMIT}~{_MAX_LIMIT} 사이의 정수여야 합니다.")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise AdminValidationError("offset은 0 이상의 정수여야 합니다.")


def _clean_reason(reason: Optional[str]) -> Optional[str]:
    if reason is None:
        return None
    cleaned = reason.strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_REASON_LENGTH]


# ===== 서비스 =====

class AdminService:
    """docs/sql/phase4_admin_rpc.sql의 admin_* RPC를 호출하는 얇은 래퍼."""

    def __init__(self, client_manager: Optional[SupabaseClientManager] = None):
        # AuthService.client_manager를 공유해서 넘겨야 로그인 세션이 이 서비스의
        # RPC 호출에도 반영된다 — 생략 시 새 클라이언트를 만들지만, 그 경우
        # 세션이 없는 client가 되어 모든 RPC가 ADMIN_REQUIRED로 거부된다(안전한
        # 기본값 — 클라우드 동기화가 비활성화됐거나 아직 로그인 안 된 경우와 동일).
        self._client_mgr = client_manager or SupabaseClientManager()

    @property
    def client_manager(self) -> SupabaseClientManager:
        return self._client_mgr

    # ----- 조회 -----

    def list_users(
        self,
        status: Optional[str] = None,
        role: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AdminUserRecord]:
        status = _validate_status(status, "status", allow_none=True)
        role = _validate_role(role, "role", allow_none=True)
        _validate_pagination(limit, offset)

        rows = self._call_rpc("admin_list_users", {
            "p_status": status,
            "p_role": role,
            "p_search": search,
            "p_limit": limit,
            "p_offset": offset,
        })
        return [AdminUserRecord.from_row(r) for r in rows]

    def list_audit_logs(
        self,
        target_user_id: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AdminAuditLogRecord]:
        if target_user_id is not None:
            target_user_id = _validate_uuid(target_user_id, "target_user_id")
        _validate_pagination(limit, offset)

        rows = self._call_rpc("admin_list_audit_logs", {
            "p_target_user_id": target_user_id,
            "p_action": action,
            "p_limit": limit,
            "p_offset": offset,
        })
        return [AdminAuditLogRecord.from_row(r) for r in rows]

    # ----- 변경 -----

    def approve_user(
        self, target_user_id: str, role: str = "viewer", reason: Optional[str] = None
    ) -> AdminUserRecord:
        target_user_id = _validate_uuid(target_user_id, "target_user_id")
        role = _validate_role(role, "role")
        reason = _clean_reason(reason)

        rows = self._call_rpc("admin_approve_user", {
            "p_target_user_id": target_user_id,
            "p_role": role,
            "p_reason": reason,
        })
        return self._single(rows)

    def block_user(self, target_user_id: str, reason: Optional[str] = None) -> AdminUserRecord:
        target_user_id = _validate_uuid(target_user_id, "target_user_id")
        reason = _clean_reason(reason)

        rows = self._call_rpc("admin_block_user", {
            "p_target_user_id": target_user_id,
            "p_reason": reason,
        })
        return self._single(rows)

    def unblock_user(
        self, target_user_id: str, restore_status: str = "approved", reason: Optional[str] = None
    ) -> AdminUserRecord:
        target_user_id = _validate_uuid(target_user_id, "target_user_id")
        restore_status = _validate_status(restore_status, "restore_status")
        if restore_status not in ("approved", "pending"):
            raise AdminValidationError("restore_status는 approved 또는 pending만 허용됩니다.")
        reason = _clean_reason(reason)

        rows = self._call_rpc("admin_unblock_user", {
            "p_target_user_id": target_user_id,
            "p_restore_status": restore_status,
            "p_reason": reason,
        })
        return self._single(rows)

    def update_user_role(
        self, target_user_id: str, new_role: str, reason: Optional[str] = None
    ) -> AdminUserRecord:
        target_user_id = _validate_uuid(target_user_id, "target_user_id")
        new_role = _validate_role(new_role, "new_role")
        reason = _clean_reason(reason)

        rows = self._call_rpc("admin_update_user_role", {
            "p_target_user_id": target_user_id,
            "p_new_role": new_role,
            "p_reason": reason,
        })
        return self._single(rows)

    # ----- 내부 -----

    def _call_rpc(self, fn_name: str, params: dict) -> list:
        client_result = self._client_mgr.get_client()
        if not client_result.success:
            raise AdminServiceError(client_result.error or "Supabase 클라이언트를 사용할 수 없습니다.")

        try:
            response = client_result.client.rpc(fn_name, params).execute()
        except Exception as e:
            if PostgrestAPIError is not None and isinstance(e, PostgrestAPIError):
                raise _translate_rpc_error(e) from None
            logger.error(f"AdminService RPC 호출 실패 — {fn_name} ({type(e).__name__})")
            raise AdminServiceError("관리자 작업 요청에 실패했습니다.") from None

        return response.data or []

    def _single(self, rows: list) -> AdminUserRecord:
        if not rows:
            raise AdminServiceError("서버 응답이 비어 있습니다.")
        return AdminUserRecord.from_row(rows[0])
