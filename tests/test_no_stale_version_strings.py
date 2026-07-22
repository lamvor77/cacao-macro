# 구버전 앱 버전 문자열 회귀 방지 테스트.
#
# 버전 표시 정리 작업 — 소스 코드 전체에 이전 공식 버전 문자열이 남아있지
# 않은지 자동으로 확인한다. 새 패치를 낼 때마다 이 튜플의 "지금 막
# 지나간" 버전을 추가하면 된다. 과거에 실제로 배포된 릴리스 산출물
# (release/cacao_macro-v<옛 버전>/)은 그 자체가 배포 이력이므로 예외로
# 둔다(요구사항 — "과거 changelog나 release notes는 제외 가능").

import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_STALE_STRINGS = ("1.0.0-rc.1", "release-candidate", "1.2.0")

_EXCLUDED_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", "dist", "build",
    "test-runtime", "backup", ".pytest_cache",
}

# 과거 배포 이력 산출물 — 요구사항에 따라 예외 처리.
_EXCLUDED_PATH_PREFIXES = (
    os.path.join(PROJECT_ROOT, "release", "cacao_macro-v1.0.0-rc.1"),
    os.path.join(PROJECT_ROOT, "release", "cacao_macro-v1.2.0"),
)

# requirements.txt의 "schedule>=1.2.0"처럼 우리 앱 버전과 무관한 서드파티
# 패키지 버전 고정 표기가 우연히 겹치는 파일 — 파일 단위로 예외 처리한다.
_EXCLUDED_FILENAMES = {"requirements.txt"}

_SCANNED_EXTENSIONS = (".py", ".ts", ".tsx", ".spec", ".txt")


_SELF_PATH = os.path.abspath(__file__)


def _iter_source_files():
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        if any(dirpath.startswith(prefix) for prefix in _EXCLUDED_PATH_PREFIXES):
            continue
        for filename in filenames:
            if filename in _EXCLUDED_FILENAMES:
                continue
            path = os.path.join(dirpath, filename)
            if os.path.abspath(path) == _SELF_PATH:
                continue  # 이 스캐너 자신은 대상 문자열을 상수로 정의하므로 제외
            if filename.endswith(_SCANNED_EXTENSIONS):
                yield path


class TestNoStaleVersionStrings(unittest.TestCase):
    def test_old_version_string_not_present_in_source(self):
        offenders = []
        for path in _iter_source_files():
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            for stale in _STALE_STRINGS:
                if stale in content:
                    offenders.append((os.path.relpath(path, PROJECT_ROOT), stale))

        self.assertEqual(
            offenders, [],
            f"구버전 문자열이 남아있는 파일: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
