# 운영 dist/와 완전히 분리된 테스트 전용 실행 폴더를 만드는 도구
# (Test Environment Deployment & E2E Validation Sprint 6절)
#
# dist/cacao_macro.exe + license_build.json을 test-runtime/ 아래로 복사하고
# storage/logs 하위 폴더를 새로 만든다. .env는 여기서 만들지 않는다 — 테스트
# 프로젝트 URL/anon key/APP_ENV=test는 사용자가 직접 채워야 하는 값이라 이
# 스크립트가 대신 만들지 않는다(값 없는 .env.example만 복사해 둔다).
#
# 이 스크립트는 파일 복사만 한다 — 네트워크 호출, Supabase 접근, EXE 실행을
# 하지 않는다.

import argparse
import os
import shutil
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def setup(target_dir: str, force: bool) -> int:
    dist_exe = os.path.join(PROJECT_ROOT, "dist", "cacao_macro.exe")
    if not os.path.exists(dist_exe):
        print(f"[오류] {dist_exe} 가 없습니다 — 먼저 PyInstaller 빌드를 실행하세요.")
        return 1

    if os.path.exists(target_dir):
        if not force:
            print(f"[오류] {target_dir} 가 이미 존재합니다. 덮어쓰려면 --force를 사용하세요.")
            return 1
        print(f"[안내] 기존 {target_dir} 를 덮어씁니다.")

    os.makedirs(target_dir, exist_ok=True)
    os.makedirs(os.path.join(target_dir, "storage"), exist_ok=True)
    os.makedirs(os.path.join(target_dir, "logs"), exist_ok=True)

    shutil.copy2(dist_exe, os.path.join(target_dir, "cacao_macro.exe"))
    print(f"[완료] cacao_macro.exe 복사됨")

    license_src = os.path.join(PROJECT_ROOT, "dist", "license_build.json")
    if os.path.exists(license_src):
        shutil.copy2(license_src, os.path.join(target_dir, "license_build.json"))
        print(f"[완료] license_build.json 복사됨")
    else:
        print(f"[안내] dist/license_build.json이 없습니다 — 테스트 라이선스가 필요하면 별도로 준비하세요.")

    env_example_src = os.path.join(PROJECT_ROOT, ".env.example")
    env_example_dst = os.path.join(target_dir, ".env.example")
    shutil.copy2(env_example_src, env_example_dst)
    print(f"[완료] .env.example 복사됨 (값 없음 — 직접 .env로 복사 후 테스트 프로젝트 값을 채우세요)")

    print()
    print(f"[안내] {target_dir}\\.env.example 을 {target_dir}\\.env 로 복사한 뒤 다음을 채우세요:")
    print("  APP_ENV=test")
    print("  SUPABASE_ENVIRONMENT=test")
    print("  SUPABASE_ENABLED=true")
    print("  SUPABASE_URL=<테스트 프로젝트 URL>")
    print("  SUPABASE_ANON_KEY=<테스트 프로젝트 anon key>")
    print()
    print(f"[안내] 운영 storage/logs와 절대 공유되지 않습니다 — {target_dir} 내부에서만 데이터가 쌓입니다.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="운영 dist/와 분리된 테스트 전용 실행 폴더 생성")
    parser.add_argument("--target", default=os.path.join(PROJECT_ROOT, "test-runtime"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    return setup(args.target, args.force)


if __name__ == "__main__":
    sys.exit(main())
