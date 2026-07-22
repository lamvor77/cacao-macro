# gui/panels/diagnostics_panel.py 정적 검사
#
# 이 프로젝트는 GUI 패널을 Tk 인스턴스화해서 테스트하지 않는 기존 관례를
# 따른다(gui/main_window.py 등도 동일) — 대신 소스 텍스트 자체를 검사해
# "시작일/만료일/남은 기간"류 문구가 진단 화면에 다시 나타나지 않는지
# 회귀를 막는다(배포 전 마지막 보완 작업 요구사항 6절 12번).

import os
import unittest

_PANEL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "gui", "panels", "diagnostics_panel.py",
)


class TestDiagnosticsPanelNoPeriodFields(unittest.TestCase):
    def setUp(self):
        with open(_PANEL_PATH, encoding="utf-8") as f:
            self._source = f.read()

    def test_no_start_date_label(self):
        self.assertNotIn("시작일", self._source)

    def test_no_end_date_label(self):
        self.assertNotIn("만료일", self._source)

    def test_no_remaining_days_label(self):
        self.assertNotIn("남은 일수", self._source)
        self.assertNotIn("남은 기간", self._source)

    def test_no_remaining_days_field_access(self):
        self.assertNotIn("remaining_days", self._source)
        self.assertNotIn("s.license.start_date", self._source)
        self.assertNotIn("s.license.end_date", self._source)


if __name__ == "__main__":
    unittest.main()
