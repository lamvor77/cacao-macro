# Phase 4-2: 운영 관리자 창(gui/panels/operations_admin_panel.py) 테스트
#
# 이 프로젝트는 지금까지 실제 Tkinter 위젯을 자동화 테스트한 적이 없다(디스플레이
# 의존 테스트의 불안정성 때문). 그래서 대부분의 판단 로직은 이미
# tests/test_admin_ui_permissions.py에서 gui/panels/admin_ui_state.py(Tk 비의존)만
# 검증했다. 이 파일은 나머지 두 가지만 다룬다:
#   1) AdminRequestSequencer(비동기 응답 순서/중복 실행 방지) — 역시 Tk 비의존, 순수 로직.
#   2) 실제 OperationsAdminPanel을 FakeAdminService로 한 번 생성해 보는 통합
#      스모크 테스트 — 이 환경은 실제 Tk 루트를 만들 수 있음을 확인했으므로(개발 중
#      확인됨), 최소 1개는 실제 위젯 생성 자체가 깨지지 않는지 검증한다. 다만
#      worker thread에서의 self.after() 호출은 실제 mainloop()이 돌고 있어야
#      "main thread is not in main loop" 오류 없이 동작하므로(Tkinter의 스레드
#      안전장치), 이 테스트도 실제 mainloop()을 짧게 돌리는 방식으로 구성했다.
#
# 실제 Supabase/storage에는 절대 접근하지 않는다.
#
# 실행: python -m unittest tests.test_operations_admin_panel -v

import inspect
import os
import re
import sys
import threading
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from gui.panels.admin_ui_state import AdminRequestSequencer

from tests._admin_service_fakes import FakeAdminService, make_user


# ============================================================
# 23~25: AdminRequestSequencer — 순수 로직, Tk 비의존
# ============================================================

class TestRequestSequencer(unittest.TestCase):
    def test_24_stale_response_rejected_latest_accepted(self):
        """23/24. 검색 A 시작 → 검색 B 시작 → B 응답 먼저 도착(수락) → A 응답 나중 도착(거부)."""
        seq = AdminRequestSequencer()
        gen_a = seq.start()
        gen_b = seq.start()

        self.assertTrue(seq.accept(gen_b), "가장 최근 세대(B)는 항상 수락되어야 함")
        seq.finish(gen_b)

        self.assertFalse(seq.accept(gen_a), "오래된 세대(A)의 응답은 B가 이미 최신이므로 거부되어야 함")
        seq.finish(gen_a)

    def test_23_in_flight_reflects_outstanding_requests(self):
        """패널은 `if seq.in_flight: return`으로 중복 실행을 막는다 — in_flight 플래그 자체를 검증."""
        seq = AdminRequestSequencer()
        self.assertFalse(seq.in_flight)

        gen = seq.start()
        self.assertTrue(seq.in_flight)

        seq.finish(gen)
        self.assertFalse(seq.in_flight)

    def test_25_disposed_sequencer_rejects_any_response(self):
        """창이 닫힌(dispose) 뒤에는 어떤 세대의 응답도 accept되지 않는다 — 죽은 위젯을
        건드리는 콜백을 막는다."""
        seq = AdminRequestSequencer()
        gen = seq.start()
        seq.dispose()

        self.assertFalse(seq.accept(gen), "dispose 이후에는 방금 시작한 요청이라도 거부되어야 함")

        # dispose 이후 start()가 다시 호출되더라도(방어적 상황) accept는 계속 거부되어야 한다.
        gen2 = seq.start()
        self.assertFalse(seq.accept(gen2))


# ============================================================
# 26~27: 직접 DB 호출 정적 점검
# ============================================================

class TestNoDirectDbAccess(unittest.TestCase):
    _GUI_FILES = [
        os.path.join(PROJECT_ROOT, "gui", "panels", "operations_admin_panel.py"),
        os.path.join(PROJECT_ROOT, "gui", "panels", "admin_ui_state.py"),
    ]
    # app_users/admin_audit_logs 같은 실제 테이블 이름 문자열이 코드에 등장하는 것 자체는
    # (주석에서 "이 테이블을 직접 안 쓴다"고 설명하는 용도로) 허용한다 — 여기서는
    # 실제 DB 호출로 이어지는 코드 패턴(.table(...)/.from_(...)/.rpc(...)/create_client(...))
    # 이 없는지만 검사한다. 이게 실제 보안 경계다.
    _FORBIDDEN_PATTERNS = [
        r"\.table\(",
        r"\.from_\(",
        r"\.rpc\(",
        r"create_client\(",
        r"service_role",
        r"SupabaseClientManager\(",  # 새 client_manager를 직접 만들면 안 됨 — 주입만 받는다
    ]

    def test_26_27_no_forbidden_db_patterns_in_gui_files(self):
        for path in self._GUI_FILES:
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
            for pattern in self._FORBIDDEN_PATTERNS:
                matches = re.findall(pattern, source)
                self.assertEqual(
                    matches, [],
                    f"{os.path.basename(path)}에서 금지된 패턴 발견: {pattern} ({len(matches)}건)",
                )

    def test_admin_service_methods_are_the_only_data_access(self):
        """operations_admin_panel.py가 self._admin.<메서드>() 형태로만 데이터에 접근하는지
        (AdminService의 공개 메서드 이름 목록과 대조)."""
        from services.admin_service import AdminService
        allowed_methods = {
            name for name, _ in inspect.getmembers(AdminService, predicate=inspect.isfunction)
            if not name.startswith("_")
        }
        path = os.path.join(PROJECT_ROOT, "gui", "panels", "operations_admin_panel.py")
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        used_methods = set(re.findall(r"self\._admin\.(\w+)\(", source))
        self.assertTrue(used_methods, "self._admin.<method>() 호출이 하나도 없으면 검사 의미가 없음")
        self.assertTrue(
            used_methods.issubset(allowed_methods),
            f"AdminService에 없는 메서드를 호출하고 있음: {used_methods - allowed_methods}",
        )


# ============================================================
# 통합 스모크 테스트 — 실제 mainloop() 아래에서 FakeAdminService로 창을 만들어 본다
# ============================================================

class TestOperationsAdminPanelSmoke(unittest.TestCase):
    def test_panel_loads_users_via_fake_service_under_real_mainloop(self):
        """실제 Supabase는 전혀 쓰지 않는다(FakeAdminService만 주입) — 위젯 생성/최초
        비동기 조회/트리 채우기/종료 정리가 실제로 동작하는지 확인하는 통합 스모크 테스트.

        worker thread → self.after(0, ...) 패턴은 실제 mainloop()이 돌고 있어야
        Tkinter의 스레드 안전장치("main thread is not in main loop")에 걸리지 않는다
        (main_window.py의 기존 클라우드 동기화 스레드와 동일한 제약) — 그래서
        manual update() 대신 실제 mainloop()을 별도 checker 스레드로 짧게 종료시키는
        방식을 쓴다.
        """
        import customtkinter as ctk
        from gui.panels.operations_admin_panel import OperationsAdminPanel

        fake = FakeAdminService()
        fake.users_response = [make_user(id="u1", status="pending")]

        root = ctk.CTk()
        root.withdraw()
        result = {}
        try:
            panel = OperationsAdminPanel(root, fake, current_user_id="admin-uuid")

            def checker():
                deadline = time.time() + 5
                while time.time() < deadline:
                    if panel._tree.get_children():
                        break
                    time.sleep(0.02)
                result["children"] = panel._tree.get_children()
                result["list_users_called"] = any(name == "list_users" for name, _ in fake.calls)
                panel.dispose()
                root.after(0, root.quit)

            threading.Thread(target=checker, daemon=True).start()
            root.mainloop()
        finally:
            root.destroy()

        self.assertTrue(result.get("list_users_called"), "AdminService.list_users()가 호출되어야 함")
        self.assertEqual(result.get("children"), ("u1",), "fake가 반환한 사용자가 트리에 채워져야 함")


if __name__ == "__main__":
    unittest.main()
