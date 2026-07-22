// syncLogic.ts 테스트 — PC 쪽 tests/test_shared_message_coordinator.py와 대응한다.
import { describe, expect, it } from "vitest";
import {
  applyRemoteEvent, beginEdit, createInitialState, discardEdit, endEdit,
  keepLocalAndDiscardRemote, loadLatestAndDiscardEdit, markConflict, markSaved, markSaving,
  shouldApplyRemoteEvent,
  type RemoteMessageSnapshot,
} from "./syncLogic";

function snap(overrides: Partial<RemoteMessageSnapshot> = {}): RemoteMessageSnapshot {
  return {
    messageNo: 1, content: "hello", revision: 2, title: null,
    updatedByName: "", updatedAt: "", updateSource: "mobile",
    ...overrides,
  };
}

describe("shouldApplyRemoteEvent", () => {
  it("applies when event revision is higher", () => {
    expect(shouldApplyRemoteEvent(1, 2)).toBe(true);
  });
  it("ignores equal revision", () => {
    expect(shouldApplyRemoteEvent(2, 2)).toBe(false);
  });
  it("ignores lower revision", () => {
    expect(shouldApplyRemoteEvent(3, 2)).toBe(false);
  });
});

describe("applyRemoteEvent", () => {
  it("applies first event even at revision 1", () => {
    const state = createInitialState(1);
    const next = applyRemoteEvent(state, snap({ revision: 1, content: "first" }));
    expect(next.content).toBe("first");
    expect(next.revision).toBe(1);
    expect(next.status).toBe("synced");
  });

  it("ignores duplicate/echo events at equal revision", () => {
    let state = createInitialState(1);
    state = applyRemoteEvent(state, snap({ revision: 2, content: "v2" }));
    const next = applyRemoteEvent(state, snap({ revision: 2, content: "echo" }));
    expect(next.content).toBe("v2");
  });

  it("defers remote event while editing and marks remote_updated", () => {
    let state = createInitialState(1);
    state = applyRemoteEvent(state, snap({ revision: 1, content: "v1" }));
    state = beginEdit(state);
    const next = applyRemoteEvent(state, snap({ revision: 2, content: "v2-from-other" }));
    expect(next.content).toBe("v1"); // 화면(로컬) 텍스트는 안 바뀜
    expect(next.status).toBe("remote_updated");
    expect(next.pendingRemote?.content).toBe("v2-from-other");
  });

  it("applies immediately when not editing", () => {
    let state = createInitialState(1);
    state = applyRemoteEvent(state, snap({ revision: 1, content: "v1" }));
    const next = applyRemoteEvent(state, snap({ revision: 2, content: "v2" }));
    expect(next.content).toBe("v2");
  });

  it("과거(PC 등)에 입력된 title이 있는 기존 데이터도 오류 없이 반영된다 — 모바일이 title을 직접 다루지 않아도 됨", () => {
    const state = createInitialState(1);
    const next = applyRemoteEvent(state, snap({ revision: 1, content: "본문", title: "예전에 PC에서 입력한 제목" }));
    expect(next.content).toBe("본문");
    expect(next.title).toBe("예전에 PC에서 입력한 제목");
    expect(next.status).toBe("synced");
  });
});

describe("edit lifecycle", () => {
  it("beginEdit captures baseRevision", () => {
    let state = createInitialState(1);
    state = applyRemoteEvent(state, snap({ revision: 4 }));
    state = beginEdit(state);
    expect(state.baseRevision).toBe(4);
    expect(state.isEditing).toBe(true);
  });

  it("endEdit clears isEditing but keeps baseRevision", () => {
    let state = createInitialState(1);
    state = applyRemoteEvent(state, snap({ revision: 4 }));
    state = beginEdit(state);
    state = endEdit(state);
    expect(state.isEditing).toBe(false);
    expect(state.baseRevision).toBe(4);
  });

  it("discardEdit clears baseRevision and pendingRemote", () => {
    let state = createInitialState(1);
    state = applyRemoteEvent(state, snap({ revision: 1 }));
    state = beginEdit(state);
    state = applyRemoteEvent(state, snap({ revision: 2, content: "v2" }));
    state = discardEdit(state);
    expect(state.baseRevision).toBeNull();
    expect(state.pendingRemote).toBeNull();
    expect(state.status).toBe("synced");
  });
});

describe("save lifecycle", () => {
  it("markSaving sets status", () => {
    const state = markSaving(createInitialState(1));
    expect(state.status).toBe("saving");
  });

  it("markSaved bumps revision and clears editing", () => {
    let state = createInitialState(1);
    state = beginEdit(state);
    state = markSaving(state);
    state = markSaved(state, snap({ revision: 2, content: "saved" }));
    expect(state.revision).toBe(2);
    expect(state.content).toBe("saved");
    expect(state.status).toBe("synced");
    expect(state.isEditing).toBe(false);
  });

  it("own echo after save is ignored (no duplicate reflection)", () => {
    let state = createInitialState(1);
    state = markSaved(state, snap({ revision: 5, content: "my-save" }));
    const next = applyRemoteEvent(state, snap({ revision: 5, content: "my-save" }));
    expect(next).toBe(state); // 참조 동일 = 실제로 아무 것도 안 바뀜(적용 안 됨)
  });

  it("markConflict sets status", () => {
    const state = markConflict(createInitialState(1));
    expect(state.status).toBe("conflict");
  });
});

describe("conflict resolution choices", () => {
  it("loadLatestAndDiscardEdit resolves pending remote", () => {
    let state = createInitialState(1);
    state = applyRemoteEvent(state, snap({ revision: 1, content: "v1" }));
    state = beginEdit(state);
    state = applyRemoteEvent(state, snap({ revision: 2, content: "v2" }));
    state = loadLatestAndDiscardEdit(state);
    expect(state.content).toBe("v2");
    expect(state.status).toBe("synced");
    expect(state.isEditing).toBe(false);
  });

  it("keepLocalAndDiscardRemote preserves edit text but clears pendingRemote", () => {
    let state = createInitialState(1);
    state = applyRemoteEvent(state, snap({ revision: 1, content: "v1" }));
    state = beginEdit(state);
    state = applyRemoteEvent(state, snap({ revision: 2, content: "v2" }));
    state = keepLocalAndDiscardRemote(state);
    expect(state.pendingRemote).toBeNull();
    expect(state.content).toBe("v1");
    expect(state.baseRevision).toBe(1); // 여전히 오래된 값 -> 저장 시 서버가 다시 충돌 처리
  });
});
