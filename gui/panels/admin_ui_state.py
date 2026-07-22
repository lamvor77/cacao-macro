# 운영 관리자 UI(operations_admin_panel.py)의 판정/변환 로직을 Tkinter와
# 완전히 분리해 순수 함수/클래스로 둔다 (Phase 4-2).
#
# 이렇게 분리한 이유: 이 프로젝트는 지금까지 실제 Tkinter 위젯을 자동화
# 테스트한 적이 없다(디스플레이 의존 테스트의 불안정성 때문 — 기존 GUI
# 코드 전부 동일한 이유로 미테스트). 권한 노출/버튼 활성화/payload 생성/
# 오류 메시지 매핑/비동기 응답 순서 처리 같은 "판단 로직"만큼은 위젯 없이도
# 정확성을 보장해야 하므로, 이 모듈에 전부 모아 순수 로직으로 테스트한다.
# operations_admin_panel.py는 이 모듈의 함수/클래스를 호출하기만 한다.

from dataclasses import dataclass
from typing import Optional

from services.admin_service import (
    AdminConflictError,
    AdminPermissionError,
    AdminServiceError,
    AdminValidationError,
)
from services.auth_service import AppUserProfile

# 승인 다이얼로그에서 고를 수 있는 역할 — admin은 여기서 제공하지 않는다
# (admin 승격은 반드시 역할 변경 기능에서 별도로, 명시적 확인과 함께 처리한다).
APPROVE_DIALOG_ROLES: tuple = ("viewer", "editor")

# 역할 변경 다이얼로그에서 고를 수 있는 역할 — admin도 포함한다.
ROLE_CHANGE_DIALOG_ROLES: tuple = ("viewer", "editor", "admin")

STATUS_FILTER_OPTIONS: tuple = ("전체", "pending", "approved", "blocked")
ROLE_FILTER_OPTIONS: tuple = ("전체", "viewer", "editor", "admin")
RESTORE_STATUS_OPTIONS: tuple = ("approved", "pending")

DEFAULT_PAGE_SIZE = 100

ACTION_LABELS_KO: dict = {
    "user_approved": "사용자 승인",
    "user_blocked": "사용자 차단",
    "user_unblocked": "차단 해제",
    "user_role_changed": "역할 변경",
    "user_profile_updated": "사용자 정보 변경",
}


def action_label_ko(action: str) -> str:
    return ACTION_LABELS_KO.get(action, action)


# ============================================================
# 1~3: 운영 관리자 메뉴 노출 판정
# ============================================================

def is_operations_admin_menu_visible(profile: Optional[AppUserProfile]) -> bool:
    """운영 관리자 메뉴/버튼을 표시할지 판정한다.

    UI 노출은 편의 기능일 뿐 보안 경계가 아니다 — 실제 권한은 매 admin_* RPC
    호출마다 fn_is_admin()이 다시 확인한다(services/admin_service.py 경유).
    profile이 None(로그아웃 상태 포함)이면 항상 숨긴다.
    """
    return bool(profile is not None and profile.is_admin)


# ============================================================
# 4~9: 사용자별 작업 버튼 활성화 판정
# ============================================================

@dataclass
class AdminActionState:
    can_approve: bool
    can_block: bool
    can_unblock: bool
    can_change_role: bool
    is_self: bool


_NO_SELECTION_STATE = AdminActionState(
    can_approve=False, can_block=False, can_unblock=False, can_change_role=False, is_self=False,
)


def get_admin_action_state(current_user_id: Optional[str], selected_user) -> AdminActionState:
    """selected_user는 services.admin_service.AdminUserRecord(또는 .id/.status를
    가진 동등 객체) — None이면 아무 것도 선택되지 않은 상태(전부 비활성)."""
    if selected_user is None:
        return _NO_SELECTION_STATE

    is_self = bool(current_user_id) and selected_user.id == current_user_id

    return AdminActionState(
        can_approve=(selected_user.status == "pending"),
        can_block=(selected_user.status != "blocked" and not is_self),
        can_unblock=(selected_user.status == "blocked"),
        can_change_role=True,  # 역할 변경 자체는 항상 시도 가능 — 세부 제약은 아래 함수가 판정
        is_self=is_self,
    )


def can_change_role_to(current_user_id: Optional[str], selected_user, new_role: str) -> bool:
    """역할 변경 다이얼로그의 "확인" 버튼을 활성화해도 되는지(사전 차단용).

    최종 방어는 항상 DB RPC(SELF_DEMOTION_FORBIDDEN/LAST_ADMIN_PROTECTED)다 —
    이 함수는 UI에서 뻔히 실패할 시도를 미리 막아 불필요한 오류 팝업을 줄이는
    용도일 뿐, 이 함수만 신뢰하고 서버 측 검증을 생략하지 않는다.
    """
    if selected_user is None:
        return False
    if new_role == selected_user.role:
        return False  # no-op — 같은 값이면 막는다
    is_self = bool(current_user_id) and selected_user.id == current_user_id
    if is_self and new_role != "admin":
        return False  # 자기 자신 강등 사전 차단
    return True


# ============================================================
# 12~15, 20: 조회 payload 생성(검색/필터/페이지네이션)
# ============================================================

def build_list_users_params(
    search_text: str,
    status_filter: str,
    role_filter: str,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict:
    """검색창/필터 콤보박스의 원시 입력값을 AdminService.list_users() kwargs로 변환한다.

    - 검색어는 trim하고, 빈 문자열이면 None으로 보낸다("전체 선택은 None을 전달").
    - "전체"는 None으로, 그 외 값은 그대로 전달한다.
    """
    search = search_text.strip() or None
    status = None if status_filter in (None, "전체") else status_filter
    role = None if role_filter in (None, "전체") else role_filter
    return {"search": search, "status": status, "role": role, "limit": limit, "offset": offset}


def build_audit_log_params(
    target_user_id: Optional[str],
    action_filter: str,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict:
    action = None if action_filter in (None, "전체") else action_filter
    return {
        "target_user_id": (target_user_id or None),
        "action": action,
        "limit": limit,
        "offset": offset,
    }


# ============================================================
# 16~19: 변경 payload 생성(승인/차단/차단해제/역할변경)
# ============================================================

def build_approve_payload(target_user_id: str, role: str, reason: str) -> dict:
    return {"target_user_id": target_user_id, "role": role, "reason": (reason.strip() or None)}


def build_block_payload(target_user_id: str, reason: str) -> dict:
    return {"target_user_id": target_user_id, "reason": (reason.strip() or None)}


def build_unblock_payload(target_user_id: str, restore_status: str, reason: str) -> dict:
    return {
        "target_user_id": target_user_id,
        "restore_status": restore_status,
        "reason": (reason.strip() or None),
    }


def build_role_change_payload(target_user_id: str, new_role: str, reason: str) -> dict:
    return {"target_user_id": target_user_id, "new_role": new_role, "reason": (reason.strip() or None)}


# ============================================================
# 21~22: 오류 메시지 한국어 매핑
# ============================================================

_GENERIC_PERMISSION_MSG = "운영 관리자 권한이 없거나 권한이 변경되었습니다. 다시 로그인해 주세요."
_GENERIC_SERVICE_MSG = "관리자 작업을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."


def describe_admin_error(exc: Exception) -> str:
    """AdminService 예외를 화면에 표시할 한국어 문구로 변환한다.

    원본 API 오류/SQL/토큰/세션정보는 이 함수를 거치면 전부 사라진다 —
    반환값에는 항상 사용자에게 보여줘도 안전한 문구만 담긴다(13절 정책).
    """
    if isinstance(exc, AdminPermissionError):
        return _GENERIC_PERMISSION_MSG
    if isinstance(exc, AdminValidationError):
        return f"입력값을 확인해 주세요: {exc}"
    if isinstance(exc, AdminConflictError):
        return str(exc)  # AdminService가 이미 RPC의 'CODE: 한국어 메시지'에서 메시지 부분만 담아둠
    if isinstance(exc, AdminServiceError):
        return _GENERIC_SERVICE_MSG
    return _GENERIC_SERVICE_MSG


# ============================================================
# 23~25: 비동기 요청 순서/중복 실행 관리
# ============================================================

class AdminRequestSequencer:
    """오래된 비동기 응답이 최신 응답을 덮어쓰지 않도록 하는 순수 로직(Tk 비의존).

    사용법(operations_admin_panel.py 기준):
        gen = self._seq.start()                  # 버튼 클릭 시 — 새 세대 번호 발급
        self._run_in_thread(lambda: self._do_search(gen))
        ...
        def _do_search(self, gen):
            result = self._admin.list_users(...)  # 백그라운드 스레드, 네트워크 호출
            self.after(0, lambda: self._apply_search_result(gen, result))

        def _apply_search_result(self, gen, result):
            if not self._seq.accept(gen):
                return  # 이미 더 최신 요청이 시작됐거나 창이 닫힘 — 이 응답은 버린다
            try:
                ... 실제 UI 갱신 ...
            finally:
                self._seq.finish(gen)

    "동일 작업 중복 실행 방지"(원칙 7)는 이 클래스가 강제하지 않는다 — 호출부가
    버튼을 만들 때 `if self._seq.in_flight: return`으로 먼저 걸러야 한다(검색은
    최신 응답 우선이라 진행 중에도 새로 시작할 수 있어야 하는 반면, 단순 새로고침
    연타는 막아야 하는 등 정책이 액션마다 다를 수 있어 정책 자체는 호출부에 맡긴다).
    """

    def __init__(self):
        self._generation = 0
        self._in_flight_count = 0
        self._disposed = False

    def start(self) -> int:
        """새 요청을 시작한다. 진행 중인 요청이 있어도 항상 새 세대 번호를 발급한다
        — "중복 실행 방지"가 필요하면 호출 전에 in_flight를 직접 확인해야 한다."""
        self._generation += 1
        self._in_flight_count += 1
        return self._generation

    def accept(self, generation: int) -> bool:
        """이 세대의 응답을 UI에 반영해도 되는지. 창이 폐기됐거나(dispose 호출됨)
        더 최신 요청이 이미 시작됐으면 False — 오래된 응답이 최신 결과를 덮어쓰는
        것을 막는다."""
        return (not self._disposed) and generation == self._generation

    def finish(self, generation: int) -> None:
        """요청이 끝났음을 표시한다(성공/실패/버려짐 무관하게 항상 호출해야 in_flight
        카운트가 정확하다)."""
        if self._in_flight_count > 0:
            self._in_flight_count -= 1

    @property
    def in_flight(self) -> bool:
        return self._in_flight_count > 0 and not self._disposed

    def dispose(self) -> None:
        """창이 닫힐 때 호출한다 — 이후 어떤 응답도 accept()가 False를 반환하므로,
        닫힌 창의 콜백이 위젯을 건드리는 것을 막는다."""
        self._disposed = True
