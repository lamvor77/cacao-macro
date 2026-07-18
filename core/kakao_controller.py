# 카카오톡 PC 창 제어 모듈
# Windows API(win32gui)를 사용해 카카오톡 프로세스의 창을 열거하고 제어한다.
# 카카오톡 자체 API를 사용하지 않으며, 일반 Windows 창 제어 방식으로 동작한다.

import ctypes
import logging
import time
from typing import Optional

import psutil
import pyautogui
import pyperclip
import win32con
import win32gui
import win32process

logger = logging.getLogger(__name__)

_KAKAO_EXE = "KakaoTalk.exe"
_KAKAO_MAIN_TITLE = "카카오톡"

# pyautogui 안전 장치 비활성화 (GUI 자동화 중 마우스 코너 이동으로 인한 중단 방지)
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05


class KakaoNotRunningError(Exception):
    """카카오톡이 실행 중이지 않을 때 발생하는 예외"""


class KakaoController:
    """카카오톡 PC 창을 Windows API로 제어하는 클래스"""

    def __init__(self):
        # 통합 검색창이 이미 열려있는지 추적
        # Ctrl+F를 다시 누르면 이전 검색어가 복원되어 붙여넣기가 뒤에 붙는 문제 방지
        self._search_open: bool = False

    def reset_search_state(self) -> None:
        """검색창 상태 초기화 — 카톡방 열기 세션 시작 시 호출."""
        self._search_open = False

    # ===== 프로세스 상태 확인 =====

    def is_running(self) -> bool:
        """카카오톡이 실행 중인지 확인한다."""
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() == _KAKAO_EXE.lower():
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return False

    def _get_pid(self) -> Optional[int]:
        """카카오톡 프로세스 PID를 반환한다."""
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() == _KAKAO_EXE.lower():
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return None

    # ===== 열린 채팅방 목록 수집 =====

    def get_open_chat_rooms(self) -> list[str]:
        """현재 열려 있는 카카오톡 채팅방 창 목록을 반환한다.

        카카오톡 프로세스에 속한 최상위 가시 창 중
        메인 창("카카오톡")을 제외한 것들이 채팅방 창이다.
        """
        if not self.is_running():
            raise KakaoNotRunningError("카카오톡이 실행 중이지 않습니다.")

        pid = self._get_pid()
        rooms: list[str] = []

        def _callback(hwnd: int, _) -> None:
            if not win32gui.IsWindowVisible(hwnd):
                return
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
            if window_pid != pid:
                return
            title = win32gui.GetWindowText(hwnd).strip()
            # 메인 창과 제목 없는 보조 창 제외
            if title and title != _KAKAO_MAIN_TITLE:
                rooms.append(title)

        win32gui.EnumWindows(_callback, None)
        logger.info(f"열린 채팅방 {len(rooms)}개 수집: {rooms}")
        return rooms

    # ===== 창 핸들 조회 =====

    def get_main_hwnd(self) -> Optional[int]:
        """카카오톡 메인 창 핸들을 반환한다. 없으면 None."""
        hwnd = win32gui.FindWindow(None, _KAKAO_MAIN_TITLE)
        return hwnd if hwnd else None

    def find_room_hwnd(self, room_name: str) -> Optional[int]:
        """이름이 일치하는 채팅방 창 핸들을 반환한다. 없으면 None."""
        hwnd = win32gui.FindWindow(None, room_name)
        return hwnd if hwnd else None

    # ===== 창 활성화 =====

    def _activate(self, hwnd: int, delay: float = 0.4) -> None:
        """창을 복원하고 포그라운드로 가져온다."""
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(delay)

    # ===== 채팅방 열기 =====

    def open_room(self, room_name: str) -> bool:
        """채팅방을 카카오톡에서 연다.

        동작 순서:
        1. 이미 열린 창이 있으면 활성화하고 종료
        2. 없으면 채팅 목록 내 검색으로 열기 (Ctrl+F 통합 검색은 "친구 추가하기" 오작동 문제 있음)

        Returns:
            True: 성공 / False: 실패
        """
        if not self.is_running():
            raise KakaoNotRunningError("카카오톡이 실행 중이지 않습니다.")

        # 1. 이미 열린 창이 있으면 활성화
        hwnd = self.find_room_hwnd(room_name)
        if hwnd:
            self._activate(hwnd)
            logger.info(f"이미 열려 있음, 활성화: {room_name}")
            return True

        # 2. 카카오톡 메인 창 활성화
        main_hwnd = self.get_main_hwnd()
        if not main_hwnd:
            logger.error("카카오톡 메인 창을 찾을 수 없습니다.")
            return False

        # 3. 클립보드 준비
        pyperclip.copy(room_name)

        # 4. KakaoTalk 활성화
        self._activate(main_hwnd, delay=0.8)

        # 5. 검색창 열기 → 방 이름 입력 → Enter
        if not self._search_open:
            # 첫 번째 방: Ctrl+F로 검색창 열기 (빈 상태)
            # ※ 빈 검색창에 Ctrl+A를 누르면 친구 추가하기가 발동하므로 사용 금지
            pyautogui.hotkey("ctrl", "f")
            time.sleep(1.2)
            self._search_open = True
        else:
            # 두 번째 방부터: Ctrl+F 재열기 → End → Backspace×40 으로 이전 텍스트 삭제
            # ※ Ctrl+A는 항상 친구추가하기 발동, Shift+Home 동작 안 함
            pyautogui.hotkey("ctrl", "f")
            time.sleep(1.0)
            pyautogui.press("end")
            time.sleep(0.1)
            pyautogui.press("backspace", presses=40, interval=0.02)
            time.sleep(0.2)

        pyautogui.hotkey("ctrl", "v")
        time.sleep(1.2)
        pyautogui.press("enter")
        time.sleep(1.0)

        # 5. 창이 열렸는지 확인
        hwnd = self.find_room_hwnd(room_name)
        if hwnd:
            logger.info(f"채팅방 열기 성공: {room_name}")
            return True

        logger.warning(f"채팅방 창을 확인할 수 없음 (열렸을 수도 있음): {room_name}")
        return True
