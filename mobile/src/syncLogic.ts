// PC 쪽 core/shared_message_coordinator.py와 동일한 규칙을 모바일에서도 그대로
// 적용한다(요구사항 6절 — "revision이 로컬보다 큰 경우에만 반영"). Supabase나
// React를 import하지 않는 순수 로직이라 vitest로 독립적으로 테스트한다.

export type UpdateSource = "desktop" | "mobile" | "migration" | "system";

export type MessageSyncStatus =
  | "synced"
  | "saving"
  | "offline_pending"
  | "conflict"
  | "reconnecting"
  | "remote_updated";

export const STATUS_LABELS_KO: Record<MessageSyncStatus, string> = {
  synced: "동기화됨",
  saving: "저장 중",
  offline_pending: "오프라인 변경",
  conflict: "충돌",
  reconnecting: "재연결 중",
  remote_updated: "다른 직원이 수정함",
};

export interface RemoteMessageSnapshot {
  messageNo: number;
  content: string;
  revision: number;
  title: string | null;
  updatedByName: string;
  updatedAt: string;
  updateSource: string;
}

export interface MessageSyncState {
  messageNo: number;
  content: string;
  title: string | null;
  revision: number;
  baseRevision: number | null;
  isEditing: boolean;
  status: MessageSyncStatus;
  updatedByName: string;
  updatedAt: string;
  pendingRemote: RemoteMessageSnapshot | null;
}

export const MIN_MESSAGE_NO = 1;
export const MAX_MESSAGE_NO = 12;

export function shouldApplyRemoteEvent(localRevision: number, eventRevision: number): boolean {
  return eventRevision > localRevision;
}

export function createInitialState(messageNo: number): MessageSyncState {
  return {
    messageNo,
    content: "",
    title: null,
    revision: 0,
    baseRevision: null,
    isEditing: false,
    status: "synced",
    updatedByName: "",
    updatedAt: "",
    pendingRemote: null,
  };
}

export function createInitialStates(): Record<number, MessageSyncState> {
  const states: Record<number, MessageSyncState> = {};
  for (let n = MIN_MESSAGE_NO; n <= MAX_MESSAGE_NO; n++) {
    states[n] = createInitialState(n);
  }
  return states;
}

/** 단건 원격 이벤트 반영. 편집 중이면 즉시 덮어쓰지 않고 pendingRemote로 보류한다
 * (요구사항 10절) — 반환값은 "화면을 즉시 갱신해야 하는지" 여부다. */
export function applyRemoteEvent(state: MessageSyncState, snap: RemoteMessageSnapshot): MessageSyncState {
  if (!shouldApplyRemoteEvent(state.revision, snap.revision)) {
    return state; // 자신의 에코 또는 오래된 이벤트 — 무시
  }
  if (state.isEditing) {
    return { ...state, pendingRemote: snap, status: "remote_updated" };
  }
  return applySnapshot(state, snap);
}

function applySnapshot(state: MessageSyncState, snap: RemoteMessageSnapshot): MessageSyncState {
  return {
    ...state,
    content: snap.content,
    title: snap.title,
    revision: snap.revision,
    updatedByName: snap.updatedByName,
    updatedAt: snap.updatedAt,
    status: "synced",
    pendingRemote: null,
  };
}

export function beginEdit(state: MessageSyncState): MessageSyncState {
  return { ...state, isEditing: true, baseRevision: state.revision };
}

/** blur 시 호출 — baseRevision은 유지한다(저장 버튼을 누르기 전까지 필요). */
export function endEdit(state: MessageSyncState): MessageSyncState {
  return { ...state, isEditing: false };
}

export function discardEdit(state: MessageSyncState): MessageSyncState {
  return {
    ...state,
    isEditing: false,
    baseRevision: null,
    pendingRemote: null,
    status: state.status === "conflict" ? "conflict" : "synced",
  };
}

export function markSaving(state: MessageSyncState): MessageSyncState {
  return { ...state, status: "saving" };
}

export function markSaved(state: MessageSyncState, snap: RemoteMessageSnapshot): MessageSyncState {
  return { ...applySnapshot(state, snap), isEditing: false, baseRevision: null };
}

export function markConflict(state: MessageSyncState): MessageSyncState {
  return { ...state, status: "conflict" };
}

export function loadLatestAndDiscardEdit(state: MessageSyncState): MessageSyncState {
  if (state.pendingRemote === null) return state;
  return { ...applySnapshot(state, state.pendingRemote), isEditing: false, baseRevision: null };
}

export function keepLocalAndDiscardRemote(state: MessageSyncState): MessageSyncState {
  return { ...state, pendingRemote: null, status: state.isEditing ? "offline_pending" : state.status };
}
