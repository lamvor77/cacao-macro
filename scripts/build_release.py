# 릴리스 패키지 조립 스크립트 (Release Candidate Sprint 1)
#
# dist/cacao_macro.exe가 이미 빌드되어 있다고 가정한다 — 이 스크립트는
# PyInstaller를 실행하지 않는다(재빌드 여부는 사람이 별도로 결정해야 하는
# 일이다 — 이 스프린트의 "재빌드는 최대한 1회로 제한한다" 원칙과 맞물려,
# 이 스크립트를 몇 번 실행해도 재빌드가 유발되지 않도록 완전히 분리했다).
#
# 하는 일:
#   1. dist/cacao_macro.exe의 SHA-256/크기를 기록한다.
#   2. release/cacao_macro-v{버전}/ 폴더를 만들고 배포에 필요한 파일만 담는다
#      (.env.example만 담고, 실제 .env는 절대 담지 않는다).
#   3. release_manifest.json / checksums.sha256 / release/version.json을 만든다.
#   4. 패키지 안에 실제 비밀값(.env, 토큰, credentials 등)이 없는지 자체 검사한다.
#
# build_release(project_root)로 핵심 로직을 분리했다 — tests/test_build_release.py가
# 임시 디렉터리로 실제 프로젝트를 건드리지 않고 이 함수를 그대로 호출해 검증한다.
#
# 사용법: python scripts/build_release.py

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config.version import release_target_version  # noqa: E402
from services.backup_service import BACKUP_FORMAT_VERSION  # noqa: E402

# 패키지에 포함하면 안 되는 파일명/확장자 — 자체 검사에 사용.
FORBIDDEN_FILENAMES = {".env", "token.json", "credentials.json", "creds.json", "session.dat"}
FORBIDDEN_SUFFIXES = (".pyc",)
FORBIDDEN_DIR_NAMES = {"logs", "storage", "backup", ".git", "__pycache__"}


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_git_commit(project_root: str) -> str:
    """짧은 commit hash. 워킹 트리에 커밋되지 않은 변경이 있으면 -dirty를 붙인다
    (이 스프린트는 검증 전 커밋하지 않으므로, 이 빌드는 사실상 항상 -dirty다 —
    투명하게 표시하는 것이 목적이다). git이 없거나 저장소가 아니면 "unknown"."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=project_root, text=True,
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=project_root, text=True,
        ).strip()
        return f"{commit}-dirty" if status else commit
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


def scan_for_forbidden_content(package_dir: str) -> list:
    """실제 .env, 토큰, credentials, 로그/storage/backup 디렉터리가 패키지에
    섞여 들어가지 않았는지 확인한다."""
    problems = []
    for root, dirs, files in os.walk(package_dir):
        dirs[:] = [d for d in dirs if d not in FORBIDDEN_DIR_NAMES]
        for name in files:
            if name in FORBIDDEN_FILENAMES or name.endswith(FORBIDDEN_SUFFIXES):
                problems.append(os.path.relpath(os.path.join(root, name), package_dir))
    return problems


def build_release(project_root: str) -> dict:
    """release/cacao_macro-v{버전}/ 패키지를 조립하고 결과 요약 dict를 반환한다.

    반환값의 "problems"가 비어 있지 않으면 자체 보안 검사에 걸린 것이다
    (비밀값 자체는 반환값에도 절대 담지 않는다 — 파일 상대경로만 담는다).
    """
    version, channel = release_target_version()
    exe_path = os.path.join(project_root, "dist", "cacao_macro.exe")
    if not os.path.exists(exe_path):
        raise FileNotFoundError(f"{exe_path}가 없습니다 — 먼저 PyInstaller로 빌드하세요.")

    exe_sha256 = sha256_of_file(exe_path)
    exe_size = os.path.getsize(exe_path)
    git_commit = get_git_commit(project_root)
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    package_dir = os.path.join(project_root, "release", f"cacao_macro-v{version}")
    if os.path.exists(package_dir):
        shutil.rmtree(package_dir)
    os.makedirs(package_dir)

    shutil.copy2(exe_path, os.path.join(package_dir, "cacao_macro.exe"))

    env_example_src = os.path.join(project_root, ".env.example")
    if os.path.exists(env_example_src):
        shutil.copy2(env_example_src, os.path.join(package_dir, ".env.example"))

    # 내부 배포용 도구로 판단해 license_build.json을 포함한다(문서에 무단
    # 공유 금지를 명시함 — docs/license_deployment_guide.md, README.txt 참고).
    # 다수 사용자에게 공개 배포할 계획이 생기면 이 단계를 재검토해야 한다.
    license_src = os.path.join(project_root, "license_build.json")
    license_included = False
    if os.path.exists(license_src):
        shutil.copy2(license_src, os.path.join(package_dir, "license_build.json"))
        license_included = True

    readme_src = os.path.join(project_root, "docs", "release_readme_ko.txt")
    if os.path.exists(readme_src):
        shutil.copy2(readme_src, os.path.join(package_dir, "README.txt"))
    install_src = os.path.join(project_root, "INSTALL.md")
    if os.path.exists(install_src):
        shutil.copy2(install_src, os.path.join(package_dir, "INSTALL.md"))

    # release/version.json — 앱 자신의 런타임 버전 확인용(core/version_check.py 참고)
    nested_release_dir = os.path.join(package_dir, "release")
    os.makedirs(nested_release_dir, exist_ok=True)
    version_manifest = {
        "version": version, "channel": channel,
        "published_at": built_at, "minimum_supported_version": version,
    }
    with open(os.path.join(nested_release_dir, "version.json"), "w", encoding="utf-8") as f:
        json.dump(version_manifest, f, ensure_ascii=False, indent=2)

    release_manifest = {
        "app_name": "카카오톡 자동화",
        "version": version,
        "channel": channel,
        "built_at": built_at,
        "git_commit": git_commit,
        "files": [{"name": "cacao_macro.exe", "size": exe_size, "sha256": exe_sha256}],
        "required_external_files": [".env", "license_build.json"],
        "license_external": True,
        "license_included_in_package": license_included,
        "backup_format_version": BACKUP_FORMAT_VERSION,
    }
    with open(os.path.join(package_dir, "release_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(release_manifest, f, ensure_ascii=False, indent=2)

    checksum_lines = []
    for name in sorted(os.listdir(package_dir)):
        full = os.path.join(package_dir, name)
        if os.path.isfile(full):
            checksum_lines.append(f"{sha256_of_file(full)}  {name}")
    with open(os.path.join(package_dir, "checksums.sha256"), "w", encoding="utf-8") as f:
        f.write("\n".join(checksum_lines) + "\n")

    problems = scan_for_forbidden_content(package_dir)

    return {
        "package_dir": package_dir,
        "version": version,
        "channel": channel,
        "git_commit": git_commit,
        "exe_sha256": exe_sha256,
        "exe_size": exe_size,
        "license_included": license_included,
        "problems": problems,
    }


def main() -> int:
    try:
        result = build_release(PROJECT_ROOT)
    except FileNotFoundError as e:
        print(f"오류: {e}")
        return 1

    print(f"버전: {result['version']} ({result['channel']})")
    print(f"git commit: {result['git_commit']}")
    print(f"exe 크기: {result['exe_size']:,} bytes")
    print(f"exe SHA-256: {result['exe_sha256']}")
    print(f"license_build.json 포함: {result['license_included']}")
    print(f"패키지 경로: {result['package_dir']}")
    if result["problems"]:
        print("\n[경고] 패키지 내 금지 파일 발견:")
        for p in result["problems"]:
            print(f"  - {p}")
        return 2
    print("\n금지 파일(.env/토큰/로그 등) 없음 — 자체 검사 통과.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
