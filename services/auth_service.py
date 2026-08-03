# Supabase Auth 세션 관리 — Google OAuth(PKCE) 로그인 포함 (Phase 2D)
#
# 세션은 절대 평문 JSON으로 저장하지 않는다. Windows DPAPI(win32crypt —
# pywin32는 requirements.txt에 이미 있는 기존 의존성이라 새로 추가하지 않음)로
# 암호화해 현재 Windows 로그인 계정에만 복호화 가능한 형태로 저장한다.
#
# 로그인 방식: Supabase 공식 PKCE OAuth 흐름 (implicit flow 아님 — access
# token을 URL fragment로 장시간 다루지 않는다). 이 파일이 실제로 호출하는
# supabase-py 메서드(sign_in_with_oauth/exchange_code_for_session/set_session/
# refresh_session/get_user/sign_out)는 추측이 아니라 설치된 supabase==2.31.0
# (supabase_auth 패키지)의 SyncGoTrueClient 소스를 직접 읽고 확인한 것이다.
#
# 핵심 흐름 (login_with_google):
#   1) 사용 가능한 localhost 포트로 콜백 서버 준비 (OAuthCallbackServer)
#   2) client.auth.sign_in_with_oauth(...) 호출 — Supabase가 PKCE
#      code_verifier/code_challenge를 내부적으로 생성해 클라이언트 자체
#      저장소(같은 Client 인스턴스 안에서만 유효)에 보관하고, 인가 URL을 반환한다.
#   3) 시스템 기본 브라우저로 그 URL을 연다.
#   4) 콜백 서버가 code(또는 error)를 받을 때까지 블로킹 대기(타임아웃 있음).
#   5) client.auth.exchange_code_for_session(...) 호출 — code_verifier를
#      명시적으로 넘기지 않으면 2)번에서 저장해둔 값을 자동으로 사용하므로,
#      반드시 2)번과 "같은" Client 인스턴스로 호출해야 한다
#      (SupabaseClientManager가 클라이언트를 캐시하므로 자동으로 보장된다).
#   6) 성공하면 세션을 DPAPI로 저장하고 클라이언트에도 적용한다.
#
# login_with_google()은 브라우저 응답을 기다리는 동안 블로킹된다 — 호출부가
# 반드시 별도 스레드에서 실행해야 한다(메인 UI 스레드를 막지 않기 위해).

import hashlib
import inspect
import json
import logging
import os
import sys
import threading
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from config.cloud_settings import CloudConfig, get_cloud_config
from services.oauth_callback_server import OAuthCallbackServer
from services.supabase_client import SupabaseClientManager

logger = logging.getLogger(__name__)

try:
    import win32crypt

    _DPAPI_AVAILABLE = True
except ImportError:
    win32crypt = None  # type: ignore[assignment]
    _DPAPI_AVAILABLE = False

_DPAPI_DESCRIPTION = "cacao_macro Supabase session"
_APP_USERS_TABLE = "app_users"

# 세션 파일 포맷 버전. AuthSession의 필드 구성이나 저장 방식이 바뀌면 이 값을
# 올린다. 저장된 파일의 버전이 이 값과 다르면(버전 표시가 아예 없는 구버전
# 파일 포함) 무조건 폐기하고 재로그인을 요구한다 — 이전 버전에서 만들어진
# 세션 파일을 새 버전 코드가 그대로 파싱해서 쓰다가 필드 누락/의미 변경으로
# 오동작하는 것을 원천 차단하기 위함이다.
_SESSION_SCHEMA_VERSION = 1


@dataclass
class AuthSession:
    """Supabase Auth 세션 (로그인된 계정 1건)."""

    user_id: str
    email: str
    access_token: str
    refresh_token: str
    expires_at: str  # ISO8601 UTC

    def is_expired(self, skew_seconds: int = 60) -> bool:
        """만료 여부. 파싱 실패 시 안전하게 '만료됨'으로 취급한다."""
        try:
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        now = datetime.now(timezone.utc)
        return (expires - now).total_seconds() <= skew_seconds


@dataclass
class AuthResult:
    """로그인/refresh/logout 공통 결과 객체 — 예외 대신 반환한다."""

    success: bool
    session: Optional[AuthSession] = None
    error: Optional[str] = None


@dataclass
class SupabaseUserInfo:
    """Supabase Auth 상의 사용자 신원(가벼운 정보만)."""

    id: str
    email: str


@dataclass
class AppUserProfile:
    """public.app_users 테이블의 현재 사용자 행 (승인 상태 + 역할)."""

    id: str
    email: str
    status: str  # 'pending' | 'approved' | 'blocked'
    role: str  # 'viewer' | 'editor' | 'admin'

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @property
    def is_blocked(self) -> bool:
        return self.status == "blocked"

    @property
    def is_approved(self) -> bool:
        return self.status == "approved"

    @property
    def can_write(self) -> bool:
        return self.is_approved and self.role in ("editor", "admin")

    @property
    def is_admin(self) -> bool:
        """운영 관리자 여부 — approved 상태이고 role이 admin일 때만 True.

        Phase 3-2: 아직 이 값을 소비하는 관리자 UI/기능은 없다(판정 로직만
        추가). can_write(editor/admin 동일 취급)와는 별개 — admin이라도
        status가 approved가 아니면(pending/blocked) is_admin은 항상 False다.
        """
        return self.is_approved and self.role == "admin"


def _session_dir() -> str:
    """세션 파일을 둘 디렉터리.

    storage/cloud_sync/ — Phase 2A의 CloudSyncService 캐시와 동일한 위치 관례를
    따른다(사용자가 수동으로 저장한 storage/*.json 목록과 섞이지 않도록 분리).
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "storage", "cloud_sync")
    os.makedirs(path, exist_ok=True)
    return path


def _token_fingerprint(token: str) -> str:
    """토큰 원문을 로그에 남기지 않기 위한, 되돌릴 수 없는 짧은 지문(앞 8자).

    SHA-256 해시의 앞 8자만 쓴다 — 로그에서 "같은 토큰인지" 정도만 구분할
    수 있으면 충분하고, 원문을 복원할 방법은 없어야 한다(요구사항 6절)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _external_caller_id() -> str:
    """이 모듈(auth_service.py) 밖에서 세션 갱신을 유발한 첫 호출부를
    "파일명:함수명" 형태로 반환한다.

    자동 새로고침 타이머/수동 새로고침 버튼/Realtime 재연결/앱 시작 시
    세션 복원 등 여러 경로가 모두 refresh_session()으로 모이기 때문에,
    호출부마다 로그 인자를 추가하는 대신 스택을 직접 훑어 호출 경로를
    식별한다(요구사항 6절 "호출 스택/호출 경로 식별자")."""
    this_file = os.path.abspath(__file__)
    for frame_info in inspect.stack():
        if os.path.abspath(frame_info.filename) != this_file:
            return f"{os.path.basename(frame_info.filename)}:{frame_info.function}"
    return "unknown"


def _is_invalid_refresh_token_error(exc: Exception) -> bool:
    """"Invalid Refresh Token: Already Used"류(회전된 토큰 재사용) 오류인지
    판별한다.

    설치된 supabase_auth 라이브러리의 AuthApiError에는 이 케이스 전용
    타입(.code) 값이 없다(소스 직접 확인 — ErrorCode Literal 82종 중
    refresh_token 관련 전용 코드 없음) — 그래서 메시지 텍스트로 판별한다.
    이 판정이 True면 같은 refresh_token으로는 다시 시도하지 않는다
    (요구사항 7절)."""
    message = str(exc).lower()
    if "refresh token" not in message and "refresh_token" not in message:
        return False
    return "already used" in message or "invalid" in message or "not found" in message


def _parse_port_list(raw: str) -> list[int]:
    ports: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ports.append(int(part))
        except ValueError:
            logger.warning(f"SUPABASE_OAUTH_CALLBACK_PORTS 값이 잘못됨(무시): {part!r}")
    return ports


class AuthService:
    """Supabase Auth 세션을 로그인/로드/검증/갱신/로그아웃하는 서비스.

    이 클래스는 UI를 참조하지 않고, 네트워크 오류를 예외로 전파하지 않는다
    (CloudSyncService와 동일한 설계 원칙) — login_with_google() 계열도 예외
    대신 AuthResult(success=False, error=...)로 실패를 알린다.
    """

    def __init__(
        self,
        config: Optional[CloudConfig] = None,
        client_manager: Optional[SupabaseClientManager] = None,
    ):
        self._config = config or get_cloud_config()
        # client_manager 주입은 (1) 테스트에서 fake 클라이언트를 넣기 위해서,
        # 그리고 (2) 운영 코드에서 CloudSyncService와 "같은" Supabase Client를
        # 공유하기 위해서 쓴다 — CloudSyncCoordinator가 기본 생성 시
        # CloudSyncService(client_manager=auth_service.client_manager)로 이 매니저를
        # 넘겨받는다. 공유하지 않으면 로그인 세션이 CloudSyncService 쪽 Client에는
        # 전혀 반영되지 않아 messages 쓰기가 전부 RLS(42501)로 거부된다(버그 수정 이력).
        self._client_mgr = client_manager or SupabaseClientManager(self._config)
        self._session_path = os.path.join(_session_dir(), "session.dat")
        # refresh_session()을 동시에 여러 스레드가 호출해도(자동 새로고침
        # 타이머, 수동 새로고침, Realtime 재연결 등) 실제 네트워크 갱신
        # 요청은 최대 1건만 나가도록 직렬화한다(요구사항 2절) — Supabase의
        # refresh_token은 1회용(rotate)이라 동시에 같은 토큰으로 두 요청을
        # 보내면 하나는 반드시 "Invalid Refresh Token: Already Used"로
        # 실패한다.
        self._refresh_lock = threading.Lock()
        # 가장 최근에 저장에 성공한 세션의 메모리 캐시 — Realtime
        # 재연결처럼 네트워크 호출이 금지된 호출부(get_cached_session_tokens
        # 참고)가 디스크 재조회 없이 최신 값을 즉시 읽을 수 있게 한다.
        self._session_cache: Optional[AuthSession] = None
        # 이미 "Already Used"로 실패한 것으로 확인된 refresh_token — 같은
        # 프로세스에서 같은 토큰으로 다시 네트워크 요청을 보내지 않기 위한
        # 표시(요구사항 7절 "동일 토큰으로 재시도하지 않음").
        self._last_failed_refresh_token: Optional[str] = None

    @property
    def client_manager(self) -> SupabaseClientManager:
        """CloudSyncService 등 다른 서비스와 Supabase Client를 공유하기 위해 노출한다."""
        return self._client_mgr

    # ===== 세션 조회 =====

    def load_session(self) -> Optional[AuthSession]:
        """디스크에 저장된 세션을 그대로 반환한다 (만료 여부 확인/refresh 시도 없음)."""
        return self._load_session()

    def get_session(self) -> Optional[AuthSession]:
        """유효한(만료되지 않은) 세션을 반환한다. 없거나 갱신 실패 시 None.

        만료된 세션이 있으면 refresh_session()을 1회 시도한다 — 그래도
        실패하면 로그인 필요 상태로 취급한다(무한 재시도하지 않음).
        """
        session = self._load_session()
        if session is None:
            return None

        if session.is_expired():
            logger.info("저장된 Supabase 세션이 만료되어 갱신을 시도합니다.")
            result = self.refresh_session()
            if not result.success:
                logger.warning(f"세션 갱신 실패 — 로그인이 다시 필요합니다: {result.error}")
                return None
            return result.session

        return session

    def is_logged_in(self) -> bool:
        return self.get_session() is not None

    def get_cached_session_tokens(self) -> tuple:
        """네트워크 호출 없이 (access_token, refresh_token)을 반환한다.

        메모리 캐시(이 프로세스에서 가장 최근에 저장에 성공한 세션)가 있으면
        그것을, 없으면 디스크에 저장된 세션을 그대로 읽어 반환한다 — 만료
        여부 확인이나 갱신 시도는 하지 않는다(load_session()과 동일한 계약).
        RealtimeMessageSyncService의 get_session_tokens_fn처럼 블로킹
        네트워크 호출이 금지된 호출부 전용이다 — 메모리 캐시를 먼저 보는
        만큼, 갱신 직후에는 디스크를 다시 읽는 것보다 더 최신 값을 즉시
        돌려줄 수 있다."""
        session = self._session_cache or self._load_session()
        if session is None:
            return (None, None)
        return (session.access_token, session.refresh_token)

    # ===== 세션 갱신/적용 =====

    def refresh_session(self) -> AuthResult:
        """저장된 refresh_token으로 세션 갱신을 시도한다.

        자동 새로고침 타이머, API 호출 전후, Realtime 재연결, 수동 새로고침
        버튼 등 여러 스레드가 동시에 이 메서드를 호출할 수 있다 — 이 모든
        경로를 self._refresh_lock 하나로 직렬화한다(요구사항 2절). 락을
        기다리던 호출은 락을 얻은 뒤 디스크에서 세션을 다시 읽어, 그 사이
        먼저 락을 잡았던 다른 호출이 이미 갱신을 끝냈으면 새 네트워크
        요청 없이 그 결과를 그대로 재사용한다 — Supabase의 refresh_token은
        1회용(rotate)이라, 같은 토큰으로 두 번째 네트워크 요청을 보내면
        "Invalid Refresh Token: Already Used"로 반드시 실패하기 때문이다.
        """
        caller = _external_caller_id()
        acquired_immediately = self._refresh_lock.acquire(blocking=False)
        if not acquired_immediately:
            self._refresh_lock.acquire()
        waited = not acquired_immediately
        try:
            # 락 대기 중 다른 스레드가 이미 갱신을 끝냈을 수 있으므로, 락을
            # 잡은 뒤 디스크에서 세션을 다시 읽는다(락 획득 전에 읽은 값은
            # 쓰지 않는다).
            stored = self._load_session()
            if stored is None or not stored.refresh_token:
                return AuthResult(False, error="저장된 세션이 없습니다 — 로그인이 필요합니다.")

            if not stored.is_expired():
                logger.info(
                    f"세션 갱신 요청={caller}, 락대기={waited}, 결과=스킵(다른 요청이 이미 갱신 완료)"
                )
                return AuthResult(True, session=stored)

            token_fp = _token_fingerprint(stored.refresh_token)

            if stored.refresh_token == self._last_failed_refresh_token:
                logger.warning(
                    f"세션 갱신 요청={caller}, 락대기={waited}, token_fp={token_fp}, "
                    f"결과=거부(이미 무효로 확인된 refresh_token 재사용 방지)"
                )
                return AuthResult(False, error="INVALID_REFRESH_TOKEN: 재로그인이 필요합니다.")

            client_result = self._client_mgr.get_client()
            if not client_result.success:
                logger.warning(
                    f"세션 갱신 요청={caller}, 락대기={waited}, token_fp={token_fp}, "
                    f"결과=실패(클라이언트 획득 오류)"
                )
                return AuthResult(False, error=client_result.error)

            try:
                auth_response = client_result.client.auth.refresh_session(stored.refresh_token)
            except Exception as e:
                if _is_invalid_refresh_token_error(e):
                    self._last_failed_refresh_token = stored.refresh_token
                    self._invalidate_local_session()
                    logger.warning(
                        f"세션 갱신 요청={caller}, 락대기={waited}, token_fp={token_fp}, "
                        f"결과=실패(회전된 refresh_token 재사용 감지) — 재로그인이 필요합니다."
                    )
                    return AuthResult(False, error="INVALID_REFRESH_TOKEN: 재로그인이 필요합니다.")
                logger.warning(
                    f"세션 갱신 요청={caller}, 락대기={waited}, token_fp={token_fp}, "
                    f"결과=실패({type(e).__name__})"
                )
                return AuthResult(False, error=str(e))

            if auth_response.session is None:
                logger.warning(
                    f"세션 갱신 요청={caller}, 락대기={waited}, token_fp={token_fp}, "
                    f"결과=실패(응답에 세션 없음)"
                )
                return AuthResult(False, error="세션 갱신 응답에 세션이 없습니다.")

            new_session = _session_from_supabase_auth_response(auth_response)
            self._last_failed_refresh_token = None
            save_ok = self._save_session(new_session)
            logger.info(
                f"세션 갱신 요청={caller}, 락대기={waited}, token_fp={token_fp}, 결과=성공, "
                f"저장={'성공' if save_ok else '실패'}, 경로={self._session_path}, "
                f"expires_at={new_session.expires_at}"
            )
            return AuthResult(True, session=new_session)
        finally:
            self._refresh_lock.release()

    def _invalidate_local_session(self) -> None:
        """복구 불가능한 것으로 확인된 세션(예: 이미 사용된 refresh_token)을
        로컬에서 완전히 제거한다.

        메모리 캐시와 디스크 파일을 모두 지워, 이후 어떤 호출부도(이번 실행
        중이든 다음 실행이든) 같은 죽은 토큰으로 다시 시도하지 않고 곧바로
        "로그인 필요" 상태로 취급하게 한다(요구사항 7절 "세션을 명확히
        무효화")."""
        self._session_cache = None
        try:
            if os.path.exists(self._session_path):
                os.remove(self._session_path)
                logger.info("복구 불가능한 세션을 감지해 로컬 세션 파일을 삭제했습니다 — 재로그인이 필요합니다.")
        except OSError as e:
            logger.warning(f"무효화된 세션 파일 삭제 오류: {e}")

    def apply_session_to_client(self, session: Optional[AuthSession] = None) -> AuthResult:
        """저장된(또는 주어진) 세션을 Supabase client의 인증 상태에 반영한다.

        이후 client.table(...) 호출이 이 세션으로 인증되어 RLS의 auth.uid()가
        올바르게 채워진다. set_session()은 세션이 만료되었으면 자동으로 refresh를
        시도한다(supabase_auth.SyncGoTrueClient.set_session 소스 확인됨).
        """
        session = session or self.get_session()
        if session is None:
            return AuthResult(False, error="적용할 세션이 없습니다 — 로그인이 필요합니다.")

        client_result = self._client_mgr.get_client()
        if not client_result.success:
            return AuthResult(False, error=client_result.error)

        try:
            auth_response = client_result.client.auth.set_session(
                session.access_token, session.refresh_token
            )
        except Exception as e:
            logger.warning(f"세션을 클라이언트에 적용하는 데 실패했습니다: {e}")
            return AuthResult(False, error=str(e))

        if auth_response.session is None:
            return AuthResult(False, error="세션 적용에 실패했습니다.")

        refreshed = _session_from_supabase_auth_response(auth_response)
        if refreshed.access_token != session.access_token:
            # set_session이 만료된 세션을 자동으로 갱신했다면 갱신된 값을 다시 저장한다.
            self._save_session(refreshed)

        return AuthResult(True, session=refreshed)

    # ===== Google 로그인 (PKCE) =====

    def login_with_google(self, timeout_seconds: float = 120.0) -> AuthResult:
        """브라우저에서 Google 로그인을 진행하고 성공 시 세션을 저장한다.

        블로킹 호출이다 — 호출부가 반드시 별도 스레드에서 실행해야 한다
        (메인 UI 스레드를 막지 않기 위함).

        취소/시간초과/브라우저 닫힘은 모두 예외가 아니라
        AuthResult(success=False, error=...)로 정상 반환된다.
        """
        client_result = self._client_mgr.get_client()
        if not client_result.success:
            return AuthResult(False, error=client_result.error)
        client = client_result.client

        candidate_ports = _parse_port_list(os.environ.get("SUPABASE_OAUTH_CALLBACK_PORTS", ""))
        try:
            server = OAuthCallbackServer(candidate_ports=candidate_ports or None)
        except RuntimeError as e:
            logger.error(f"OAuth 콜백 서버 시작 실패: {e}")
            return AuthResult(False, error=str(e))

        try:
            oauth_response = client.auth.sign_in_with_oauth(
                {
                    "provider": "google",
                    "options": {"redirect_to": server.redirect_url},
                }
            )
        except Exception as e:
            server.close()
            logger.error(f"Google OAuth URL 생성 실패: {e}")
            return AuthResult(False, error=str(e))

        logger.info("브라우저에서 Google 로그인 페이지를 엽니다.")
        if not webbrowser.open(oauth_response.url):
            server.close()
            logger.warning("기본 브라우저를 여는 데 실패했습니다.")
            return AuthResult(False, error="기본 브라우저를 열 수 없습니다.")

        result = server.wait_for_callback(timeout_seconds=timeout_seconds)

        if result.timed_out:
            logger.warning("Google 로그인 대기 시간이 초과되었습니다.")
            return AuthResult(
                False, error=f"로그인 시간이 초과되었습니다 ({int(timeout_seconds)}초). 다시 시도해 주세요."
            )

        if result.error:
            # 사용자가 동의 화면을 취소했거나(Google이 error=access_denied로 리디렉션),
            # 그 외 브라우저 단의 실패 — 정상적인 흐름으로 처리한다(예외 아님).
            logger.info(f"Google 로그인이 완료되지 않았습니다: {result.error}")
            return AuthResult(False, error=f"로그인이 취소되었거나 실패했습니다: {result.error}")

        return self.complete_oauth_callback(code=result.code, redirect_to=server.redirect_url)

    def complete_oauth_callback(self, code: str, redirect_to: str) -> AuthResult:
        """수신한 인가 코드를 Supabase 세션으로 교환하고 DPAPI로 저장한다.

        login_with_google() 내부에서 호출되지만, 콜백 수신 이후 단계만 독립적으로
        테스트할 수 있도록 공개 메서드로 분리했다.
        """
        client_result = self._client_mgr.get_client()
        if not client_result.success:
            return AuthResult(False, error=client_result.error)

        try:
            auth_response = client_result.client.auth.exchange_code_for_session(
                {
                    "auth_code": code,
                    # 빈 문자열을 넘기면 sign_in_with_oauth()가 저장해둔 PKCE
                    # verifier를 SDK가 내부적으로 자동 조회한다(소스 확인됨) —
                    # 여기서 verifier 값을 직접 다루거나 로그로 남기지 않는다.
                    "code_verifier": "",
                    "redirect_to": redirect_to,
                }
            )
        except Exception:
            # 예외 메시지에 code/verifier 관련 요청 정보가 포함될 수 있어 상세를
            # 로그로 남기지 않는다(원칙 9) — 실패 사실만 기록한다.
            logger.error("Supabase 세션 교환에 실패했습니다.")
            return AuthResult(False, error="Supabase 세션 교환에 실패했습니다.")

        if auth_response.session is None:
            return AuthResult(False, error="세션을 발급받지 못했습니다.")

        session = _session_from_supabase_auth_response(auth_response)
        self._save_session(session)
        logger.info("Google 로그인 성공 — 세션 저장 완료")
        return AuthResult(True, session=session)

    # ===== 사용자/프로필 조회 =====

    def get_current_user(self) -> Optional[SupabaseUserInfo]:
        """현재 세션의 Supabase Auth 사용자 정보를 반환한다."""
        apply_result = self.apply_session_to_client()
        if not apply_result.success:
            return None

        client_result = self._client_mgr.get_client()
        if not client_result.success:
            return None

        try:
            user_response = client_result.client.auth.get_user()
        except Exception as e:
            logger.warning(f"현재 사용자 조회 실패: {e}")
            return None

        if user_response is None or user_response.user is None:
            return None
        return SupabaseUserInfo(id=user_response.user.id, email=user_response.user.email or "")

    def get_app_user_profile(self) -> Optional[AppUserProfile]:
        """public.app_users에서 현재 사용자의 승인 상태/역할을 조회한다.

        RLS(app_users_select_self)에 의해 항상 본인 행만 보인다. 조회 실패
        (네트워크 오류, 아직 트리거가 행을 못 만든 경우 등)에는 None을 반환한다
        — 호출부는 None을 "확인 불가"로 취급하고 안전한 쪽(쓰기 금지)으로
        처리해야 한다.
        """
        user = self.get_current_user()
        if user is None:
            return None

        client_result = self._client_mgr.get_client()
        if not client_result.success:
            return None

        try:
            response = (
                client_result.client.table(_APP_USERS_TABLE)
                .select("id,email,status,role")
                .eq("id", user.id)
                .limit(1)
                .execute()
            )
        except Exception as e:
            logger.warning(f"app_users 프로필 조회 실패: {e}")
            return None

        rows = response.data or []
        if not rows:
            logger.warning("app_users에 현재 사용자 행이 없습니다 (가입 트리거 지연일 수 있음).")
            return None

        row = rows[0]
        return AppUserProfile(
            id=row["id"],
            email=row.get("email") or user.email,
            status=row["status"],
            role=row["role"],
        )

    # ===== 로그아웃 =====

    def logout(self) -> None:
        """Supabase 서버 세션을 best-effort로 폐기하고, 로컬 DPAPI 세션을 삭제한다."""
        client_result = self._client_mgr.get_client()
        if client_result.success:
            try:
                client_result.client.auth.sign_out()
            except Exception as e:
                # sign_out()은 SDK 내부적으로 이미 AuthApiError를 흡수하지만,
                # 그 외(네트워크 등) 예외까지 로그아웃 실패로 이어지지 않도록 방어한다.
                logger.debug(f"서버 측 로그아웃 요청 중 오류(무시): {e}")

        self._session_cache = None
        self._last_failed_refresh_token = None
        try:
            if os.path.exists(self._session_path):
                os.remove(self._session_path)
                logger.info("로컬 Supabase 세션 삭제 완료")
        except OSError as e:
            logger.warning(f"세션 삭제 오류: {e}")

    # ===== 내부: 암호화 저장/로드 =====

    def _load_session(self) -> Optional[AuthSession]:
        if not os.path.exists(self._session_path):
            return None

        if not _DPAPI_AVAILABLE:
            logger.warning("win32crypt(pywin32)를 사용할 수 없어 저장된 세션을 읽을 수 없습니다.")
            return None

        try:
            with open(self._session_path, "rb") as f:
                encrypted = f.read()
            _, decrypted = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
            raw = json.loads(decrypted.decode("utf-8"))

            file_version = raw.pop("session_version", None)
            if file_version != _SESSION_SCHEMA_VERSION:
                logger.warning(
                    f"세션 파일 버전이 호환되지 않아(파일={file_version!r}, "
                    f"현재={_SESSION_SCHEMA_VERSION}) 폐기합니다 — 재로그인이 필요합니다."
                )
                self._invalidate_local_session()
                return None

            return AuthSession(**raw)
        except Exception as e:
            # DPAPI는 다른 사용자 계정/다른 PC에서 복호화를 시도하면 반드시 실패한다
            # (설계상 의도된 동작) — 이 경우도 포함해 "세션 없음"으로 안전하게 처리한다.
            logger.warning(f"세션 파일을 읽을 수 없어 로그인 필요 상태로 처리합니다: {e}")
            return None

    def _save_session(self, session: AuthSession) -> bool:
        """세션을 DPAPI로 암호화해 저장한다.

        임시 파일에 쓰고 flush()+fsync()로 디스크에 실제로 반영한 뒤
        os.replace()로 원자적으로 교체한다(요구사항 4절) — 쓰는 도중
        프로세스가 죽거나 다른 쓰기와 겹쳐도, 최종 경로(session.dat)에는
        "이전 파일 그대로" 또는 "새 파일로 완전히 교체됨" 둘 중 하나만
        있고, 일부만 쓰인 손상된 파일이 최종 경로에 남는 경우는 없다.

        성공 시 메모리 캐시도 함께 갱신한다(get_cached_session_tokens 참고).
        """
        if not _DPAPI_AVAILABLE:
            logger.error("win32crypt(pywin32)를 사용할 수 없어 세션을 저장할 수 없습니다.")
            return False

        tmp_path = self._session_path + ".tmp"
        try:
            payload = asdict(session)
            payload["session_version"] = _SESSION_SCHEMA_VERSION
            raw = json.dumps(payload).encode("utf-8")
            encrypted = win32crypt.CryptProtectData(raw, _DPAPI_DESCRIPTION, None, None, None, 0)
            with open(tmp_path, "wb") as f:
                f.write(encrypted)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._session_path)
        except OSError as e:
            logger.error(f"세션 저장 오류: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False

        self._session_cache = session
        logger.info("Supabase 세션을 DPAPI로 암호화해 저장했습니다.")
        return True


def _session_from_supabase_auth_response(auth_response) -> AuthSession:
    """supabase-py의 AuthResponse(세션 발급/갱신/교환 공통 반환형)를 AuthSession으로 변환한다."""
    session = auth_response.session
    user = auth_response.user or session.user
    expires_at = datetime.fromtimestamp(session.expires_at, tz=timezone.utc).isoformat()
    return AuthSession(
        user_id=user.id,
        email=user.email or "",
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        expires_at=expires_at,
    )
