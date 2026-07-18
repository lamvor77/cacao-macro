# 프로그램 전체에서 사용하는 상수 정의
# 하드코딩을 방지하고 유지보수성을 높이기 위해 모든 설정값을 여기에 모은다.

from typing import Final

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

# ===== 라이선스 설정 =====
# WARNING: 배포용 빌드 전 반드시 아래 두 값을 실제 운영 값으로 변경할 것.
#    (PyInstaller로 컴파일해도 리버스 엔지니어링으로 추출 가능한 값이므로,
#     이 값 자체를 강력한 보안 경계로 취급하지 말 것 - 캐주얼한 변조 방지 목적)
# 사용 기간은 PC 잠금 없이, 관리자 모드에서 발급한 license_build.json을
# 빌드 시점에 exe 안에 포함시키는 방식으로 적용된다 (core/license_manager.py 참고).
LICENSE_SECRET_KEY: Final[str] = "CHANGE_ME_BEFORE_DISTRIBUTING_RANDOM_STRING"  # HMAC 서명 비밀키
ADMIN_PASSWORD: Final[str] = "CHANGE_ME_ADMIN_PASSWORD"                         # 관리자 모드 비밀번호
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
