# core/shared_message_coordinator.py 테스트 — Tk/네트워크 완전 비의존 순수 로직.

import unittest

from core.shared_message_coordinator import (
    MAX_MESSAGE_NO,
    MIN_MESSAGE_NO,
    MessageSyncStatus,
    RemoteMessageSnapshot,
    SharedMessageCoordinator,
    should_apply_remote_event,
)


def _snap(message_no=1, content="hello", revision=2, **kwargs):
    return RemoteMessageSnapshot(message_no=message_no, content=content, revision=revision, **kwargs)


class TestShouldApplyRemoteEvent(unittest.TestCase):
    def test_higher_revision_applied(self):
        self.assertTrue(should_apply_remote_event(local_revision=1, event_revision=2))

    def test_equal_revision_ignored(self):
        self.assertFalse(should_apply_remote_event(local_revision=2, event_revision=2))

    def test_lower_revision_ignored(self):
        self.assertFalse(should_apply_remote_event(local_revision=3, event_revision=2))


class TestMessageNoRange(unittest.TestCase):
    def test_coordinator_covers_exactly_1_to_12(self):
        coord = SharedMessageCoordinator()
        numbers = [s.message_no for s in coord.all_states()]
        self.assertEqual(numbers, list(range(MIN_MESSAGE_NO, MAX_MESSAGE_NO + 1)))
        self.assertEqual(len(numbers), 12)


class TestApplyRemoteEvent(unittest.TestCase):
    def test_first_event_applied_even_at_revision_1(self):
        coord = SharedMessageCoordinator()
        applied = coord.apply_remote_event(_snap(message_no=1, revision=1, content="first"))
        self.assertTrue(applied)
        self.assertEqual(coord.get_state(1).content, "first")
        self.assertEqual(coord.get_state(1).revision, 1)
        self.assertEqual(coord.get_state(1).status, MessageSyncStatus.SYNCED)

    def test_higher_revision_event_applied(self):
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=1, content="v1"))
        applied = coord.apply_remote_event(_snap(message_no=1, revision=2, content="v2"))
        self.assertTrue(applied)
        self.assertEqual(coord.get_state(1).content, "v2")

    def test_equal_revision_event_ignored_not_applied(self):
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=2, content="v2"))
        applied = coord.apply_remote_event(_snap(message_no=1, revision=2, content="duplicate-or-echo"))
        self.assertFalse(applied)
        self.assertEqual(coord.get_state(1).content, "v2")  # 바뀌지 않음

    def test_lower_revision_event_ignored(self):
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=3, content="v3"))
        applied = coord.apply_remote_event(_snap(message_no=1, revision=1, content="stale"))
        self.assertFalse(applied)
        self.assertEqual(coord.get_state(1).content, "v3")

    def test_unknown_message_no_ignored_gracefully(self):
        coord = SharedMessageCoordinator()
        applied = coord.apply_remote_event(_snap(message_no=99, revision=1))
        self.assertFalse(applied)


class TestEditingDefersRemoteEvent(unittest.TestCase):
    """요구사항 10절 — 편집 중인 메시지는 즉시 덮어쓰지 않고 보류한다."""

    def test_event_during_edit_is_deferred_not_applied(self):
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=1, content="v1"))
        coord.begin_edit(1)

        applied = coord.apply_remote_event(_snap(message_no=1, revision=2, content="v2-from-other-user"))
        self.assertFalse(applied)
        self.assertEqual(coord.get_state(1).content, "v1")  # 화면 텍스트는 안 바뀜
        self.assertEqual(coord.get_state(1).status, MessageSyncStatus.REMOTE_UPDATED)
        self.assertIsNotNone(coord.get_state(1).pending_remote)
        self.assertEqual(coord.get_state(1).pending_remote.content, "v2-from-other-user")

    def test_event_when_not_editing_applied_immediately(self):
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=1, content="v1"))
        applied = coord.apply_remote_event(_snap(message_no=1, revision=2, content="v2"))
        self.assertTrue(applied)
        self.assertEqual(coord.get_state(1).content, "v2")

    def test_load_latest_and_discard_edit_resolves_deferred_event(self):
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=1, content="v1"))
        coord.begin_edit(1)
        coord.apply_remote_event(_snap(message_no=1, revision=2, content="v2"))

        result = coord.load_latest_and_discard_edit(1)
        self.assertIsNotNone(result)
        self.assertEqual(coord.get_state(1).content, "v2")
        self.assertEqual(coord.get_state(1).status, MessageSyncStatus.SYNCED)
        self.assertFalse(coord.get_state(1).is_editing)
        self.assertIsNone(coord.get_state(1).pending_remote)

    def test_keep_local_and_discard_remote_preserves_edit_text(self):
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=1, content="v1"))
        coord.begin_edit(1)
        coord.apply_remote_event(_snap(message_no=1, revision=2, content="v2"))

        coord.keep_local_and_discard_remote(1)
        self.assertIsNone(coord.get_state(1).pending_remote)
        self.assertTrue(coord.get_state(1).is_editing)
        # base_revision은 여전히 1(오래된 값)이라 저장 시 서버가 충돌로 거부하게 된다.
        self.assertEqual(coord.get_state(1).base_revision, 1)


class TestSaveLifecycle(unittest.TestCase):
    def test_mark_saving_sets_status(self):
        coord = SharedMessageCoordinator()
        coord.mark_saving(1)
        self.assertEqual(coord.get_state(1).status, MessageSyncStatus.SAVING)

    def test_mark_saved_bumps_local_revision_and_clears_editing(self):
        coord = SharedMessageCoordinator()
        coord.begin_edit(1)
        coord.mark_saving(1)
        coord.mark_saved(1, _snap(message_no=1, revision=2, content="saved-content"))

        state = coord.get_state(1)
        self.assertEqual(state.revision, 2)
        self.assertEqual(state.content, "saved-content")
        self.assertEqual(state.status, MessageSyncStatus.SYNCED)
        self.assertFalse(state.is_editing)
        self.assertIsNone(state.base_revision)

    def test_saved_revision_makes_own_echo_ignored(self):
        """요구사항 6절 — 저장 성공 후 도착하는 Realtime 에코가 중복 반영되지 않아야 한다."""
        coord = SharedMessageCoordinator()
        coord.mark_saved(1, _snap(message_no=1, revision=5, content="my-save"))

        echo_applied = coord.apply_remote_event(_snap(message_no=1, revision=5, content="my-save"))
        self.assertFalse(echo_applied, "자신이 저장한 내용의 Realtime 에코가 다시 적용되면 안 됨")

    def test_mark_conflict_sets_status(self):
        coord = SharedMessageCoordinator()
        coord.mark_conflict(1)
        self.assertEqual(coord.get_state(1).status, MessageSyncStatus.CONFLICT)

    def test_mark_offline_pending_sets_status(self):
        coord = SharedMessageCoordinator()
        coord.mark_offline_pending(1)
        self.assertEqual(coord.get_state(1).status, MessageSyncStatus.OFFLINE_PENDING)


class TestReconnection(unittest.TestCase):
    def test_mark_all_reconnecting_sets_all_except_conflict(self):
        coord = SharedMessageCoordinator()
        coord.mark_conflict(3)
        coord.mark_all_reconnecting()

        for state in coord.all_states():
            if state.message_no == 3:
                self.assertEqual(state.status, MessageSyncStatus.CONFLICT, "충돌 상태는 재연결 표시로 덮이면 안 됨")
            else:
                self.assertEqual(state.status, MessageSyncStatus.RECONNECTING)

    def test_apply_full_snapshot_recovers_missed_events(self):
        """요구사항 7절 — 재연결 직후 전체 재조회로 누락된 변경을 복구한다."""
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=1, content="old"))
        coord.apply_remote_event(_snap(message_no=2, revision=1, content="unchanged"))

        # 오프라인 동안 서버에서 message_no=1이 revision=5까지 여러 번 바뀌었다고 가정.
        snapshots = [
            _snap(message_no=n, revision=(5 if n == 1 else 1), content=("recovered" if n == 1 else "unchanged"))
            for n in range(MIN_MESSAGE_NO, MAX_MESSAGE_NO + 1)
        ]
        applied = coord.apply_full_snapshot(snapshots)

        self.assertIn(1, applied)
        self.assertNotIn(2, applied, "변경 없는 메시지는 재적용(불필요한 갱신)하지 않아야 함")
        self.assertEqual(coord.get_state(1).content, "recovered")
        self.assertEqual(coord.get_state(1).revision, 5)

    def test_apply_full_snapshot_defers_for_editing_message(self):
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=1, content="v1"))
        coord.begin_edit(1)

        snapshots = [_snap(message_no=1, revision=2, content="v2-from-server")]
        applied = coord.apply_full_snapshot(snapshots)

        self.assertEqual(applied, [])
        self.assertEqual(coord.get_state(1).content, "v1")
        self.assertEqual(coord.get_state(1).status, MessageSyncStatus.REMOTE_UPDATED)


class TestBeginEndEdit(unittest.TestCase):
    def test_begin_edit_captures_base_revision(self):
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=4, content="x"))
        coord.begin_edit(1)
        self.assertEqual(coord.get_state(1).base_revision, 4)
        self.assertTrue(coord.get_state(1).is_editing)

    def test_end_edit_clears_is_editing_but_keeps_base_revision(self):
        """자동저장이 blur 이후(2초 디바운스) 실행될 수 있으므로 base_revision은
        저장 성공 전까지 살아있어야 한다."""
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=4, content="x"))
        coord.begin_edit(1)
        coord.end_edit(1)
        self.assertFalse(coord.get_state(1).is_editing)
        self.assertEqual(coord.get_state(1).base_revision, 4)

    def test_discard_edit_clears_base_revision_and_pending_remote(self):
        coord = SharedMessageCoordinator()
        coord.apply_remote_event(_snap(message_no=1, revision=1, content="v1"))
        coord.begin_edit(1)
        coord.apply_remote_event(_snap(message_no=1, revision=2, content="v2"))

        coord.discard_edit(1)
        state = coord.get_state(1)
        self.assertFalse(state.is_editing)
        self.assertIsNone(state.base_revision)
        self.assertIsNone(state.pending_remote)
        self.assertEqual(state.status, MessageSyncStatus.SYNCED)


if __name__ == "__main__":
    unittest.main()
