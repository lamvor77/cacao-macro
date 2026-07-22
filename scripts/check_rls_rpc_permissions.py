# shared_messages RLS/RPC 권한 매트릭스 자동 점검 스크립트
# (Test Environment Deployment & E2E Validation Sprint 5절)
#
# 목적: employee_a/employee_b/admin_a/disabled_user 4개 역할이 각각
#   - shared_messages/shared_message_history SELECT
#   - update_shared_message RPC
#   - force_update_shared_message RPC
#   - shared_messages 테이블 직접 UPDATE/INSERT/DELETE(정책 우회 시도)
# 를 시도했을 때 스펙 5절이 요구하는 대로 허용/거부되는지 실제로 호출해 확인한다.
#
# ⚠ 이 스크립트는 read-only가 아니다 — update_shared_message/
# force_update_shared_message 성공 케이스를 검증하려면 실제로 한 번 써야 한다.
# 그래서 다음 안전장치를 둔다:
#   1. SUPABASE_URL에 알려진 운영 프로젝트 호스트 일부가 포함되어 있으면
#      무조건 즉시 중단한다(하드코딩된 차단 목록 — 아래 _PRODUCTION_HOST_DENYLIST).
#   2. APP_ENV=test 또는 SUPABASE_ENVIRONMENT=test가 설정되어 있지 않으면
#      실행 자체를 거부한다(운영 .env를 실수로 그대로 쓰는 사고 방지).
#   3. 테스트에 사용하는 message_no(기본 12)의 기존 내용을 먼저 읽어 두었다가,
#      각 쓰기 성공 직후 "같은 역할이 낼 수 있는 가장 안전한 방법"으로 즉시
#      원래 내용으로 복원한다(복원도 RPC를 통하며 history에 남는다 — 스펙
#      9절의 "복원도 RPC를 통해 수행" 원칙을 5절 검증에도 동일하게 적용).
#   4. 토큰/이메일 원문은 어디에도 출력/기록하지 않는다 — role alias만 쓴다.
#
# 사용법 (테스트 프로젝트 .env가 이미 로드된 상태에서):
#   python scripts/check_rls_rpc_permissions.py \
#       --employee-a-token $EMPLOYEE_A_TOKEN \
#       --employee-b-token $EMPLOYEE_B_TOKEN \
#       --admin-token $ADMIN_TOKEN \
#       --disabled-token $DISABLED_TOKEN \
#       --message-no 12
#
# 토큰은 CLI 인자 대신 환경변수(TEST_EMPLOYEE_A_TOKEN 등)로 주는 것을 권장한다
# (쉘 히스토리에 남지 않도록). 일부 역할 토큰이 없으면 해당 역할 검사만
# [WARN]으로 건너뛰고 나머지는 계속 진행한다.

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Optional

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 알려진 운영 프로젝트 호스트(일부) — 이 문자열이 SUPABASE_URL에 포함되어 있으면
# 절대 실행하지 않는다. 새 운영 프로젝트가 생기면 이 목록에 추가한다.
_PRODUCTION_HOST_DENYLIST = ("kdyxxkltafeuucijiyzp",)
# 운영 프로젝트 전환(2026-07): cacao-macro-test(kdyxxkltafeuucijiyzp)가 운영으로
# 승격되고, 기존 운영 프로젝트(nojdwuoronqmvpdptvlr)는 삭제됐다 — 이 차단 목록은
# "지금 진짜 운영인 프로젝트"를 가리켜야 안전장치로서 의미가 있으므로 값을
# 교체했다(옛 값을 지우고 새 값을 안 넣으면 이 스크립트가 아무 프로젝트도
# 차단하지 못하는 상태가 된다 — 그게 더 위험하다). 이후 별도의 테스트 전용
# 프로젝트를 새로 만들면, 그 프로젝트의 .env로 이 스크립트를 실행할 것.

_TEST_MESSAGE_NO_DEFAULT = 12
_TEST_MARKER = "[RLS_RPC_PERMISSION_TEST]"


@dataclass
class CheckResult:
    level: str  # PASS | WARN | FAIL
    name: str
    detail: str = ""

    def format(self) -> str:
        suffix = f" — {self.detail}" if self.detail else ""
        return f"[{self.level}] {self.name}{suffix}"


def _mask_host(url: str) -> str:
    from services.diagnostics_service import mask_host
    return mask_host(url)


def _guard_not_production(url: str) -> None:
    lowered = (url or "").lower()
    for bad in _PRODUCTION_HOST_DENYLIST:
        if bad.lower() in lowered:
            print(f"[FAIL] 안전장치: SUPABASE_URL에 운영 프로젝트로 알려진 호스트가 포함되어 있습니다 — 중단합니다.")
            sys.exit(2)

    app_env = os.environ.get("APP_ENV", "").strip().lower()
    supabase_env = os.environ.get("SUPABASE_ENVIRONMENT", "").strip().lower()
    if app_env != "test" and supabase_env != "test":
        print(
            "[FAIL] 안전장치: APP_ENV=test 또는 SUPABASE_ENVIRONMENT=test가 "
            "설정되어 있지 않습니다 — 테스트 프로젝트임이 명시적으로 확인되지 "
            "않으면 쓰기 검증을 실행하지 않습니다."
        )
        sys.exit(2)


def _make_client(url: str, anon_key: str, access_token: Optional[str]):
    from supabase import create_client

    client = create_client(url, anon_key)
    if access_token:
        client.auth.set_session(access_token, access_token)
    return client


def _check_select(client, table: str, role: str) -> CheckResult:
    try:
        client.table(table).select("message_no").limit(1).execute()
        return CheckResult("PASS", f"{role}: {table} SELECT 허용")
    except Exception as e:
        return CheckResult("INFO", f"{role}: {table} SELECT 거부됨({type(e).__name__})")


def _expect(role: str, name: str, should_succeed: bool, attempt_fn) -> CheckResult:
    try:
        attempt_fn()
        succeeded = True
        detail = ""
    except Exception as e:
        succeeded = False
        detail = type(e).__name__

    if succeeded == should_succeed:
        return CheckResult("PASS", f"{role}: {name}", "기대대로 동작함")
    level = "FAIL"
    expectation = "성공해야 하는데 실패함" if should_succeed else "거부되어야 하는데 성공함(위험)"
    return CheckResult(level, f"{role}: {name}", f"{expectation}({detail})" if detail else expectation)


def _check_employee_like(client, role: str, message_no: int, is_admin: bool) -> list:
    results = []
    results.append(_check_select(client, "shared_messages", role))
    results.append(_check_select(client, "shared_message_history", role))

    # 1) 정상 저장 RPC — employee/admin 모두 성공해야 한다. 성공하면 원래
    #    내용으로 즉시 복원한다(같은 역할이 낼 수 있는 가장 안전한 방법).
    original = None
    try:
        row = client.table("shared_messages").select("content,revision").eq("message_no", message_no).single().execute()
        original = row.data
    except Exception:
        original = None

    def _do_update():
        if original is None:
            raise RuntimeError("원본 내용을 읽지 못해 쓰기 시도를 건너뜁니다")
        client.rpc("update_shared_message", {
            "p_message_no": message_no,
            "p_title": None,
            "p_content": f"{_TEST_MARKER} role={role}",
            "p_base_revision": original["revision"],
            "p_update_source": "desktop",
        }).execute()

    update_result = _expect(role, "update_shared_message(정상 저장)", True, _do_update)
    results.append(update_result)

    if update_result.level == "PASS" and original is not None:
        try:
            client.rpc("update_shared_message", {
                "p_message_no": message_no,
                "p_title": None,
                "p_content": original["content"],
                "p_base_revision": original["revision"] + 1,
                "p_update_source": "desktop",
            }).execute()
            results.append(CheckResult("PASS", f"{role}: 테스트 후 원본 내용 복원", "RPC를 통해 복원, history에 기록됨"))
        except Exception as e:
            results.append(CheckResult("FAIL", f"{role}: 테스트 후 원본 내용 복원 실패", f"{type(e).__name__} — 수동 확인 필요"))

    # 2) 강제 저장 RPC — employee는 거부, admin은 성공해야 한다.
    def _do_force():
        client.rpc("force_update_shared_message", {
            "p_message_no": message_no,
            "p_title": None,
            "p_content": f"{_TEST_MARKER} force role={role}",
            "p_update_source": "admin_force",
        }).execute()

    force_result = _expect(role, "force_update_shared_message(강제 저장)", is_admin, _do_force)
    results.append(force_result)
    if is_admin and force_result.level == "PASS" and original is not None:
        try:
            client.rpc("force_update_shared_message", {
                "p_message_no": message_no,
                "p_title": None,
                "p_content": original["content"],
                "p_update_source": "admin_force",
            }).execute()
            results.append(CheckResult("PASS", f"{role}: 강제 저장 테스트 후 원본 내용 복원", "RPC를 통해 복원, history에 기록됨"))
        except Exception as e:
            results.append(CheckResult("FAIL", f"{role}: 강제 저장 테스트 후 원본 내용 복원 실패", f"{type(e).__name__} — 수동 확인 필요"))

    # 3) 직접 테이블 쓰기 시도 — RLS에 쓰기 정책이 전혀 없으므로 누구든 항상 거부되어야 한다.
    def _do_direct_update():
        client.table("shared_messages").update({"content": f"{_TEST_MARKER} direct-bypass"}).eq("message_no", message_no).execute()

    results.append(_expect(role, "shared_messages 직접 UPDATE(정책 우회 시도)", False, _do_direct_update))

    def _do_direct_insert():
        client.table("shared_messages").insert({"message_no": 999, "content": _TEST_MARKER}).execute()

    results.append(_expect(role, "shared_messages 직접 INSERT", False, _do_direct_insert))

    def _do_direct_delete():
        client.table("shared_messages").delete().eq("message_no", message_no).execute()

    results.append(_expect(role, "shared_messages 직접 DELETE", False, _do_direct_delete))

    def _do_history_insert():
        client.table("shared_message_history").insert({
            "message_no": message_no, "new_content": _TEST_MARKER, "new_revision": 1, "changed_from": "desktop",
        }).execute()

    results.append(_expect(role, "shared_message_history 직접 INSERT", False, _do_history_insert))

    return results


def _check_disabled(client, role: str, message_no: int) -> list:
    results = [_check_select(client, "shared_messages", role)]

    def _do_update():
        client.rpc("update_shared_message", {
            "p_message_no": message_no, "p_title": None, "p_content": _TEST_MARKER,
            "p_base_revision": 1, "p_update_source": "desktop",
        }).execute()

    results.append(_expect(role, "update_shared_message(비활성 계정)", False, _do_update))

    def _do_force():
        client.rpc("force_update_shared_message", {
            "p_message_no": message_no, "p_title": None, "p_content": _TEST_MARKER,
            "p_update_source": "admin_force",
        }).execute()

    results.append(_expect(role, "force_update_shared_message(비활성 계정)", False, _do_force))
    return results


def run(args) -> int:
    from config.cloud_settings import get_cloud_config

    config = get_cloud_config()
    if not config.enabled or not config.url or not config.anon_key:
        print("[FAIL] SUPABASE_ENABLED/URL/ANON_KEY가 올바르게 설정되지 않음")
        return 1

    _guard_not_production(config.url)

    print(f"대상 Supabase 프로젝트: {_mask_host(config.url)}")
    print(f"테스트 대상 message_no: {args.message_no}")
    print()

    tokens = {
        "employee_a": args.employee_a_token or os.environ.get("TEST_EMPLOYEE_A_TOKEN"),
        "employee_b": args.employee_b_token or os.environ.get("TEST_EMPLOYEE_B_TOKEN"),
        "admin_a": args.admin_token or os.environ.get("TEST_ADMIN_TOKEN"),
        "disabled_user": args.disabled_token or os.environ.get("TEST_DISABLED_TOKEN"),
    }

    all_results = []
    for role, token in tokens.items():
        if not token:
            all_results.append(CheckResult("WARN", f"{role} 토큰 없음", "--{role}-token 또는 TEST_*_TOKEN 환경변수로 제공하면 검사됨"))
            continue
        client = _make_client(config.url, config.anon_key, token)
        if role == "disabled_user":
            all_results.extend(_check_disabled(client, role, args.message_no))
        else:
            all_results.extend(_check_employee_like(client, role, args.message_no, is_admin=(role == "admin_a")))

    for r in all_results:
        print(r.format())

    fail_count = sum(1 for r in all_results if r.level == "FAIL")
    warn_count = sum(1 for r in all_results if r.level == "WARN")
    pass_count = sum(1 for r in all_results if r.level == "PASS")
    print()
    print(f"결과: PASS={pass_count} WARN={warn_count} FAIL={fail_count}")
    return 1 if fail_count > 0 else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="shared_messages RLS/RPC 권한 매트릭스 검증(테스트 프로젝트 전용)")
    parser.add_argument("--employee-a-token", default="")
    parser.add_argument("--employee-b-token", default="")
    parser.add_argument("--admin-token", default="")
    parser.add_argument("--disabled-token", default="")
    parser.add_argument("--message-no", type=int, default=_TEST_MESSAGE_NO_DEFAULT)
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
