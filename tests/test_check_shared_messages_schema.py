# scripts/check_shared_messages_schema.py 단위 테스트
#
# 실제 Supabase에는 절대 연결하지 않는다 — fake client/OpenAPI 문서만 사용한다.
# 이 스크립트가 SELECT 이외의 어떤 쓰기도 하지 않는다는 것은 소스 코드에 INSERT/
# UPDATE/DELETE/rpc(...) 호출이 없다는 정적 검사로도 별도 확인한다.
#
# 실행: python -m unittest tests.test_check_shared_messages_schema -v

import ast
import os
import unittest

from scripts.check_shared_messages_schema import (
    CheckResult,
    check_columns,
    check_message_rows,
    check_rpc_exists,
    check_select_access,
    check_table_exists,
    mask_host,
)


class _FakeQuery:
    def __init__(self, data=None, error: Exception = None):
        self._data = data
        self._error = error

    def select(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    def execute(self):
        if self._error is not None:
            raise self._error
        from types import SimpleNamespace
        return SimpleNamespace(data=self._data)


class _FakeClient:
    def __init__(self, table_data=None, table_error=None):
        self._data = table_data
        self._error = table_error

    def table(self, name):
        return _FakeQuery(self._data, self._error)


class _MissingTableError(Exception):
    code = "PGRST205"
    message = "Could not find the table 'public.shared_messages' in the schema cache"

    def __str__(self):
        return self.message


class TestMaskHost(unittest.TestCase):
    def test_masks_long_prefix(self):
        self.assertEqual(mask_host("https://abcdefgh1234.supabase.co"), "abcd********.supabase.co")

    def test_empty_url(self):
        self.assertEqual(mask_host(""), "")


class TestCheckTableExists(unittest.TestCase):
    def test_pass_when_query_succeeds(self):
        client = _FakeClient(table_data=[])
        result = check_table_exists(client, "shared_messages")
        self.assertEqual(result.level, "PASS")

    def test_fail_when_relation_missing(self):
        client = _FakeClient(table_error=_MissingTableError())
        result = check_table_exists(client, "shared_messages")
        self.assertEqual(result.level, "FAIL")

    def test_pass_when_other_error_occurs(self):
        """RLS 거부 등 "테이블은 있지만 접근 방식 문제"인 경우는 FAIL이 아니라
        PASS로 취급한다 — 이 함수는 오직 테이블 존재 여부만 확인한다."""
        client = _FakeClient(table_error=RuntimeError("some other error"))
        result = check_table_exists(client, "shared_messages")
        self.assertEqual(result.level, "PASS")


class TestCheckRpcExists(unittest.TestCase):
    def test_pass_when_rpc_in_openapi_paths(self):
        paths = {"/rpc/update_shared_message": {}}
        result = check_rpc_exists(paths, "update_shared_message")
        self.assertEqual(result.level, "PASS")

    def test_fail_when_rpc_missing(self):
        paths = {"/rpc/other_function": {}}
        result = check_rpc_exists(paths, "update_shared_message")
        self.assertEqual(result.level, "FAIL")

    def test_warn_when_openapi_unavailable(self):
        result = check_rpc_exists(None, "update_shared_message")
        self.assertEqual(result.level, "WARN")


class TestCheckColumns(unittest.TestCase):
    def test_pass_when_all_columns_present(self):
        definitions = {
            "shared_messages": {"properties": {
                "id": {}, "message_no": {}, "title": {}, "content": {}, "revision": {},
                "is_active": {}, "updated_at": {}, "updated_by": {}, "updated_by_name": {},
                "update_source": {}, "created_at": {},
            }},
        }
        result = check_columns(definitions, "shared_messages")
        self.assertEqual(result.level, "PASS")

    def test_fail_when_column_missing(self):
        definitions = {"shared_messages": {"properties": {"id": {}, "message_no": {}}}}
        result = check_columns(definitions, "shared_messages")
        self.assertEqual(result.level, "FAIL")
        self.assertIn("content", result.detail)

    def test_warn_when_openapi_unavailable(self):
        result = check_columns(None, "shared_messages")
        self.assertEqual(result.level, "WARN")

    def test_fail_when_table_missing_from_definitions(self):
        result = check_columns({}, "shared_messages")
        self.assertEqual(result.level, "FAIL")


class TestCheckMessageRows(unittest.TestCase):
    def test_warn_when_rows_none(self):
        results = check_message_rows(None)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].level, "WARN")

    def test_pass_when_all_12_present_no_duplicates_valid_revisions(self):
        rows = [{"message_no": n, "revision": 1} for n in range(1, 13)]
        results = check_message_rows(rows)
        self.assertTrue(all(r.level == "PASS" for r in results))
        self.assertEqual(len(results), 3)

    def test_fail_when_missing_numbers(self):
        rows = [{"message_no": n, "revision": 1} for n in range(1, 12)]  # 12 빠짐
        results = check_message_rows(rows)
        self.assertEqual(results[0].level, "FAIL")
        self.assertIn("12", results[0].detail)

    def test_fail_when_duplicate_message_no(self):
        rows = [{"message_no": n, "revision": 1} for n in range(1, 12)] + [{"message_no": 1, "revision": 1}]
        results = check_message_rows(rows)
        dup_result = results[1]
        self.assertEqual(dup_result.level, "FAIL")

    def test_fail_when_revision_not_positive(self):
        rows = [{"message_no": n, "revision": 1} for n in range(1, 13)]
        rows[0]["revision"] = 0
        results = check_message_rows(rows)
        revision_result = results[2]
        self.assertEqual(revision_result.level, "FAIL")


class TestCheckSelectAccess(unittest.TestCase):
    def test_warn_without_access_token(self):
        result = check_select_access(None, has_access_token=False)
        self.assertEqual(result.level, "WARN")

    def test_pass_with_access_token_and_rows(self):
        result = check_select_access([], has_access_token=True)
        self.assertEqual(result.level, "PASS")

    def test_fail_with_access_token_but_no_rows(self):
        result = check_select_access(None, has_access_token=True)
        self.assertEqual(result.level, "FAIL")


class TestScriptNeverWrites(unittest.TestCase):
    """정적 검사 — 이 스크립트가 Supabase 클라이언트로 INSERT/UPDATE/DELETE/RPC를
    실행하지 않아야 한다(운영 DB를 절대 변경하지 않는다는 요구사항의 코드 수준
    보장). AST의 메서드 체인을 따라가 그 호출의 "루트"가 client/table(...)로
    시작하는 경우만 검사한다 — sys.path.insert(...)처럼 Supabase와 무관한 동명
    메서드(list.insert 등)는 오탐이므로 제외한다."""

    _SUPABASE_WRITE_METHODS = ("insert", "update", "delete", "upsert")

    def test_no_supabase_write_or_rpc_calls_in_source(self):
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "check_shared_messages_schema.py",
        )
        with open(script_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)

        findings = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method_name = node.func.attr
            root = self._chain_root(node.func.value)
            if method_name in self._SUPABASE_WRITE_METHODS and root in ("client",):
                findings.append(method_name)
            if method_name == "rpc" and root in ("client",):
                findings.append(method_name)

        self.assertEqual(findings, [], f"Supabase 쓰기/RPC 실행 호출 발견: {findings}")

    @staticmethod
    def _chain_root(node):
        """a.b.c(...) 형태의 체인에서 가장 왼쪽 이름(a)을 찾는다."""
        while isinstance(node, ast.Attribute):
            node = node.value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            return TestScriptNeverWrites._chain_root(node.func.value)
        if isinstance(node, ast.Name):
            return node.id
        return None


if __name__ == "__main__":
    unittest.main()
