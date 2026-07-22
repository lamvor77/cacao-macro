# storage/data_manager.py 테스트
# PC 단톡방 목록 저장을 "파일명 입력" 방식에서 "탐색기 저장 대화상자" 방식으로
# 바꾸면서(gui/main_window.py::_on_save_list), 대화상자 자체(Tkinter)는 기존
# 프로젝트 관례상 테스트하지 않고(테스트 스위트에 GUI 클래스 인스턴스화 사례가
# 없음), 대화상자가 반환하는 "선택된 경로"를 받아 실제로 파일을 쓰고 읽는
# 순수 계층(DataManager, default_room_list_filename)을 단위 테스트한다.

import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest.mock import mock_open, patch

from storage.data_manager import DataManager, default_room_list_filename


class TestDefaultRoomListFilename(unittest.TestCase):
    def test_format_matches_requirement_example(self):
        now = datetime(2026, 7, 21, 15, 30, 0)
        name = default_room_list_filename(now)
        self.assertEqual(name, "카카오톡_단톡방목록_20260721_153000.json")

    def test_uses_current_time_when_not_given(self):
        before = datetime.now().replace(microsecond=0)
        name = default_room_list_filename()
        after = datetime.now().replace(microsecond=0)
        self.assertTrue(name.startswith("카카오톡_단톡방목록_"))
        self.assertTrue(name.endswith(".json"))
        stamp = name[len("카카오톡_단톡방목록_"):-len(".json")]
        parsed = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
        self.assertLessEqual(before, parsed)
        self.assertLessEqual(parsed, after)


class TestSaveAtDialogChosenPath(unittest.TestCase):
    """asksaveasfilename()이 반환할 법한 임의 경로에 실제로 저장/로드되는지 확인한다."""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(prefix="cacao_macro_test_")
        self._data = DataManager()
        self._data._storage_dir = self._tmp_dir  # 실제 storage 폴더를 건드리지 않는다

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_file_created_at_chosen_path_and_openable(self):
        filepath = os.path.join(self._tmp_dir, "카카오톡_단톡방목록_20260721_153000.json")
        rooms = {"방A": True, "방B": False, "방C": True}
        messages = {1: "안내1", 2: "", 3: "안내3"}

        self._data.save(rooms, messages, filepath)

        self.assertTrue(os.path.exists(filepath))
        loaded = self._data.load(filepath)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded["rooms"]), len(rooms))
        self.assertEqual({r["name"] for r in loaded["rooms"]}, set(rooms.keys()))
        self.assertEqual(loaded["messages"][1], "안내1")

    def test_korean_subfolder_and_filename_supported(self):
        korean_dir = os.path.join(self._tmp_dir, "한글 폴더 경로")
        os.makedirs(korean_dir, exist_ok=True)
        filepath = os.path.join(korean_dir, "저장된 단톡방 목록.json")

        self._data.save({"방1": True}, {1: "메시지"}, filepath)

        self.assertTrue(os.path.exists(filepath))
        loaded = self._data.load(filepath)
        self.assertEqual(len(loaded["rooms"]), 1)

    def test_room_count_matches_saved_file_row_count(self):
        filepath = os.path.join(self._tmp_dir, "목록.json")
        rooms = {f"방{i}": (i % 2 == 0) for i in range(10)}
        self._data.save(rooms, {}, filepath)

        loaded = self._data.load(filepath)
        self.assertEqual(len(loaded["rooms"]), len(rooms))

    def test_save_failure_raises_permission_error_for_caller_to_handle(self):
        filepath = os.path.join(self._tmp_dir, "잠긴파일.json")
        with patch("builtins.open", mock_open()) as mocked:
            mocked.side_effect = PermissionError("다른 프로그램에서 사용 중")
            with self.assertRaises(PermissionError):
                self._data.save({"방1": True}, {}, filepath)
        # 실패 시 tmp 파일이 남지 않아야 한다
        self.assertFalse(os.path.exists(filepath + ".tmp"))


if __name__ == "__main__":
    unittest.main()
