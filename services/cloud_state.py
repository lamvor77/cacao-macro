# 클라우드 동기화 상태 모델
#
# CloudSyncCoordinator가 UI에 알려줄 현재 상태를 표현한다. 내부 로직은 항상
# CloudState(enum) 값으로 판단하고, 화면에 보여줄 한국어 문구는 이 파일의
# 매핑 테이블 하나로만 관리한다 — 문구를 바꿀 때 로직을 건드릴 필요가 없다.

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CloudState(Enum):
    """클라우드 동기화가 있을 수 있는 상태.

    Phase 2C의 8개 상태는 그대로 유지했다(기존 호환성) — Phase 2D에서
    app_users 승인 상태/역할을 반영하기 위해 3개를 추가했다.
    """

    NOT_CONFIGURED = "not_configured"  # SUPABASE_ENABLED=false 등 미설정
    LOGIN_REQUIRED = "login_required"  # 설정은 되어 있으나 로그인 세션이 없음
    CONNECTING = "connecting"          # 초기 연결/동기화 시도 중
    SYNCING = "syncing"                # pull/push 네트워크 요청 진행 중
    CONNECTED = "connected"            # 마지막 동기화 성공(editor/admin), 유휴 상태
    OFFLINE = "offline"                # 네트워크/연결 문제로 실패
    SYNC_FAILED = "sync_failed"        # 연결 외 이유로 동기화 실패
    CONFLICT = "conflict"              # 버전 충돌 감지, 사용자 확인 필요

    # ===== Phase 2D: app_users 상태 반영 =====
    APPROVAL_PENDING = "approval_pending"      # 로그인은 됐지만 관리자 승인 대기(app_users.status='pending')
    BLOCKED = "blocked"                        # 관리자가 차단함(app_users.status='blocked')
    CONNECTED_READ_ONLY = "connected_read_only"  # approved + viewer — 읽기는 되지만 업로드는 하지 않음


CLOUD_STATE_LABELS_KO: dict[CloudState, str] = {
    CloudState.NOT_CONFIGURED: "클라우드 미설정",
    CloudState.LOGIN_REQUIRED: "로그인 필요",
    CloudState.CONNECTING: "연결 중",
    CloudState.SYNCING: "동기화 중",
    CloudState.CONNECTED: "동기화 완료",
    CloudState.OFFLINE: "오프라인",
    CloudState.SYNC_FAILED: "동기화 실패",
    CloudState.CONFLICT: "충돌 확인 필요",
    CloudState.APPROVAL_PENDING: "관리자 승인 대기",
    CloudState.BLOCKED: "접근 차단됨",
    CloudState.CONNECTED_READ_ONLY: "읽기 전용",
}


@dataclass
class CloudStatusInfo:
    """현재 상태 + 부가 설명(로그/디버깅용, UI 라벨에는 노출하지 않음).

    pending_count/conflict_count: Phase 2E 후속 — 새 팝업/새 상태를 만들지
    않고 기존 CONNECTED/CONFLICT 라벨에 건수만 덧붙이기 위한 값이다("로컬
    저장됨 · 클라우드 대기 N건" / "충돌 확인 필요 N건"). 0이면 라벨에 아무
    것도 덧붙지 않는다(기존 문구 그대로).
    """

    state: CloudState
    detail: Optional[str] = None
    pending_count: int = 0
    conflict_count: int = 0

    @property
    def label_ko(self) -> str:
        base = CLOUD_STATE_LABELS_KO.get(self.state, self.state.value)
        if self.state == CloudState.CONFLICT and self.conflict_count > 0:
            return f"{base} {self.conflict_count}건"
        if self.state == CloudState.CONNECTED and self.pending_count > 0:
            return f"{base} · 클라우드 대기 {self.pending_count}건"
        return base
