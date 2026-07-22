# 자동 발송 스케줄러
# 08:00~19:00 운영 시간에 매시간 0/15/30/45분에 그룹 메시지를 자동 발송한다.
#
# 핵심 발송 순서 (CLAUDE.md 규칙 준수):
#   단톡방1 → 메시지1 → 2초 → 메시지2 → 2초 → 메시지3
#   → 단톡방 이동 딜레이(0.5~1.5초) →
#   단톡방2 → 메시지1 → 2초 → ...

import logging
import random
import threading
import time
from datetime import datetime
from typing import Callable, Optional

import psutil

from config.settings import (
    MESSAGE_DELAY_SECONDS,
    MESSAGE_GROUPS,
    OPERATION_END_HOUR,
    OPERATION_START_HOUR,
    ROOM_DELAY_MAX,
    ROOM_DELAY_MIN,
)
from core.message_sender import MessageSender
from core.send_verification import SendMessageVerificationResult

logger = logging.getLogger(__name__)

_KAKAO_EXE = "KakaoTalk.exe"


def _is_kakao_running() -> bool:
    """카카오톡 프로세스가 실행 중인지 빠르게 확인한다."""
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == _KAKAO_EXE.lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


class AutoScheduler:
    """자동 발송 스케줄러"""

    def __init__(
        self,
        get_rooms_fn: Callable[[], dict[str, bool]],
        get_messages_fn: Callable[[], dict[int, str]],
        log_fn: Callable[[str], None],
        verify_message_fn: Optional[Callable[[int, str, int], SendMessageVerificationResult]] = None,
        get_local_revision_fn: Optional[Callable[[int], int]] = None,
    ):
        self._get_rooms = get_rooms_fn
        self._get_messages = get_messages_fn
        self._log = log_fn
        # Production Stabilization Sprint(요구사항 4절) — 그룹 발송 시작 직전, 그
        # 그룹에 실제로 필요한 message_no 각각에 대해 개별 조회한다(전체 12건을
        # 매번 조회하지 않음). 생략하면(None) 기존과 동일하게 검증 없이
        # get_messages_fn() 스냅샷만 사용한다(레거시 동작 완전 보존, 요구사항 12).
        # 시그니처: verify_message_fn(message_no, local_content, local_revision)
        #   -> core.send_verification.SendMessageVerificationResult
        # 호출부(gui/main_window.py)가 실패해도 항상 결과 객체를 반환하도록
        # 설계되어 있다 — 이 클래스는 정책(block/cached) 판단을 하지 않고 결과의
        # allowed/content만 그대로 따른다(정책 자체는 verify_message_fn 내부 책임).
        self._verify_message = verify_message_fn
        self._get_local_revision = get_local_revision_fn or (lambda n: 0)
        self._sender = MessageSender()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_sent_id: Optional[tuple] = None

        # 그룹 발송이 진행 중인 동안 True — 클라우드 변경 알림(Phase 2B에서 연동)이
        # 도착했을 때 "지금 발송 중인 그룹에는 적용되지 않는다"는 로그를 위해 사용한다.
        self._group_in_progress = False

    # ===== 공개 메서드 =====

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_sent_id = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._log(f"[INFO] 자동 발송 시작 — 다음 발송: {self._next_send_info()}")

    def stop(self) -> None:
        self._running = False
        self._log("[INFO] 자동 발송 중지")

    def is_running(self) -> bool:
        return self._running

    def notify_cloud_update(self) -> None:
        """클라우드에서 메시지 변경 사항을 수신했을 때 호출한다 (Phase 2B 연동 예정).

        _send_group()은 그룹 시작 시점에 메시지 스냅샷을 뜬 뒤 그 그룹이 끝날
        때까지 스냅샷만 사용하므로(아래 _send_group 참고), 그룹 발송 도중
        도착한 변경 사항은 현재 진행 중인 발송에 전혀 영향을 주지 않고
        다음 그룹부터 자동으로 반영된다 — 다음 그룹 시작 시 get_messages_fn()을
        다시 호출해 새 스냅샷을 뜨기 때문이다. 이 메서드는 그 사실을
        로그로 남기는 역할만 하며, 스냅샷 로직 자체를 바꾸지 않는다.
        """
        if self._group_in_progress:
            self._log("[INFO] 클라우드 수정사항 수신 — 현재 그룹 종료 후 다음 발송부터 적용")
        else:
            self._log("[INFO] 클라우드 수정사항 수신 — 다음 발송부터 적용")

    # ===== 스케줄 루프 =====

    def _loop(self) -> None:
        """1초 간격으로 발송 시간을 확인하는 메인 루프"""
        while self._running:
            try:
                self._tick()
            except Exception as e:
                logger.exception("스케줄 루프 예외")
                self._log(f"[오류] 스케줄 루프 오류: {e}")
            time.sleep(1)

    def _tick(self) -> None:
        """현재 시간이 발송 시간이면 해당 그룹을 발송한다."""
        now = datetime.now()
        hour, minute = now.hour, now.minute

        if not (OPERATION_START_HOUR <= hour < OPERATION_END_HOUR):
            return

        group_key = next(
            (k for k, v in MESSAGE_GROUPS.items() if v["minute"] == minute),
            None,
        )
        if group_key is None:
            return

        send_id = (hour, minute)
        if send_id == self._last_sent_id:
            return

        self._last_sent_id = send_id
        self._send_group(group_key, MESSAGE_GROUPS[group_key])

    # ===== 그룹 발송 =====

    def _send_group(self, group_key: str, group_info: dict) -> None:
        """그룹에 포함된 메시지를 모든 체크된 단톡방에 순차 발송한다."""
        now = datetime.now()
        self._log(f"[{now.strftime('%H:%M')}] Group {group_key} 발송 시작")

        # 카카오톡 실행 확인
        if not _is_kakao_running():
            self._log("[오류] 카카오톡이 실행 중이지 않습니다. 발송을 건너뜁니다.")
            return

        rooms = self._get_rooms()
        messages = self._get_messages()

        checked_rooms = [name for name, checked in rooms.items() if checked]
        if not checked_rooms:
            self._log("[경고] 발송 대상 단톡방이 없습니다.")
            return

        # Production Stabilization Sprint(요구사항 4절) — 그룹 시작 시점 스냅샷을 뜨기
        # "직전"에 이 그룹이 실제로 쓸 message_no만, 한 건씩 서버 최신 상태를
        # 확인한다(전체 12건 조회 아님). verify_message_fn이 없으면(None) 완전히
        # 기존과 동일하게 검증 없이 진행한다(레거시 동작 보존, 요구사항 12).
        #
        # 한 message_no의 검증 실패(block 정책)는 "그 메시지 하나만" 이번 발송에서
        # 제외한다 — 그룹 전체를 취소하거나 스케줄러를 중단하지 않는다(완료 기준 7).
        verified_contents: dict[int, str] = {}
        for num in group_info["messages"]:
            local_content = messages.get(num, "")
            if self._verify_message is not None:
                try:
                    result = self._verify_message(num, local_content, self._get_local_revision(num))
                except Exception as e:
                    logger.exception(f"발송 직전 검증 훅 자체 오류(message_no={num}) — 이 메시지는 제외하고 계속 진행")
                    self._log(f"[오류] 발송 직전 검증 훅 오류로 메시지 제외 — message_no={num}: {type(e).__name__}")
                    continue
                if not result.allowed:
                    self._log(
                        f"[경고] 발송 보류(서버 확인 실패, block 정책) — message_no={num}, "
                        f"오류={result.error_code.value if result.error_code else '알 수 없음'}"
                    )
                    continue
                if result.used_cached_content:
                    self._log(f"[경고] 캐시 내용으로 발송(cached 정책) — message_no={num}")
                verified_contents[num] = result.content
            else:
                verified_contents[num] = local_content

        # ===== 메시지 스냅샷 =====
        # 검증(또는 검증 생략)을 마친 시점의 메시지를 불변 튜플로 고정한다. 아래 try
        # 블록이 끝날 때까지(=이 그룹의 모든 단톡방 발송이 끝날 때까지) 이 스냅샷만
        # 사용하며 get_messages_fn()을 다시 호출하지 않는다 — 그룹 발송 도중
        # UI나 클라우드에서 메시지가 바뀌어도 현재 발송에는 영향이 없고,
        # 다음 그룹(또는 다음 동일 그룹) 발송부터 최신 메시지가 적용된다.
        # (클라우드 변경 도착 시의 로그 안내는 notify_cloud_update() 참고)
        group_messages: tuple[tuple[int, str], ...] = tuple(
            (num, verified_contents[num])
            for num in group_info["messages"]
            if verified_contents.get(num, "").strip()
        )
        if not group_messages:
            self._log(f"[경고] Group {group_key} 메시지가 모두 비어 있거나 검증에서 제외되었습니다.")
            return

        fail_count = 0  # 연속 실패 단톡방 카운트

        self._group_in_progress = True
        try:
            # ===== 단톡방 기준 순차 발송 =====
            for room_idx, room_name in enumerate(checked_rooms):
                if not self._running:
                    self._log("[INFO] 중지 요청으로 발송을 멈춥니다.")
                    return

                # 발송 중 카카오톡 종료 감지 (3회 이상 연속 실패 시)
                if fail_count >= 3:
                    self._log("[오류] 카카오톡이 종료된 것으로 보입니다. 발송을 중단합니다.")
                    return

                self._log(f"  {room_name}")
                room_failed = False

                for msg_idx, (msg_num, msg_text) in enumerate(group_messages):
                    if not self._running:
                        return

                    ok = self._sender.send_message(room_name, msg_text)

                    if ok:
                        self._log(f"    메시지{msg_num} 완료")
                        fail_count = 0  # 성공 시 실패 카운트 초기화
                    else:
                        self._log(f"    메시지{msg_num} 실패 — 채팅방 창을 찾을 수 없음")
                        room_failed = True
                        break

                    # 메시지 간 2초 대기 (마지막 메시지 제외)
                    if msg_idx < len(group_messages) - 1:
                        time.sleep(MESSAGE_DELAY_SECONDS)

                if room_failed:
                    fail_count += 1

                # 단톡방 이동 랜덤 딜레이 (마지막 방 제외)
                if room_idx < len(checked_rooms) - 1 and self._running:
                    delay = round(random.uniform(ROOM_DELAY_MIN, ROOM_DELAY_MAX), 2)
                    time.sleep(delay)

            self._log(f"[{datetime.now().strftime('%H:%M')}] Group {group_key} 완료")
            self._log(f"[INFO] 다음 발송: {self._next_send_info()}")
        finally:
            self._group_in_progress = False

    # ===== 유틸리티 =====

    def _next_send_info(self) -> str:
        """다음 발송 예정 시간과 그룹을 반환한다 (운영시간 기준)."""
        now = datetime.now()
        hour, minute = now.hour, now.minute

        schedule_minutes = sorted(v["minute"] for v in MESSAGE_GROUPS.values())  # [0, 15, 30, 45]

        # 운영 시간 이전 (오늘 08:00이 첫 발송)
        if hour < OPERATION_START_HOUR:
            return f"오늘 {OPERATION_START_HOUR:02d}:00 Group A"

        # 운영 시간 이후 (내일 08:00이 첫 발송)
        if hour >= OPERATION_END_HOUR:
            return f"내일 {OPERATION_START_HOUR:02d}:00 Group A"

        # 운영 시간 내: 현재 시간 이후의 다음 발송 시각 탐색
        future = [m for m in schedule_minutes if m > minute]
        if future:
            next_m, next_h = future[0], hour
        else:
            next_m = schedule_minutes[0]
            next_h = hour + 1
            # 다음 시간이 운영시간을 넘으면 내일 08:00
            if next_h >= OPERATION_END_HOUR:
                return f"내일 {OPERATION_START_HOUR:02d}:00 Group A"

        group = next(
            (k for k, v in MESSAGE_GROUPS.items() if v["minute"] == next_m), "?"
        )
        return f"{next_h:02d}:{next_m:02d} Group {group}"
