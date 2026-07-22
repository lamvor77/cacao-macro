# scripts/health_snapshot.py의 _tail_log_flags() 테스트
#
# 실제 psutil 프로세스나 장시간 루프는 테스트하지 않는다(수동 E2E 영역) —
# 로그 파일에서 "새로 추가된 부분만" 훑어 키워드 존재 여부를 판단하는 순수
# 파일 파싱 로직만 검증한다.

import importlib.util
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(PROJECT_ROOT, "scripts", "health_snapshot.py")

_spec = importlib.util.spec_from_file_location("health_snapshot", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["health_snapshot"] = _mod
_spec.loader.exec_module(_mod)


class TestTailLogFlags(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

    def tearDown(self):
        os.remove(self.path)

    def _write(self, text: str):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file_returns_all_false(self):
        reconnect, subscribed, error, offset = _mod._tail_log_flags("no-such-file.log", 0)
        self.assertFalse(reconnect)
        self.assertFalse(subscribed)
        self.assertFalse(error)
        self.assertEqual(offset, 0)

    def test_detects_reconnect_keyword(self):
        self._write("[INFO] RECONNECTING 상태로 전환\n")
        reconnect, subscribed, error, offset = _mod._tail_log_flags(self.path, 0)
        self.assertTrue(reconnect)
        self.assertFalse(subscribed)
        self.assertFalse(error)
        self.assertGreater(offset, 0)

    def test_detects_subscribed_keyword(self):
        self._write("[INFO] 실시간 연결됨\n")
        reconnect, subscribed, error, offset = _mod._tail_log_flags(self.path, 0)
        self.assertFalse(reconnect)
        self.assertTrue(subscribed)

    def test_detects_error_keyword(self):
        self._write("[오류] 전송 실패\n")
        _, _, error, _ = _mod._tail_log_flags(self.path, 0)
        self.assertTrue(error)

    def test_only_reads_new_content_since_offset(self):
        self._write("[INFO] 실시간 연결됨\n")
        _, _, _, offset = _mod._tail_log_flags(self.path, 0)
        self._write("[INFO] RECONNECTING\n")
        reconnect, subscribed, error, offset2 = _mod._tail_log_flags(self.path, offset)
        self.assertTrue(reconnect)
        self.assertFalse(subscribed, "이전에 이미 읽은 SUBSCRIBED 줄은 다시 감지되면 안 됨")
        self.assertGreater(offset2, offset)

    def test_no_new_content_returns_all_false(self):
        self._write("[INFO] 실시간 연결됨\n")
        _, _, _, offset = _mod._tail_log_flags(self.path, 0)
        reconnect, subscribed, error, offset2 = _mod._tail_log_flags(self.path, offset)
        self.assertFalse(subscribed)
        self.assertEqual(offset, offset2)

    def test_rotated_file_smaller_than_offset_restarts_from_zero(self):
        self._write("x" * 100)
        reconnect, subscribed, error, offset = _mod._tail_log_flags(self.path, 500)
        self.assertEqual(offset, 100, "since_byte가 파일 크기보다 크면(회전됨) 처음부터 다시 읽어야 함")


if __name__ == "__main__":
    unittest.main()
