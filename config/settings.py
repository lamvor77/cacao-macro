# 프로그램 전체에서 사용하는 상수 정의
# 하드코딩을 방지하고 유지보수성을 높이기 위해 모든 설정값을 여기에 모은다.

import logging
import os
from typing import Final

logger = logging.getLogger(__name__)

# python-dotenv가 설치되어 있으면 .env를 읽어 os.environ에 반영한다 — 아래
# LICENSE_ADMIN_PASSWORD/LICENSE_SECRET_KEY가 os.getenv()로 읽히기 "전에"
# 반드시 호출되어야 하므로, 이 모듈 스스로 호출한다(config/cloud_settings.py의
# load_dotenv() 호출 순서에 의존하지 않는다 — 이 파일이 더 먼저 import되는
# 경우가 많다: main.py가 가장 먼저 config.settings를 가져온다).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    logger.debug("python-dotenv가 설치되어 있지 않습니다 — 시스템 환경변수만 사용합니다.")

# ===== 윈도우 설정 =====
WINDOW_TITLE: Final[str] = "카카오톡 자동 메시지 전송"
WINDOW_DEFAULT_WIDTH: Final[int] = 1200
WINDOW_DEFAULT_HEIGHT: Final[int] = 860
WINDOW_MIN_WIDTH: Final[int] = 1000
WINDOW_MIN_HEIGHT: Final[int] = 700

# ===== 탭 이름 =====
TAB_ACTIVE_ROOMS: Final[str] = "활성화된 카톡방 전송"
TAB_BULK_MESSAGE: Final[str] = "대량 메시지 전송"

# ===== 메시지 그룹 =====
# 그룹별 포함 메시지 번호(1~12)와 발송 분(minute) 정의
MESSAGE_COUNT: Final[int] = 12
MESSAGE_GROUPS: Final[dict] = {
    "A": {"label": "Group A", "messages": [1, 2, 3],   "minute": 0},
    "B": {"label": "Group B", "messages": [4, 5, 6],   "minute": 15},
    "C": {"label": "Group C", "messages": [7, 8, 9],   "minute": 30},
    "D": {"label": "Group D", "messages": [10, 11, 12], "minute": 45},
}

# ===== 운영 시간 =====
OPERATION_START_HOUR: Final[int] = 8   # 08:00 부터
OPERATION_END_HOUR: Final[int] = 19    # 19:00 까지

# ===== 딜레이 설정 (초) =====
MESSAGE_DELAY_SECONDS: Final[float] = 2.0      # 메시지 간 대기
ROOM_DELAY_MIN: Final[float] = 0.5             # 단톡방 이동 최소 대기
ROOM_DELAY_MAX: Final[float] = 1.5             # 단톡방 이동 최대 대기

# ===== 저장 파일 경로 =====
SAVE_FILE_PATH: Final[str] = "storage/data.json"

# ===== 중복 실행 방지 포트 =====
LOCK_PORT: Final[int] = 47832

# ===== 라이선스 관리자 설정 (Phase 3-2: 환경변수 기반, fail-closed) =====
# 더 이상 소스코드에 하드코딩하지 않는다 — LICENSE_ADMIN_PASSWORD/LICENSE_SECRET_KEY를
# .env(빌드/배포 담당자 로컬 환경에만 존재)에서 읽는다. 값이 비어있거나
# "CHANGE_ME_"로 시작하는 플레이스홀더면 core/license_manager.py가 해당 기능
# (관리자 인증/라이선스 발급·검증)을 즉시 차단한다(안전 실패 — 기본 비밀번호나
# 예제 키로 동작하지 않는다). 실제 값은 .env.example에 넣지 않는다.
#
# 배포 시 주의: check_build_license()는 frozen(exe) 모드에서 이 값을 그대로
# 재사용해 서명을 재계산한다(core/license_manager.py 참고) — 배포되는 exe와
# 같은 폴더에 LICENSE_SECRET_KEY만 담은 .env를 함께 배치해야 한다(LICENSE_
# ADMIN_PASSWORD/LICENSE_ADMIN_UI_ENABLED는 최종 사용자 배포본에 넣지 않는다 —
# 관리자 전용 값이기 때문).
LICENSE_ADMIN_PASSWORD: Final[str] = os.getenv("LICENSE_ADMIN_PASSWORD", "").strip()  # 관리자 모드 비밀번호
LICENSE_SECRET_KEY: Final[str] = os.getenv("LICENSE_SECRET_KEY", "").strip()          # HMAC 서명 비밀키
LICENSE_ADMIN_UI_ENABLED: Final[bool] = os.getenv("LICENSE_ADMIN_UI_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
LICENSE_BUILD_FILE_NAME: Final[str] = "license_build.json"  # 프로젝트 루트에 생성되어 빌드 시 exe에 포함됨

# ===== UI 치수 =====
ROOM_LIST_PANEL_WIDTH: Final[int] = 270     # 단톡방 목록 패널 폭 (고정)
LOG_PANEL_HEIGHT: Final[int] = 195          # 로그 패널 높이 (고정)
BOTTOM_BAR_HEIGHT: Final[int] = 62         # 시작/종료 버튼 바 높이 (고정)
BUTTON_HEIGHT: Final[int] = 38             # 버튼 높이
MESSAGE_INPUT_HEIGHT: Final[int] = 108     # 메시지 입력 텍스트박스 높이 (기본 72의 1.5배)

# ===== 폰트 패밀리 =====
FONT_FAMILY: Final[str] = "맑은 고딕"
FONT_FAMILY_MONO: Final[str] = "Consolas"

# ===== 로그 설정 =====
LOG_MAX_LINES: Final[int] = 1000  # 로그 최대 줄 수 (초과 시 오래된 줄 삭제)
