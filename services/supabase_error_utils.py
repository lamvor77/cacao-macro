# Supabase APIError에서 안전하게 로그로 남길 수 있는 진단 정보를 뽑아내는 공용 유틸.
#
# services/admin_service.py와 services/shared_message_service.py가 각자
# "RPC 오류가 나면 code/message/details/hint가 없어 원인을 알 수 없다"는
# 동일한 문제를 겪었다(둘 다 postgrest.exceptions.APIError를 씀) — 이 파일은
# 그중 "APIError에서 값을 어떻게 뽑는지"와 "user_id/project ref를 어떻게
# 안전하게 마스킹하는지"만 공용으로 뺀다. "어떤 code를 어떤 도메인 예외로
# 바꿀지"는 모듈마다 의미가 달라 각자 유지한다.

import re
from dataclasses import dataclass
from typing import Optional

_PROJECT_REF_RE = re.compile(r"^https?://([a-z0-9]+)\.supabase\.co")


@dataclass
class ApiErrorFields:
    """postgrest.exceptions.APIError 하나에서 뽑아낸 안전한 진단 필드.

    이 네 값 모두 PostgREST가 오류 응답에 담아 보내는 것들이라 토큰/anon
    key를 포함하지 않는다 — 다만 message/details/hint에는 서버가 돌려준
    텍스트가 그대로 들어있을 수 있으므로(예: 사용자 입력을 반사하는 경우),
    로그로 남길 때 별도로 사용자 원문 콘텐츠를 덧붙이지 않도록 호출부가
    주의해야 한다.
    """

    code: Optional[str]
    message: str
    details: Optional[str]
    hint: Optional[str]


def extract_api_error_fields(exc: Exception) -> ApiErrorFields:
    """APIError(또는 동일한 속성을 가진 예외)에서 code/message/details/hint를 뽑는다."""
    return ApiErrorFields(
        code=getattr(exc, "code", None),
        message=getattr(exc, "message", None) or str(exc),
        details=getattr(exc, "details", None),
        hint=getattr(exc, "hint", None),
    )


def short_user_id(user_id: Optional[str]) -> str:
    """로그에 사용자를 식별할 정도로만 남긴다 — UUID 전체가 아니라 앞 8자."""
    if not user_id:
        return "unknown"
    return user_id[:8]


def project_ref_from_url(url: Optional[str]) -> str:
    """SUPABASE_URL에서 프로젝트 ref만 뽑는다 — URL 전체나 키는 로그에 남기지 않는다."""
    if not url:
        return "unknown"
    match = _PROJECT_REF_RE.match(url)
    return match.group(1) if match else "unknown"
