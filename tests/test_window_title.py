# gui/main_window.py::build_window_title() 테스트
#
# MainWindow(Tk 위젯)를 인스턴스화하지 않는 기존 프로젝트 관례를 따른다 —
# 창 제목 문자열은 순수 함수로 분리되어 있어 렌더링 없이 테스트할 수 있다.
# 버전 표시를 v1.2.1로 통일하는 작업(v1.2.1 패치 릴리스)의 회귀 테스트.

import unittest
from unittest.mock import patch

import gui.main_window as mw


class TestBuildWindowTitle(unittest.TestCase):
    def test_production_environment_shows_version_only(self):
        with patch.object(mw, "APP_VERSION", "1.2.1"):
            self.assertEqual(
                mw.build_window_title(False),
                "카카오톡 자동 메시지 전송 — v1.2.1",
            )

    def test_test_environment_appends_suffix(self):
        with patch.object(mw, "APP_VERSION", "1.2.1"):
            self.assertEqual(
                mw.build_window_title(True),
                "카카오톡 자동 메시지 전송 — v1.2.1 — TEST ENVIRONMENT",
            )

    def test_title_tracks_whatever_app_version_currently_is(self):
        """향후 버전이 다시 바뀌어도(예: 1.3.0) 이 함수가 config/version.py의
        APP_VERSION을 그대로 반영하는지 — 버전 문자열 이중 관리 방지."""
        with patch.object(mw, "APP_VERSION", "9.9.9"):
            self.assertIn("v9.9.9", mw.build_window_title(False))


if __name__ == "__main__":
    unittest.main()
