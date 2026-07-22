# PC 쪽 shared_messages 동기화 상태 기계 — Tkinter/네트워크에 전혀 의존하지 않는
# 순수 로직 모듈이다(gui/panels/admin_ui_state.py와 동일한 설계 원칙: 판단 로직은
# 위젯 없이도 정확성을 보장해야 테스트할 수 있다).
#
# 이 모듈이 하는 일:
#   - 메시지 1~12 각각의 동기화 상태(SYNCED/SAVING/OFFLINE_PENDING/CONFLICT/
#     RECONNECTING/REMOTE_UPDATED)를 관리한다.
#   - Realtime 이벤트의 revision이 로컬보다 높을 때만 반영한다(요구사항 6절 —
#     낮거나 같은 revision은 무시, 자신이 낸 이벤트의 에코도 이 규칙으로
#     자연스럽게 걸러진다: 저장 성공 시 로컬 revision을 먼저 올려두므로 에코의
#     revision은 "같음"이 되어 무시된다).
#   - 사용자가 편집 중인 메시지에 원격 변경이 들어오면 즉시 텍스트를 덮어쓰지
#     않고 pending_remote로 보류한다(요구사항 10절).
#   - gui/panels/control_panel.py는 이 모듈의 결과를 받아 위젯에 반영하기만 한다.
#
# 이 모듈은 services/shared_message_service.py나 Supabase를 import하지 않는다 —
# 호출부(gui/main_window.py)가 서비스 계층과 이 모듈을 연결한다.

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

MIN_MESSAGE_NO = 1
MAX_MESSAGE_NO = 12


class MessageSyncStatus(Enum):
    SYNCED = "synced"                  # 동기화됨
    SAVING = "saving"                  # 저장 중
    OFFLINE_PENDING = "offline_pending"  # 오프라인 변경(서버 미반영)
    CONFLICT = "conflict"              # 충돌
    RECONNECTING = "reconnecting"      # 재연결 중
    REMOTE_UPDATED = "remote_updated"  # 다른 사용자가 변경(편집 중이라 보류됨)


STATUS_LABELS_KO: dict[MessageSyncStatus, str] = {
    MessageSyncStatus.SYNCED: "동기화됨",
    MessageSyncStatus.SAVING: "저장 중",
    MessageSyncStatus.OFFLINE_PENDING: "오프라인 변경",
    MessageSyncStatus.CONFLICT: "충돌",
    MessageSyncStatus.RECONNECTING: "재연결 중",
    MessageSyncStatus.REMOTE_UPDATED: "서버 변경 수신",
}


@dataclass
class RemoteMessageSnapshot:
    """Realtime 이벤트 또는 전체 재조회(list_messages) 결과에서 얻은 서버 측 값.

    services.shared_message_service.SharedMessageRecord /
    services.realtime_message_sync_service.MessageChangeEvent 둘 다 이 형태로
    변환해서 넘긴다(이 모듈이 두 서비스 모듈을 직접 import하지 않기 위함).
    """

    message_no: int
    content: str
    revision: int
    title: Optional[str] = None
    updated_by_name: str = ""
    updated_at: str = ""
    update_source: str = ""


@dataclass
class MessageSyncState:
    message_no: int
    content: str = ""
    title: Optional[str] = None
    revision: int = 0
    base_revision: Optional[int] = None
    is_editing: bool = False
    status: MessageSyncStatus = MessageSyncStatus.SYNCED
    updated_by_name: str = ""
    updated_at: str = ""
    pending_remote: Optional[RemoteMessageSnapshot] = None

    @property
    def status_label_ko(self) -> str:
        return STATUS_LABELS_KO.get(self.status, self.status.value)


def should_apply_remote_event(local_revision: int, event_revision: int) -> bool:
    """이벤트의 revision이 로컬보다 클 때만 True — 요구사항 6절의 핵심 규칙.

    동일하거나 낮으면(자신의 에코 포함) False다.
    """
    return event_revision > local_revision


class SharedMessageCoordinator:
    """MainWindow가 소유하는 12개 MessageSyncState의 컨테이너 + 상태 전이 로직."""

    def __init__(self):
        self._states: dict[int, MessageSyncState] = {
            n: MessageSyncState(message_no=n) for n in range(MIN_MESSAGE_NO, MAX_MESSAGE_NO + 1)
        }

    def get_state(self, message_no: int) -> MessageSyncState:
        return self._states[message_no]

    def all_states(self) -> list:
        return [self._states[n] for n in range(MIN_MESSAGE_NO, MAX_MESSAGE_NO + 1)]

    # ===== 초기 로드 / 수동 새로고침 / 재연결 직후 전체 재조회 =====

    def apply_full_snapshot(self, snapshots: list) -> list:
        """list_messages() 결과 전체를 반영한다(시작/수동 새로고침/재연결 직후 복구,
        요구사항 5/7/15절). 편집 중인 메시지는 즉시 덮어쓰지 않고 pending_remote로
        보류한다(요구사항 10절 — "편집하지 않는 메시지는 즉시 화면에 반영해도 된다"의
        반대 경우).

        Returns:
            실제로 화면 텍스트가 즉시 바뀐 message_no 목록(호출부가 위젯을
            갱신해야 하는 대상 — 편집 중이라 보류된 것은 제외).
        """
        applied: list[int] = []
        for snap in snapshots:
            state = self._states.get(snap.message_no)
            if state is None:
                continue
            if not should_apply_remote_event(state.revision, snap.revision) and state.revision != 0:
                # 로컬이 이미 이 revision을 알고 있거나 더 최신이면 건드리지 않는다.
                # 단, revision=0(아직 한 번도 안 받음)은 무조건 최초 반영한다.
                continue
            if state.is_editing:
                state.pending_remote = _snapshot_from(snap)
                state.status = MessageSyncStatus.REMOTE_UPDATED
                continue
            self._apply_snapshot_to_state(state, snap)
            applied.append(snap.message_no)
        return applied

    # ===== Realtime 단건 이벤트 =====

    def apply_remote_event(self, snap: RemoteMessageSnapshot) -> bool:
        """단건 Realtime 이벤트 반영. 반영해서 화면을 갱신해야 하면 True.

        편집 중이면 pending_remote에 보류하고 False를 반환한다(호출부는 대신
        REMOTE_UPDATED 배너를 표시해야 함을 True/False와 state.status로 판단한다).
        """
        state = self._states.get(snap.message_no)
        if state is None:
            return False
        if not should_apply_remote_event(state.revision, snap.revision):
            return False  # 자신의 에코 또는 오래된 이벤트 — 무시(요구사항 6절)

        if state.is_editing:
            state.pending_remote = snap
            state.status = MessageSyncStatus.REMOTE_UPDATED
            return False

        self._apply_snapshot_to_state(state, snap)
        return True

    def _apply_snapshot_to_state(self, state: MessageSyncState, snap) -> None:
        state.content = snap.content
        state.title = snap.title
        state.revision = snap.revision
        state.updated_by_name = snap.updated_by_name
        state.updated_at = snap.updated_at
        state.status = MessageSyncStatus.SYNCED
        state.pending_remote = None

    # ===== 편집 시작/종료 =====

    def begin_edit(self, message_no: int) -> None:
        state = self._states[message_no]
        state.is_editing = True
        state.base_revision = state.revision

    def discard_edit(self, message_no: int) -> None:
        """"취소" 버튼 — 편집을 완전히 포기하고 base_revision도 초기화한다.

        end_edit()과 달리 이후 자동저장이 이 메시지를 더 이상 "저장 대기 중"으로
        취급하지 않게 한다(단, 호출부가 텍스트 위젯 내용도 서버 값으로 되돌려야
        진짜 취소가 된다 — 이 메서드는 상태만 정리한다).
        """
        state = self._states[message_no]
        state.is_editing = False
        state.base_revision = None
        state.pending_remote = None
        if state.status not in (MessageSyncStatus.CONFLICT,):
            state.status = MessageSyncStatus.SYNCED

    def end_edit(self, message_no: int) -> None:
        """포커스가 빠져나갔음을 표시한다 — base_revision은 지우지 않는다.

        자동저장은 마지막 키 입력 후 2초 디바운스로 실행되므로(gui/main_window.py의
        기존 로컬 자동저장과 동일 트리거), 저장이 실제로 실행되는 시점에는 이미
        포커스가 다른 곳으로 옮겨가 있을 수 있다 — base_revision(편집 시작 시점의
        revision)은 저장이 성공(mark_saved)하거나 편집이 명시적으로 취소될 때까지
        유지되어야 저장 시 올바른 OCC 비교가 가능하다.
        """
        self._states[message_no].is_editing = False

    # ===== 편집 중 원격 변경 선택지 처리(요구사항 10절) =====

    def view_pending_remote(self, message_no: int) -> Optional[RemoteMessageSnapshot]:
        """"최신 내용 확인" 선택 — 보류된 원격 값을 조회만 한다(상태는 안 바꿈)."""
        return self._states[message_no].pending_remote

    def keep_local_and_discard_remote(self, message_no: int) -> None:
        """"현재 작성 내용 유지" 선택 — 보류된 원격 변경을 버리고 편집을 계속한다.

        주의: 로컬 base_revision은 그대로 두므로, 이후 저장 시 여전히 오래된
        base_revision으로 시도하게 되어 서버가 REVISION_CONFLICT로 거부한다 —
        즉 "무시하고 계속 편집"은 저장 시점에 다시 한 번 충돌 처리로 이어진다
        (조용한 덮어쓰기를 원천적으로 막는 설계, 요구사항 9/10절 공통 원칙).
        """
        state = self._states[message_no]
        state.pending_remote = None
        if state.is_editing:
            state.status = MessageSyncStatus.OFFLINE_PENDING

    def load_latest_and_discard_edit(self, message_no: int) -> Optional[RemoteMessageSnapshot]:
        """"작성 내용 복사 후 최신 버전 불러오기"에서 "최신 버전 불러오기" 부분 —
        보류된 원격 값을 현재 상태로 확정 반영하고 편집 상태를 종료한다.
        "작성 내용 복사"는 호출부(UI)가 반영 전에 현재 텍스트를 클립보드 등으로
        먼저 보관해야 한다(이 모듈은 클립보드를 다루지 않는다)."""
        state = self._states[message_no]
        pending = state.pending_remote
        if pending is None:
            return None
        self._apply_snapshot_to_state(state, pending)
        state.is_editing = False
        state.base_revision = None
        return pending

    # ===== 저장 시작/성공/충돌/실패 =====

    def mark_saving(self, message_no: int) -> None:
        self._states[message_no].status = MessageSyncStatus.SAVING

    def mark_saved(self, message_no: int, snap: RemoteMessageSnapshot) -> None:
        """저장 RPC 성공 — 로컬 revision을 새 값으로 즉시 올린다. 이렇게 해야 뒤이어
        도착하는 이 저장 자체의 Realtime 에코가 should_apply_remote_event()에서
        "같음"으로 걸러진다(요구사항 6절 "자신이 발생시킨 이벤트 재처리 방지")."""
        state = self._states[message_no]
        self._apply_snapshot_to_state(state, snap)
        state.is_editing = False
        state.base_revision = None

    def mark_conflict(self, message_no: int) -> None:
        self._states[message_no].status = MessageSyncStatus.CONFLICT

    def mark_offline_pending(self, message_no: int) -> None:
        self._states[message_no].status = MessageSyncStatus.OFFLINE_PENDING

    def mark_all_reconnecting(self) -> None:
        for state in self._states.values():
            if state.status != MessageSyncStatus.CONFLICT:
                state.status = MessageSyncStatus.RECONNECTING


def _snapshot_from(record) -> RemoteMessageSnapshot:
    """SharedMessageRecord(또는 동일한 속성을 가진 객체)를 RemoteMessageSnapshot으로 변환."""
    return RemoteMessageSnapshot(
        message_no=record.message_no,
        content=record.content,
        revision=record.revision,
        title=getattr(record, "title", None),
        updated_by_name=getattr(record, "updated_by_name", "") or "",
        updated_at=getattr(record, "updated_at", "") or "",
        update_source=getattr(record, "update_source", "") or "",
    )
