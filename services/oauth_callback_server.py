# 로컬 loopback OAuth 콜백 서버
#
# Google/Supabase 로그인이 끝나면 브라우저가 이 서버로 리디렉션한다.
# 127.0.0.1(외부에 노출되지 않음)의 포트 하나에서 요청을 딱 1번만 받고 즉시
# 종료한다. 포트는 기본적으로 OS가 골라주는 임시(ephemeral) 포트를 쓴다
# (고정 포트 하드코딩 없음) — Supabase 대시보드의 Redirect URLs에 와일드카드
# 패턴을 등록해두면 매번 포트가 달라져도 동작한다 (docs/PHASE2D_GOOGLE_OAUTH.md
# 참고). 와일드카드 설정이 여의치 않은 환경을 위해 후보 포트 목록을 직접
# 지정하는 것도 지원한다.
#
# 이 서버는 code(또는 error) 쿼리스트링이 그대로 콘솔 로그에 찍히지 않도록
# BaseHTTPRequestHandler의 기본 요청 로그를 억제한다.

import hmac
import logging
import secrets
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_SUCCESS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>로그인 완료</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px;">
<h2>로그인이 완료되었습니다.</h2>
<p>이 창을 닫고 프로그램으로 돌아가세요.</p>
</body></html>"""

_FAILURE_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>로그인 실패</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px;">
<h2>로그인에 실패했습니다.</h2>
<p>{message}</p>
<p>이 창을 닫고 프로그램으로 돌아가세요.</p>
</body></html>"""

_NOT_FOUND_HTML = """<!doctype html><html><body><p>Not Found</p></body></html>"""


@dataclass
class CallbackResult:
    """콜백 수신 결과. code/error 중 하나만 채워진다. 둘 다 None이면 시간 초과."""

    code: Optional[str] = None
    error: Optional[str] = None

    @property
    def timed_out(self) -> bool:
        return self.code is None and self.error is None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 규약)
        parsed = urlparse(self.path)
        expected_path = self.server.expected_path  # type: ignore[attr-defined]

        # 무작위 토큰이 포함된 경로와 정확히 일치하는 요청만 처리한다 — 이 프로그램이
        # 시작한 이 로그인 시도가 아닌 다른 요청(예: 브라우저의 자동 프리페치,
        # 동시에 실행 중인 다른 프로그램의 우연한 접근)을 걸러내는 최소한의
        # CSRF/재생 방지 장치다. hmac.compare_digest로 타이밍 공격도 피한다.
        if not hmac.compare_digest(parsed.path, expected_path):
            self.send_response(404)
            self._write(_NOT_FOUND_HTML)
            return

        qs = parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
        error = qs.get("error_description", [None])[0] or qs.get("error", [None])[0]

        if code:
            self.server.result = CallbackResult(code=code)  # type: ignore[attr-defined]
            self.send_response(200)
            self._write(_SUCCESS_HTML)
        else:
            message = error or "code 파라미터를 받지 못했습니다."
            self.server.result = CallbackResult(error=message)  # type: ignore[attr-defined]
            self.send_response(400)
            self._write(_FAILURE_HTML_TEMPLATE.format(message=message))

    def _write(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # 기본 구현은 요청 라인(=code/error 쿼리스트링 포함)을 그대로 stderr에
        # 남긴다 — 인가 코드가 로그에 노출되지 않도록 완전히 억제한다.
        pass


class OAuthCallbackServer:
    """127.0.0.1의 임시 포트에서 OAuth 콜백을 1회만 받고 종료하는 서버."""

    def __init__(self, candidate_ports: Optional[list[int]] = None):
        self._token = secrets.token_urlsafe(24)
        self._expected_path = f"/oauth/callback/{self._token}"
        self._server = self._bind(candidate_ports)
        self._server.expected_path = self._expected_path  # type: ignore[attr-defined]
        self._server.result = None  # type: ignore[attr-defined]
        self._closed = False

    def _bind(self, candidate_ports: Optional[list[int]]) -> HTTPServer:
        if candidate_ports:
            last_error: Optional[OSError] = None
            for port in candidate_ports:
                try:
                    return HTTPServer(("127.0.0.1", port), _Handler)
                except OSError as e:
                    last_error = e
                    continue
            raise RuntimeError(
                f"설정된 콜백 포트가 모두 사용 중입니다: {candidate_ports}"
            ) from last_error
        # 포트 0 = OS가 사용 가능한 임시 포트를 자동 할당한다 (고정 포트 없음).
        return HTTPServer(("127.0.0.1", 0), _Handler)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def redirect_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self._expected_path}"

    def wait_for_callback(self, timeout_seconds: float = 120.0) -> CallbackResult:
        """콜백을 1건 받거나 timeout_seconds가 지날 때까지 블로킹한다.

        네트워크 대기를 포함하므로 호출부가 반드시 별도 스레드에서 실행해야
        한다 — 메인 UI 스레드를 막지 않기 위해서다.
        """
        self._server.timeout = timeout_seconds
        try:
            self._server.handle_request()
        finally:
            self.close()
        return self._server.result or CallbackResult()  # type: ignore[attr-defined]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._server.server_close()
        except OSError as e:
            logger.debug(f"콜백 서버 소켓 정리 중 오류(무시): {e}")
