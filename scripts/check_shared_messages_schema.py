# shared_messages 스키마/RPC 점검 스크립트 (Production Stabilization Sprint)
#
# 운영 DB를 절대 변경하지 않는다 — 이 스크립트가 실행하는 것은 다음뿐이다:
#   - SELECT (테이블 존재/데이터 확인, RLS 허용된 범위 내에서만 행이 보임)
#   - GET 요청으로 PostgREST의 OpenAPI 루트 문서 조회(RPC/컬럼 존재 확인 —
#     이 요청 자체는 함수를 "호출"하지 않는다. 실행이 아니라 스키마 설명서를
#     읽는 것이다)
# INSERT/UPDATE/DELETE/RPC 실행은 어디에도 없다.
#
# 비밀번호/토큰/URL 전체를 출력하지 않는다 — 호스트 일부만 마스킹해 보여준다
# (services/diagnostics_service.py의 mask_host()와 동일한 규칙).
#
# 사용법:
#   python scripts/check_shared_messages_schema.py
#   python scripts/check_shared_messages_schema.py --access-token <JWT>
#
# --access-token(또는 SUPABASE_TEST_ACCESS_TOKEN 환경변수)을 주면 실제 로그인
# 세션으로 RLS가 허용하는 범위에서 데이터 수준 점검(1~12행 존재/중복/revision)도
# 수행한다. 주지 않으면 anon key만으로 가능한 존재 여부 점검만 수행하고,
# 데이터/Realtime publication 점검은 [WARN]으로 건너뛴다(둘 다 anon 키만으로는
# 확인할 수 없는 항목이다 — RLS로 막혀 있거나, PostgREST가 익명에게 노출하지
# 않는 시스템 카탈로그이기 때문).

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

# Windows 콘솔 기본 인코딩(cp949 등)은 이 스크립트가 쓰는 한글/em-dash(—) 같은
# 문자를 인코딩하지 못해 UnicodeEncodeError로 죽을 수 있다 — 운영 담당자가 이
# 스크립트를 실행하는 실제 환경(Windows PowerShell/cmd)에서 흔히 겪는 문제라
# 항상 UTF-8로 강제한다(Python 3.7+의 TextIOWrapper.reconfigure).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

EXPECTED_TABLES = ("shared_messages", "shared_message_history")
EXPECTED_RPCS = ("update_shared_message", "force_update_shared_message")
EXPECTED_COLUMNS = {
    "shared_messages": (
        "id", "message_no", "title", "content", "revision", "is_active",
        "updated_at", "updated_by", "updated_by_name", "update_source", "created_at",
    ),
    "shared_message_history": (
        "id", "message_id", "message_no", "previous_content", "new_content",
        "previous_revision", "new_revision", "changed_by", "changed_by_name",
        "changed_from", "changed_at",
    ),
}
MIN_MESSAGE_NO = 1
MAX_MESSAGE_NO = 12

# PostgREST가 "테이블/뷰를 스키마 캐시에서 찾을 수 없음"을 나타낼 때 쓰는 코드.
_MISSING_RELATION_CODE = "PGRST205"


@dataclass
class CheckResult:
    level: str  # "PASS" | "WARN" | "FAIL"
    name: str
    detail: str = ""

    def format(self) -> str:
        suffix = f" — {self.detail}" if self.detail else ""
        return f"[{self.level}] {self.name}{suffix}"


def mask_host(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    if not host:
        return ""
    parts = host.split(".", 1)
    prefix = parts[0]
    rest = f".{parts[1]}" if len(parts) > 1 else ""
    if len(prefix) <= 4:
        return prefix[:1] + "*" * max(len(prefix) - 1, 0) + rest
    return prefix[:4] + "*" * (len(prefix) - 4) + rest


# ============================================================
# 개별 점검 함수 — 각각 CheckResult를 반환한다(테스트 용이성을 위해 분리).
# ============================================================

def check_table_exists(client, table_name: str) -> CheckResult:
    try:
        client.table(table_name).select("*").limit(1).execute()
        return CheckResult("PASS", f"{table_name} table")
    except Exception as e:
        code = getattr(e, "code", None)
        message = str(getattr(e, "message", None) or e)
        if code == _MISSING_RELATION_CODE or "could not find the table" in message.lower():
            return CheckResult("FAIL", f"{table_name} table", "테이블을 찾을 수 없습니다 — 마이그레이션 미적용 가능성")
        # RLS로 인한 권한 거부 등은 "테이블이 존재는 한다"는 뜻이므로 PASS로 취급한다 —
        # 여기서는 오직 "테이블 자체가 있는지"만 확인한다(데이터 접근 가능 여부는 별도 점검).
        return CheckResult("PASS", f"{table_name} table", f"조회 중 알림({type(e).__name__}, 테이블 자체는 존재하는 것으로 판단)")


def check_rpc_exists(openapi_paths: Optional[dict], rpc_name: str) -> CheckResult:
    if openapi_paths is None:
        return CheckResult("WARN", f"{rpc_name} RPC", "OpenAPI 문서를 가져오지 못해 확인할 수 없음")
    key = f"/rpc/{rpc_name}"
    if key in openapi_paths:
        return CheckResult("PASS", f"{rpc_name} RPC")
    return CheckResult("FAIL", f"{rpc_name} RPC", "PostgREST에 노출된 RPC 목록에 없음")


def check_columns(openapi_definitions: Optional[dict], table_name: str) -> CheckResult:
    expected = EXPECTED_COLUMNS.get(table_name, ())
    if openapi_definitions is None:
        return CheckResult("WARN", f"{table_name} columns", "OpenAPI 문서를 가져오지 못해 확인할 수 없음")
    table_def = openapi_definitions.get(table_name)
    if table_def is None:
        return CheckResult("FAIL", f"{table_name} columns", "OpenAPI 정의에 테이블이 없음")
    actual_columns = set(table_def.get("properties", {}).keys())
    missing = [c for c in expected if c not in actual_columns]
    if missing:
        return CheckResult("FAIL", f"{table_name} columns", f"누락된 컬럼: {missing}")
    return CheckResult("PASS", f"{table_name} columns")


def check_message_rows(rows: Optional[list]) -> list:
    """rows가 None이면(인증 세션 없어 조회 못함) WARN 하나만 반환.
    있으면 1~12 존재/중복/revision 이상 3개 결과를 반환한다."""
    if rows is None:
        return [CheckResult(
            "WARN", "message rows 1-12",
            "인증 세션이 없어 데이터 확인 불가 — --access-token으로 재실행하면 확인됨",
        )]

    results = []
    numbers = [r.get("message_no") for r in rows if isinstance(r.get("message_no"), int)]
    expected = set(range(MIN_MESSAGE_NO, MAX_MESSAGE_NO + 1))
    missing = expected - set(numbers)
    if missing:
        results.append(CheckResult("FAIL", "message rows 1-12", f"누락된 번호: {sorted(missing)}"))
    else:
        results.append(CheckResult("PASS", "message rows 1-12"))

    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        results.append(CheckResult("FAIL", "no duplicate message_no", f"중복 번호: {duplicates}"))
    else:
        results.append(CheckResult("PASS", "no duplicate message_no"))

    bad_revisions = [
        (r.get("message_no"), r.get("revision")) for r in rows
        if not isinstance(r.get("revision"), int) or r.get("revision") < 1
    ]
    if bad_revisions:
        results.append(CheckResult("FAIL", "revision values sane", f"비정상 revision: {bad_revisions}"))
    else:
        results.append(CheckResult("PASS", "revision values sane"))

    return results


def check_select_access(rows: Optional[list], has_access_token: bool) -> CheckResult:
    if not has_access_token:
        return CheckResult("WARN", "current user SELECT access", "anon key만으로는 확인 불가 — --access-token 필요")
    if rows is not None:
        return CheckResult("PASS", "current user SELECT access")
    return CheckResult("FAIL", "current user SELECT access", "인증 세션으로도 조회 실패(RLS 또는 승인 상태 확인 필요)")


def check_realtime_publication_placeholder() -> CheckResult:
    # pg_publication_tables는 PostgREST가 anon/authenticated에게 노출하는 스키마가
    # 아니다(시스템 카탈로그) — REST API만으로는 원천적으로 확인할 수 없다.
    return CheckResult(
        "WARN", "realtime publication",
        "REST API로는 확인 불가 — Supabase 대시보드의 Database > Replication에서 shared_messages 활성화 여부를 수동 확인할 것",
    )


# ============================================================
# 실행부 — 실제 네트워크 호출
# ============================================================

def _fetch_openapi_document(url: str, anon_key: str, access_token: Optional[str]):
    import httpx

    headers = {"apikey": anon_key}
    headers["Authorization"] = f"Bearer {access_token or anon_key}"
    try:
        response = httpx.get(f"{url.rstrip('/')}/rest/v1/", headers=headers, timeout=10.0)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def run_checks(access_token: Optional[str] = None) -> list:
    from config.cloud_settings import get_cloud_config
    from services.supabase_client import SupabaseClientManager

    config = get_cloud_config()
    results = []

    print(f"대상 Supabase 프로젝트: {mask_host(config.url)}")
    print(f"인증 세션: {'제공됨(--access-token)' if access_token else '없음(anon key만 사용)'}")
    print()

    if not config.enabled or not config.url or not config.anon_key:
        results.append(CheckResult("FAIL", "설정", "SUPABASE_ENABLED/URL/ANON_KEY가 올바르게 설정되지 않음"))
        return results

    client_manager = SupabaseClientManager(config)
    client_result = client_manager.get_client()
    if not client_result.success:
        results.append(CheckResult("FAIL", "Supabase 클라이언트 생성", client_result.error or ""))
        return results
    client = client_result.client

    if access_token:
        try:
            client.auth.set_session(access_token, access_token)
        except Exception as e:
            results.append(CheckResult("WARN", "인증 세션 적용", f"{type(e).__name__} — anon 권한으로 계속 진행"))

    for table_name in EXPECTED_TABLES:
        results.append(check_table_exists(client, table_name))

    openapi_doc = _fetch_openapi_document(config.url, config.anon_key, access_token)
    openapi_paths = openapi_doc.get("paths") if openapi_doc else None
    openapi_definitions = openapi_doc.get("definitions") if openapi_doc else None

    for rpc_name in EXPECTED_RPCS:
        results.append(check_rpc_exists(openapi_paths, rpc_name))

    for table_name in EXPECTED_TABLES:
        results.append(check_columns(openapi_definitions, table_name))

    rows = None
    if access_token:
        try:
            response = client.table("shared_messages").select("message_no,revision").execute()
            rows = response.data or []
        except Exception:
            rows = None

    results.extend(check_message_rows(rows))
    results.append(check_select_access(rows, has_access_token=bool(access_token)))
    results.append(check_realtime_publication_placeholder())

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="shared_messages 스키마/RPC read-only 점검(운영 DB 미변경)")
    parser.add_argument("--access-token", default=os.environ.get("SUPABASE_TEST_ACCESS_TOKEN", ""))
    args = parser.parse_args()

    results = run_checks(access_token=args.access_token or None)

    for r in results:
        print(r.format())

    fail_count = sum(1 for r in results if r.level == "FAIL")
    warn_count = sum(1 for r in results if r.level == "WARN")
    print()
    print(f"결과: PASS={sum(1 for r in results if r.level == 'PASS')} WARN={warn_count} FAIL={fail_count}")
    return 1 if fail_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
