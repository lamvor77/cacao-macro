# 애플리케이션 버전 — 단일 진실 공급원(single source of truth).
#
# 창 제목, 진단정보 화면, 로그 시작 메시지, 릴리스 문서, release manifest가
# 전부 이 모듈만 참조한다 — 버전 문자열을 여러 파일에 중복 하드코딩하지 않는다.
#
# 값을 정하는 우선순위:
#   1. 빌드 환경변수(CACAO_MACRO_VERSION 등)에서 읽기 — 배포판 exe와 같은
#      폴더의 .env를 통해 주입한다(license_build.json/LICENSE_SECRET_KEY와
#      동일한 방식 — config/settings.py의 get_runtime_base_dir() 기준 .env
#      로딩에 이미 이 값들도 함께 실린다. 이 파일이 별도로 .env를 읽지는
#      않는다).
#   2. 환경변수가 없으면 안전한 기본값(APP_VERSION/BUILD_CHANNEL만 — 아래
#      "왜 BUILD_DATE는 하드코딩하지 않는가" 참고).
#   3. 개발 모드(python main.py 직접 실행, CACAO_MACRO_VERSION 미설정)에서는
#      "development"로 표시한다 — 정식 RC 번호를 사칭하지 않는다.
#
# 왜 BUILD_DATE/GIT_COMMIT은 하드코딩 기본값이 없는가: 이 두 값은 빌드마다
# 달라지는 값이라 "안전한 고정 기본값"이라는 개념 자체가 성립하지 않는다
# (오래된 값을 기본값으로 박아두면 오히려 오해를 유발한다). 비어 있으면
# 진단정보 화면에 "알 수 없음"으로 표시한다 — 릴리스 패키징 스크립트가
# 빌드 시점에 실제 값을 채워 dist/.env에 기록해야 한다(문서:
# docs/operations_guide.md의 "신규 버전 배포 절차" 참고).

import os

APP_NAME = "카카오톡 자동화"

# 환경변수가 전혀 없을 때만 쓰이는 안전한 기본값 — 이 스프린트(Release
# Candidate Sprint 1)의 RC 번호. 다음 버전을 낼 때는 이 두 값만 갱신하면
# 된다(문자열 하드코딩 지점이 이 한 곳뿐이라는 것이 이 모듈의 핵심 목적).
_FALLBACK_VERSION = "1.0.0-rc.1"
_FALLBACK_CHANNEL = "release-candidate"

_DEV_VERSION = "development"
_DEV_CHANNEL = "development"


def _is_frozen() -> bool:
    import sys
    return getattr(sys, "frozen", False)


def _resolve(env_name: str, frozen_fallback: str, dev_fallback: str) -> str:
    """우선순위: 환경변수 > (frozen이면) 안전한 기본값 > (개발 모드) dev 기본값."""
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    return frozen_fallback if _is_frozen() else dev_fallback


APP_VERSION: str = _resolve("CACAO_MACRO_VERSION", _FALLBACK_VERSION, _DEV_VERSION)
BUILD_CHANNEL: str = _resolve("CACAO_MACRO_BUILD_CHANNEL", _FALLBACK_CHANNEL, _DEV_CHANNEL)

# BUILD_DATE/GIT_COMMIT은 환경변수가 없으면 항상 빈 문자열이다(하드코딩 기본값 없음 — 위 설명 참고).
BUILD_DATE: str = os.environ.get("CACAO_MACRO_BUILD_DATE", "").strip()
GIT_COMMIT: str = os.environ.get("CACAO_MACRO_GIT_COMMIT", "").strip()


def git_commit_short() -> str:
    """진단정보/로그에 표시할 축약 commit 값(7자). 없으면 빈 문자열."""
    return GIT_COMMIT[:7] if GIT_COMMIT else ""


def version_summary() -> str:
    """창 제목/로그 시작 메시지에 쓰는 짧은 한 줄 표시."""
    return f"{APP_VERSION} ({BUILD_CHANNEL})"


def release_target_version() -> tuple:
    """scripts/build_release.py 전용 — "지금 릴리스로 패키징할 버전"을 반환한다.

    이 스크립트는 항상 개발 모드(python 직접 실행)로 돌아가므로, 위
    APP_VERSION/BUILD_CHANNEL(런타임 우선순위 로직)을 그대로 쓰면 개발 모드
    기본값인 "development"가 나온다 — 릴리스 스크립트가 원하는 값은 항상
    "다음에 배포할 고정 버전 번호"(_FALLBACK_VERSION/_FALLBACK_CHANNEL)이므로
    별도 접근자로 분리했다. 버전을 올릴 때는 이 파일 위쪽의 두 상수만
    바꾸면 되고, 이 함수는 그 값을 그대로 반환할 뿐 별도 로직이 없다.
    """
    return _FALLBACK_VERSION, _FALLBACK_CHANNEL
