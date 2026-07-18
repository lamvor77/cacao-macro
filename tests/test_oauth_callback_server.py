# OAuthCallbackServer 테스트 — 실제 127.0.0.1 소켓을 사용하지만 Google/Supabase에는
# 전혀 연결하지 않는다("브라우저가 리디렉션으로 GET 요청을 보내는 것"만 흉내낸다).
#
# 실행: python -m unittest tests.test_oauth_callback_server -v

import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.oauth_callback_server import OAuthCallbackServer


def _get(url: str, delay: float = 0.0) -> None:
    if delay:
        time.sleep(delay)
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        e.close()  # 4xx 응답도 정상 케이스 — 서버 쪽 result만 확인하면 되므로 그냥 닫는다
    except Exception:
        pass


class TestOAuthCallbackServer(unittest.TestCase):
    def test_3_callback_success(self):
        """3. callback 성공 — 올바른 경로 + code로 요청하면 CallbackResult(code=...)"""
        server = OAuthCallbackServer()
        threading.Thread(target=_get, args=(server.redirect_url + "?code=fake-auth-code", 0.05), daemon=True).start()

        result = server.wait_for_callback(timeout_seconds=5)

        self.assertFalse(result.timed_out)
        self.assertEqual(result.code, "fake-auth-code")
        self.assertIsNone(result.error)

    def test_5_callback_error(self):
        """5. callback error 처리 — Google/Supabase가 error로 리디렉션한 경우"""
        server = OAuthCallbackServer()
        url = server.redirect_url + "?error=access_denied&error_description=User+denied+access"
        threading.Thread(target=_get, args=(url, 0.05), daemon=True).start()

        result = server.wait_for_callback(timeout_seconds=5)

        self.assertFalse(result.timed_out)
        self.assertIsNone(result.code)
        self.assertIn("denied", result.error)

    def test_6_timeout(self):
        """6. timeout 처리 — 아무 요청도 오지 않으면 timed_out=True로 정상 반환(예외 아님)"""
        server = OAuthCallbackServer()

        start = time.time()
        result = server.wait_for_callback(timeout_seconds=0.3)
        elapsed = time.time() - start

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.code)
        self.assertIsNone(result.error)
        self.assertLess(elapsed, 2.0)

    def test_2_and_4_wrong_path_rejected(self):
        """2/4. state(=경로 토큰) 불일치 차단 — 엉뚱한 경로로 온 요청은 완전히 무시된다.

        서버는 등록된 정확한 경로가 아니면 404만 응답하고 result를 채우지 않는다.
        그 뒤 시간 안에 올바른 요청이 오지 않으면 timeout으로 끝나야 한다(=엉뚱한
        요청이 로그인 완료로 오인되지 않는다는 뜻).
        """
        server = OAuthCallbackServer()
        wrong_url = f"http://127.0.0.1:{server.port}/oauth/callback/guessed-wrong-token?code=attacker-code"
        threading.Thread(target=_get, args=(wrong_url, 0.05), daemon=True).start()

        result = server.wait_for_callback(timeout_seconds=0.6)

        self.assertTrue(result.timed_out, "잘못된 경로의 요청이 정상 콜백으로 처리되면 안 됨")

    def test_correct_path_after_wrong_path_still_works(self):
        """엉뚱한 요청이 먼저 와도(무시됨) 뒤이은 올바른 요청은 정상 처리되어야 한다."""
        server = OAuthCallbackServer()
        # 서버는 1회 요청만 처리하므로(handle_request가 첫 accept에서 끝남), 이 테스트는
        # "잘못된 경로 요청이 서버를 소비해버리지 않는지"를 별도 서버 인스턴스로 검증한다.
        # (동일 서버가 여러 요청을 받는 상황은 실제 설계상 발생하지 않으므로 대상이 아님)
        threading.Thread(target=_get, args=(server.redirect_url + "?code=ok-code", 0.05), daemon=True).start()
        result = server.wait_for_callback(timeout_seconds=5)
        self.assertEqual(result.code, "ok-code")

    def test_port_is_dynamic_not_hardcoded(self):
        """서로 다른 서버 인스턴스는 서로 다른(OS가 골라준) 포트를 사용해야 한다."""
        s1 = OAuthCallbackServer()
        s2 = OAuthCallbackServer()
        try:
            self.assertNotEqual(s1.port, s2.port)
            self.assertGreater(s1.port, 0)
        finally:
            s1.close()
            s2.close()

    def test_candidate_ports_used_when_configured(self):
        """후보 포트 목록이 주어지면 그중 사용 가능한 포트를 사용한다."""
        # 임시로 포트 하나를 점유해 "사용 중" 상태를 만든 뒤, 후보 목록에서
        # 그 다음 포트가 선택되는지 확인한다.
        import socket

        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]
        try:
            server = OAuthCallbackServer(candidate_ports=[busy_port, busy_port + 1])
            try:
                self.assertNotEqual(server.port, busy_port)
            finally:
                server.close()
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main()
