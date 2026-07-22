# 버전 비교 유틸리티 — 자동 업데이트 "기반 구조"만 담당한다.
#
# 실제 다운로드/설치/자동 재시작은 이번 스프린트 범위가 아니다(사용자 지시:
# "자동 업데이트 다운로드 기능은 이번에 구현하지 않는다"). 여기서는 다음만
# 제공한다:
#   - 로컬에 기록된 버전 읽기 (release/version.json)
#   - "1.0.0-rc.1" < "1.0.0" < "1.0.1" 같은 프리릴리스 순서를 올바르게 처리하는
#     버전 비교
#   - 새 버전 여부 판정 함수
#
# 문자열 단순 비교("1.0.10" < "1.0.9"처럼 잘못 판정되는 문제)를 피하기 위해
# packaging.version.Version을 사용한다 — 이미 설치된 환경에 존재함을 확인했고
# (다른 패키지의 전이 의존성), 순수 Python이라 PyInstaller가 별도 설정 없이도
# 정상적으로 포함한다(이번 RC 빌드에서 실제로 검증함).

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

VERSION_MANIFEST_FILENAME = "version.json"


@dataclass
class VersionInfo:
    """release/version.json 1건의 내용."""

    version: str
    channel: str
    published_at: str
    minimum_supported_version: str


def parse_version_safe(raw: str) -> Optional[Version]:
    """비교 가능한 Version 객체로 변환한다. 형식이 잘못되었으면 None(예외를 던지지 않음)."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        logger.warning(f"버전 문자열을 해석할 수 없습니다: {raw!r}")
        return None


def is_newer_version(current: str, candidate: str) -> bool:
    """candidate가 current보다 실제로 더 새 버전인지 판정한다.

    둘 중 하나라도 파싱할 수 없으면(형식 오류) 안전하게 False를 반환한다 —
    "새 버전이 있다"고 잘못 판단해 불필요한 알림을 띄우는 쪽보다, 판단이
    애매하면 아무 것도 하지 않는 쪽이 안전하다("development" 같은 비-semver
    문자열이 여기 들어오는 것도 정상적인 상황이다).
    """
    current_v = parse_version_safe(current)
    candidate_v = parse_version_safe(candidate)
    if current_v is None or candidate_v is None:
        return False
    return candidate_v > current_v


def compare_versions(a: str, b: str) -> int:
    """a와 b를 비교한다. a<b: -1, a==b: 0, a>b: 1. 파싱 불가 시 0(비교 불가 취급)."""
    va, vb = parse_version_safe(a), parse_version_safe(b)
    if va is None or vb is None:
        return 0
    if va < vb:
        return -1
    if va > vb:
        return 1
    return 0


def load_version_manifest(base_dir: str) -> Optional[VersionInfo]:
    """base_dir(보통 exe/스크립트가 있는 폴더)의 release/version.json을 읽는다.

    이 함수는 파일을 읽기만 한다 — 인터넷 조회, 자동 다운로드는 절대 하지
    않는다(이번 스프린트의 명시적 제약).
    """
    path = os.path.join(base_dir, "release", VERSION_MANIFEST_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return VersionInfo(
            version=data.get("version", ""),
            channel=data.get("channel", ""),
            published_at=data.get("published_at", ""),
            minimum_supported_version=data.get("minimum_supported_version", ""),
        )
    except (json.JSONDecodeError, OSError, TypeError, KeyError) as e:
        logger.warning(f"version.json 읽기 실패: {type(e).__name__}")
        return None
