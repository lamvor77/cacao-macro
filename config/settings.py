# 프로그램 전체에서 사용하는 상수 정의
# 하드코딩을 방지하고 유지보수성을 높이기 위해 모든 설정값을 여기에 모은다.

import logging
import os
import sys
from typing import Final

logger = logging.getLogger(__name__)


def get_runtime_base_dir() -> str:
    """.env/license_build.json을 찾을 기준 디렉터리 — 실행 위치(현재 작업
    디렉터리)에 의존하지 않는다.

    - frozen(PyInstaller exe) 모드: exe 파일이 있는 폴더(sys.executable의 부모).
      바탕화면 바로가기, 다른 폴더에서 실행 등 CWD가 무엇이든 항상 동일하다.
    - 개발 모드: 프로젝트 루트(이 파일의 두 단계 위 디렉터리).

    core/license_manager.py도 라이선스 파일 경로를 계산할 때 이 함수를 그대로
    재사용한다(License Externalization Sprint) — .env와 license_build.json이
    항상 "같은 기준 디렉터리"를 쓰도록 통일하기 위함이다. 이 함수를
    config/settings.py에 두는 이유는 순환 임포트 방지다: config.settings는
    가장 먼저 import되는 모듈이라(주석 아래 참고) 자기 자신의 load_dotenv()
    호출에도 이 함수가 필요하고, core.license_manager는 반대로 config.settings의
    상수(LICENSE_SECRET_KEY 등)를 이미 가져다 쓰고 있으므로, 이 함수가
    core.license_manager에 있었다면 config.settings → core.license_manager →
    config.settings로 순환 임포트가 생겼을 것이다.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# python-dotenv가 설치되어 있으면 .env를 읽어 os.environ에 반영한다 — 아래
# LICENSE_ADMIN_PASSWORD/LICENSE_SECRET_KEY가 os.getenv()로 읽히기 "전에"
# 반드시 호출되어야 하므로, 이 모듈 스스로 호출한다(config/cloud_settings.py의
# load_dotenv() 호출 순서에 의존하지 않는다 — 이 파일이 더 먼저 import되는
# 경우가 많다: main.py가 가장 먼저 config.settings를 가져온다).
#
# get_runtime_base_dir() 기준 경로를 명시적으로 넘긴다 — 인자 없이 호출하면
# python-dotenv가 frozen 모드에서 os.getcwd()를 기준으로 찾는데(설치된
# dotenv.main.find_dotenv() 소스로 확인함), 이는 exe를 실행한 위치에 따라
# .env 로딩 여부가 달라지는 문제가 있다. exe 경로 기준으로 고정하면 항상
# 예측 가능하게 동작한다. override=False(기본값과 동일하지만 명시)로 이미
# 설정된 OS 환경변수를 덮어쓰지 않는다.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(get_runtime_base_dir(), ".env"), override=False)
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
# 참고: "누가 쓸 수 있는가"(사용 권한)는 Supabase 계정 인증/승인 상태로
# 관리한다(services/auth_service.py) — LICENSE_SECRET_KEY는 그 판단에 쓰이지
# 않는다. 다만 main.py 시작 시 core/license_manager.py::verify_build_signature()가
# "이 exe가 관리자가 발급한 빌드가 맞는지"(license_build.json 서명 재계산)를
# 검증하고 실패하면 실행을 막으므로, LICENSE_SECRET_KEY는 exe와 같은 폴더의
# .env에 반드시 배치해야 한다(값이 없으면 서명을 재계산할 수 없어 검증
# 실패로 처리됨). LICENSE_ADMIN_PASSWORD/LICENSE_ADMIN_UI_ENABLED는 관리자가
# 라이선스를 발급할 때만 필요한 값이므로 최종 사용자 배포본에는 넣지 않는다.
LICENSE_ADMIN_PASSWORD: Final[str] = os.getenv("LICENSE_ADMIN_PASSWORD", "").strip()  # 관리자 모드 비밀번호
LICENSE_SECRET_KEY: Final[str] = os.getenv("LICENSE_SECRET_KEY", "").strip()          # HMAC 서명 비밀키
LICENSE_ADMIN_UI_ENABLED: Final[bool] = os.getenv("LICENSE_ADMIN_UI_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
# License Externalization Sprint: 더 이상 PyInstaller datas로 exe 내부에 포함되지
# 않는다 — exe(또는 개발 스크립트)와 같은 폴더에 두는 외부 파일이다
# (core.license_manager.get_external_license_path() 참고). 파일명 자체는 그대로다.
LICENSE_BUILD_FILE_NAME: Final[str] = "license_build.json"

# ===== 환경 구분 (Test Environment Deployment & E2E Validation Sprint) =====
# 운영 Supabase 프로젝트와 테스트 프로젝트를 화면에서 명확히 구분하기 위한
# 표시 플래그. .env의 APP_ENV 또는 SUPABASE_ENVIRONMENT 중 하나라도 "test"이면
# 테스트 환경으로 간주한다(운영자가 실수로 아무 표시 없이 테스트 프로젝트에
# 연결한 채로 프로그램을 켜는 사고를 막는 것이 목적이므로 관대하게 인식한다).
# 값이 없으면(둘 다 빈 문자열) 운영 환경으로 취급하며, 이 경우 화면에 아무
# 표시도 하지 않는다 — 기존 운영 배포본은 동작이 전혀 바뀌지 않는다.
APP_ENV: Final[str] = os.getenv("APP_ENV", "").strip().lower()
SUPABASE_ENVIRONMENT: Final[str] = os.getenv("SUPABASE_ENVIRONMENT", "").strip().lower()
IS_TEST_ENVIRONMENT: Final[bool] = APP_ENV == "test" or SUPABASE_ENVIRONMENT == "test"

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
