# 메시지 전송 모듈
# 카카오톡 채팅방 창을 활성화한 뒤 클립보드 붙여넣기로 메시지를 전송한다.
# 한글, 줄바꿈, 이모지를 모두 안전하게 처리하기 위해 pyautogui.typewrite 대신
# pyperclip + Ctrl+V 방식을 사용한다.

import logging
import time

import pyautogui
import pyperclip
import win32con
import win32gui

logger = logging.getLogger(__name__)

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.0


class MessageSendError(Exception):
    """메시지 전송 실패 예외"""


class MessageSender:
    """카카오톡 채팅방에 메시지 하나를 전송하는 클래스"""

    def send_message(self, room_name: str, text: str) -> bool:
        """채팅방에 메시지 하나를 전송한다.

        동작 순서:
        1. 채팅방 창 핸들 획득
        2. 창 활성화 (포그라운드)
        3. 입력창 클릭
        4. 클립보드에 텍스트 복사 후 Ctrl+V 붙여넣기
        5. Enter 전송

        Returns:
            True: 전송 성공 / False: 채팅방 창 없음
        """
        hwnd = win32gui.FindWindow(None, room_name)
        if not hwnd:
            logger.warning(f"채팅방 창을 찾을 수 없음: {room_name}")
            return False

        try:
            self._activate_window(hwnd)
            self._click_input_area(hwnd)
            self._paste_and_send(text)
            logger.debug(f"전송 완료 [{room_name}]: {text[:40]!r}")
            return True

        except Exception as e:
            logger.error(f"전송 오류 [{room_name}]: {e}")
            return False

    # ===== 내부 구현 =====

    def _activate_window(self, hwnd: int) -> None:
        """창을 복원하고 포그라운드로 가져온다."""
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.4)

    def _click_input_area(self, hwnd: int) -> None:
        """채팅창 하단 입력 영역을 클릭해 포커스를 맞춘다.

        KakaoTalk PC 입력창은 창 높이의 약 88% 지점에 위치한다.
        """
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        click_x = (left + right) // 2
        click_y = top + int((bottom - top) * 0.88)
        pyautogui.click(click_x, click_y)
        time.sleep(0.25)

    def _paste_and_send(self, text: str) -> None:
        """텍스트를 클립보드로 붙여넣고 Enter로 전송한다."""
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "a")   # 기존 입력 텍스트 전체 선택
        time.sleep(0.08)
        pyautogui.hotkey("ctrl", "v")   # 붙여넣기
        time.sleep(0.15)
        pyautogui.press("enter")        # 전송
