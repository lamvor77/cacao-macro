# docs/sql/shared_messages_realtime.sql의 보안 관련 정적 속성을 검사한다.
#
# 실제 Supabase에는 절대 연결하지 않는다(이 스프린트/프로젝트 전체 원칙) — SQL
# 텍스트 자체를 정적으로 검사해 "이 파일을 그대로 적용하면 지켜질 규칙"을
# 회귀 방지 차원에서 고정한다. 진짜 RLS 동작 자체는 실제 Supabase 프로젝트에
# 적용한 뒤 수동으로 최종 검증해야 한다(요구사항 17절 — "실제 Supabase 운영 DB를
# 호출하지 마세요"의 자연스러운 한계).
#
# 실행: python -m unittest tests.test_shared_messages_sql_security -v

import os
import re
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_PATH = os.path.join(PROJECT_ROOT, "docs", "sql", "shared_messages_realtime.sql")


class TestSharedMessagesSqlSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SQL_PATH, "r", encoding="utf-8") as f:
            cls.sql = f.read()
        cls.sql_lower = cls.sql.lower()

    def test_file_exists(self):
        self.assertTrue(os.path.exists(SQL_PATH))

    def test_no_service_role_usage(self):
        self.assertNotIn("service_role", self.sql_lower)

    def test_rls_enabled_on_both_tables(self):
        self.assertIn("alter table public.shared_messages enable row level security", self.sql_lower)
        self.assertIn("alter table public.shared_message_history enable row level security", self.sql_lower)

    def test_no_force_row_level_security(self):
        # phase2b_schema.sql과 동일한 원칙 — FORCE RLS를 걸면 SECURITY DEFINER
        # 함수의 "소유자는 우회" 전제가 깨진다.
        self.assertNotIn("force row level security", self.sql_lower)

    def test_shared_messages_has_no_client_insert_or_delete_policy(self):
        """shared_messages는 INSERT/DELETE 정책이 없어야 한다(요구사항 12/20절 —
        메시지 13번 이상 생성 금지, 삭제 금지)."""
        insert_policies = re.findall(
            r"create policy\s+\S+\s+on public\.shared_messages for insert", self.sql_lower,
        )
        delete_policies = re.findall(
            r"create policy\s+\S+\s+on public\.shared_messages for delete", self.sql_lower,
        )
        self.assertEqual(insert_policies, [])
        self.assertEqual(delete_policies, [])

    def test_shared_message_history_has_no_client_write_policy(self):
        """shared_message_history는 INSERT/UPDATE/DELETE 정책이 전혀 없어야 한다 —
        RPC(SECURITY DEFINER)만 쓸 수 있다."""
        for verb in ("insert", "update", "delete"):
            policies = re.findall(
                rf"create policy\s+\S+\s+on public\.shared_message_history for {verb}", self.sql_lower,
            )
            self.assertEqual(policies, [], f"shared_message_history에 클라이언트 {verb} 정책이 있으면 안 됨")

    def test_rpcs_revoke_anon_execute(self):
        self.assertIn(
            "revoke execute on function public.update_shared_message(integer, text, text, bigint, "
            "public.shared_message_source) from anon",
            self.sql_lower,
        )
        self.assertIn(
            "revoke execute on function public.force_update_shared_message(integer, text, text, "
            "public.shared_message_source) from anon",
            self.sql_lower,
        )

    def test_rpcs_are_security_definer_with_fixed_search_path(self):
        update_fn_match = re.search(
            r"create or replace function public\.update_shared_message.*?\$\$;", self.sql_lower, re.DOTALL,
        )
        self.assertIsNotNone(update_fn_match)
        self.assertIn("security definer", update_fn_match.group(0))
        self.assertIn("set search_path = public", update_fn_match.group(0))

        force_fn_match = re.search(
            r"create or replace function public\.force_update_shared_message.*?\$\$;", self.sql_lower, re.DOTALL,
        )
        self.assertIsNotNone(force_fn_match)
        self.assertIn("security definer", force_fn_match.group(0))
        self.assertIn("set search_path = public", force_fn_match.group(0))

    def test_update_rpc_checks_permission_before_mutation(self):
        fn_match = re.search(
            r"create or replace function public\.update_shared_message.*?\$\$;", self.sql, re.DOTALL,
        )
        self.assertIsNotNone(fn_match)
        body = fn_match.group(0)
        permission_check_pos = body.find("fn_can_edit()")
        update_pos = body.find("update public.shared_messages")
        self.assertGreater(permission_check_pos, -1)
        self.assertGreater(update_pos, -1)
        self.assertLess(permission_check_pos, update_pos, "권한 검사가 실제 UPDATE보다 먼저 나와야 함")

    def test_force_update_rpc_requires_admin(self):
        fn_match = re.search(
            r"create or replace function public\.force_update_shared_message.*?\$\$;", self.sql, re.DOTALL,
        )
        self.assertIsNotNone(fn_match)
        self.assertIn("fn_is_admin()", fn_match.group(0))

    def test_update_rpc_only_allows_desktop_and_mobile_sources(self):
        """Production Stabilization Sprint — migration/admin_force는 이제
        force_update_shared_message 전용이다. 일반 저장 RPC로는 지정할 수 없다."""
        fn_match = re.search(
            r"create or replace function public\.update_shared_message.*?\$\$;", self.sql_lower, re.DOTALL,
        )
        body = fn_match.group(0)
        self.assertIn("not in ('desktop', 'mobile')", body)
        self.assertNotIn("'migration'", body)

    def test_force_update_rpc_only_allows_migration_and_admin_force_sources(self):
        fn_match = re.search(
            r"create or replace function public\.force_update_shared_message.*?\$\$;", self.sql_lower, re.DOTALL,
        )
        body = fn_match.group(0)
        self.assertIn("not in ('migration', 'admin_force')", body)

    def test_admin_force_is_a_valid_update_source_enum_value(self):
        self.assertIn("'admin_force'", self.sql)

    def test_revision_positive_check_constraint_exists(self):
        self.assertIn("check (revision > 0)", self.sql_lower)

    def test_message_no_has_explicit_unique_constraint(self):
        self.assertIn("shared_messages_message_no_unique unique (message_no)", self.sql_lower)

    def test_shared_messages_has_no_client_update_policy(self):
        """Production Stabilization Sprint에서 발견/수정한 핵심 취약점 — 클라이언트가
        RPC를 우회해 직접 UPDATE할 수 있는 정책이 있어서는 안 된다."""
        update_policies = re.findall(
            r"create policy\s+\S+\s+on public\.shared_messages for update", self.sql_lower,
        )
        self.assertEqual(update_policies, [], "shared_messages에 클라이언트 UPDATE 정책이 있으면 RPC를 우회할 수 있음")

    def test_preflight_checklist_and_rollback_section_present(self):
        self.assertIn("적용 전 확인사항", self.sql)
        self.assertIn("롤백", self.sql)

    def test_message_no_range_constrained_at_table_level(self):
        self.assertIn("check (message_no between 1 and 12)", self.sql_lower)

    def test_reuses_existing_permission_helpers_not_new_role_system(self):
        """요구사항 11/12절 — 새 사용자/역할 테이블을 만들지 않고 기존
        app_users/fn_is_approved/fn_can_edit/fn_is_admin을 재사용해야 한다."""
        self.assertIn("public.app_users", self.sql_lower)
        self.assertIn("fn_is_approved()", self.sql_lower)
        self.assertIn("fn_can_edit()", self.sql_lower)
        self.assertIn("fn_is_admin()", self.sql_lower)
        self.assertNotIn("create table if not exists public.mobile_users", self.sql_lower)

    def test_does_not_modify_legacy_messages_table(self):
        """레거시 messages/message_history 테이블 정의를 다시 만들거나 변경하지
        않아야 한다(요구사항 12 — 기존 기능 유지)."""
        self.assertNotIn("create table if not exists public.messages ", self.sql_lower)
        self.assertNotIn("alter table public.messages ", self.sql_lower)

    def test_replica_identity_full_for_realtime_payload_completeness(self):
        self.assertIn("alter table public.shared_messages replica identity full", self.sql_lower)


if __name__ == "__main__":
    unittest.main()
