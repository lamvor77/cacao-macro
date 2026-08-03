# docs/sql/phase4_admin_rpc.sql과 services/admin_service.py의 정합성을 정적으로
# 검사한다 — 실제 Supabase에는 절대 연결하지 않는다(이 프로젝트 전체 원칙).
#
# 배경: 운영 프로젝트에서 admin_list_users 호출 시 PGRST202(함수를 찾을 수
# 없음) 오류가 발생했다. 조사 결과 Python이 보내는 파라미터와 SQL 정의는
# 정확히 일치했고, 실제 원인은 이 SQL 파일이 운영 프로젝트에 적용된 적이
# 없었기 때문으로 추정된다. 이 테스트는 "SQL 파일과 Python 호출이 실제로
# 일치하는가"를 회귀 방지 차원에서 고정한다 — 향후 둘 중 하나만 바뀌어
# 다시 어긋나는 사고를 미리 잡기 위함이다.
#
# 실행: python -m unittest tests.test_admin_rpc_sql_deployment -v

import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_PATH = os.path.join(PROJECT_ROOT, "docs", "sql", "phase4_admin_rpc.sql")
ADMIN_SERVICE_PATH = os.path.join(PROJECT_ROOT, "services", "admin_service.py")

# Python AdminService가 실제로 client.rpc(이름, {파라미터...})로 호출하는
# admin_* RPC와 그 파라미터 이름(services/admin_service.py에서 직접 옮겨 적음 —
# 이 목록 자체가 바뀌면 아래 테스트가 그 변경을 놓치지 않도록, 파이썬 소스도
# 별도로 파싱해 이 목록과 실제 소스가 일치하는지까지 검증한다).
EXPECTED_RPC_PARAMS = {
    "admin_list_users": {"p_status", "p_role", "p_search", "p_limit", "p_offset"},
    "admin_list_audit_logs": {"p_target_user_id", "p_action", "p_limit", "p_offset"},
    "admin_approve_user": {"p_target_user_id", "p_role", "p_reason"},
    "admin_block_user": {"p_target_user_id", "p_reason"},
    "admin_unblock_user": {"p_target_user_id", "p_restore_status", "p_reason"},
    "admin_update_user_role": {"p_target_user_id", "p_new_role", "p_reason"},
}


def _extract_sql_function_params(sql: str, fn_name: str) -> set:
    """`create or replace function public.<fn_name>(...)` 괄호 안의 파라미터
    이름만 뽑아낸다(타입/기본값은 무시, 이름 집합만 비교)."""
    pattern = rf"create or replace function public\.{re.escape(fn_name)}\s*\((.*?)\)\s*\n?\s*returns"
    match = re.search(pattern, sql, re.IGNORECASE | re.DOTALL)
    assert match, f"{fn_name} 정의를 SQL에서 찾지 못함"
    params_block = match.group(1)
    return set(re.findall(r"(p_[a-z_]+)\s+", params_block))


def _extract_python_rpc_calls(source: str) -> dict:
    """services/admin_service.py에서 self._call_rpc("이름", {파라미터...}) 호출을
    직접 파싱해, 실제 소스의 RPC 이름/파라미터 목록을 얻는다."""
    calls = {}
    for match in re.finditer(
        r'self\._call_rpc\(\s*"([a-z_]+)"\s*,\s*\{(.*?)\}\s*\)', source, re.DOTALL
    ):
        fn_name, body = match.group(1), match.group(2)
        params = set(re.findall(r'"(p_[a-z_]+)"\s*:', body))
        calls[fn_name] = params
    return calls


class TestAdminRpcSqlDeployment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SQL_PATH, encoding="utf-8") as f:
            cls.sql = f.read()
        with open(ADMIN_SERVICE_PATH, encoding="utf-8") as f:
            cls.py_source = f.read()
        cls.py_calls = _extract_python_rpc_calls(cls.py_source)

    def test_file_exists(self):
        self.assertTrue(os.path.exists(SQL_PATH))

    def test_python_source_matches_expected_rpc_list(self):
        """이 테스트 파일 상단의 EXPECTED_RPC_PARAMS가 실제 admin_service.py
        소스와 어긋나면(RPC 추가/삭제/파라미터 변경), 먼저 이 사실 자체를
        드러낸다 — 그래야 아래 SQL 비교 테스트가 낡은 기준으로 통과하는
        일을 막을 수 있다."""
        self.assertEqual(set(self.py_calls.keys()), set(EXPECTED_RPC_PARAMS.keys()))
        for fn_name, expected_params in EXPECTED_RPC_PARAMS.items():
            self.assertEqual(
                self.py_calls[fn_name], expected_params,
                f"{fn_name}: admin_service.py가 실제로 보내는 파라미터가 예상과 다름",
            )

    def test_every_python_called_rpc_exists_in_sql_with_matching_params(self):
        offenders = []
        for fn_name, py_params in self.py_calls.items():
            try:
                sql_params = _extract_sql_function_params(self.sql, fn_name)
            except AssertionError as e:
                offenders.append(str(e))
                continue
            if sql_params != py_params:
                offenders.append(
                    f"{fn_name}: SQL={sorted(sql_params)} vs Python={sorted(py_params)}"
                )
        self.assertEqual(offenders, [], f"SQL↔Python 파라미터 불일치: {offenders}")

    def test_every_public_rpc_has_admin_required_check(self):
        for fn_name in EXPECTED_RPC_PARAMS:
            pattern = rf"create or replace function public\.{fn_name}\s*\(.*?\$\$;"
            match = re.search(pattern, self.sql, re.IGNORECASE | re.DOTALL)
            self.assertIsNotNone(match, f"{fn_name} 함수 본문을 찾지 못함")
            self.assertIn(
                "fn_is_admin()", match.group(0),
                f"{fn_name}이 fn_is_admin() 검사를 호출하지 않음",
            )

    def test_every_public_rpc_is_security_definer_with_fixed_search_path(self):
        for fn_name in EXPECTED_RPC_PARAMS:
            pattern = rf"create or replace function public\.{fn_name}\s*\(.*?\$\$;"
            match = re.search(pattern, self.sql, re.IGNORECASE | re.DOTALL)
            body = match.group(0)
            self.assertIn("security definer", body.lower(), f"{fn_name}: security definer 아님")
            self.assertIn("set search_path = public", body.lower(), f"{fn_name}: search_path 미고정")

    def test_every_public_rpc_grants_execute_to_authenticated_only(self):
        for fn_name in EXPECTED_RPC_PARAMS:
            self.assertIn(
                f"grant execute on function public.{fn_name}", self.sql,
                f"{fn_name}: authenticated에게 EXECUTE 부여 문장을 찾지 못함",
            )
            # revoke ... from anon 문장이 함수 본문 종료($$;) 직후 근처에 있는지 확인
            # (함수 정의 시작이 아니라 끝을 기준으로 찾아야 함 — 본문이 길 수 있음).
            body_end = re.search(
                rf"create or replace function public\.{fn_name}\s*\(.*?\$\$;",
                self.sql, re.IGNORECASE | re.DOTALL,
            )
            nearby = self.sql[body_end.end(): body_end.end() + 400]
            self.assertIn("revoke execute", nearby, f"{fn_name}: anon revoke 문장을 근처에서 찾지 못함")

    def test_notify_pgrst_reload_schema_present(self):
        """PostgREST 스키마 캐시 갱신 — 이 파일을 실행한 직후 새/변경된 함수를
        바로 호출할 수 있으려면 반드시 필요하다(PGRST202 재발 방지)."""
        self.assertIn("NOTIFY pgrst, 'reload schema';", self.sql)
        # 파일의 뒷부분(마지막 실행 문장 근처)에 있어야 의미가 있다.
        idx = self.sql.rfind("NOTIFY pgrst, 'reload schema';")
        self.assertGreater(idx, len(self.sql) * 0.9, "NOTIFY 문장이 파일 끝부분에 있어야 함")

    def test_no_destructive_statements(self):
        sql_lower = self.sql.lower()
        for keyword in ("drop table ", "truncate ", "delete from app_users", "delete from auth.users"):
            self.assertNotIn(keyword, sql_lower, f"파괴적 SQL 발견: {keyword}")


if __name__ == "__main__":
    unittest.main()
