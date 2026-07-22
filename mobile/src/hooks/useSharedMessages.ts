// shared_messages 조회/저장/실시간 구독을 한 곳에 모은 훅.
// PC 쪽 services/shared_message_service.py + services/realtime_message_sync_service.py +
// core/shared_message_coordinator.py 세 모듈의 역할을 모바일에서는 훅 하나로 합쳤다
// (모바일은 화면이 단순해 굳이 계층을 나눌 필요가 없다 — 요구사항 11절 "최소한의 기능").

import { useCallback, useEffect, useRef, useState } from "react";
import { supabase } from "../supabaseClient";
import type { SharedMessageRow } from "../types";
import type { MessageSyncState, RemoteMessageSnapshot } from "../syncLogic";
import {
  applyRemoteEvent, beginEdit as beginEditLogic, buildForceUpdateParams, buildUpdateParams,
  createInitialStates, discardEdit as discardEditLogic,
  endEdit as endEditLogic, keepLocalAndDiscardRemote as keepLocalLogic,
  loadLatestAndDiscardEdit as loadLatestLogic, markConflict, markSaved, markSaving,
} from "../syncLogic";

export type ConnectionStatus = "connecting" | "connected" | "reconnecting" | "disconnected";

function rowToSnapshot(row: SharedMessageRow): RemoteMessageSnapshot {
  return {
    messageNo: row.message_no,
    content: row.content,
    revision: row.revision,
    title: row.title,
    updatedByName: row.updated_by_name ?? "",
    updatedAt: row.updated_at,
    updateSource: row.update_source,
  };
}

export class SaveConflictError extends Error {}
export class SavePermissionError extends Error {}

export function useSharedMessages(enabled: boolean) {
  const [states, setStates] = useState<Record<number, MessageSyncState>>(createInitialStates);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("connecting");
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);
  const statesRef = useRef(states);
  statesRef.current = states;

  const applySnapshotList = useCallback((rows: SharedMessageRow[]) => {
    setStates((prev) => {
      const next = { ...prev };
      for (const row of rows) {
        const snap = rowToSnapshot(row);
        const current = next[row.message_no];
        if (current) next[row.message_no] = applyRemoteEvent(current, snap);
      }
      return next;
    });
    setLastUpdatedAt(new Date().toISOString());
  }, []);

  const refresh = useCallback(async () => {
    const { data, error } = await supabase.from("shared_messages").select("*").order("message_no");
    if (error) {
      // eslint-disable-next-line no-console
      console.warn("메시지 목록 조회 실패:", error.message);
      return;
    }
    applySnapshotList((data ?? []) as SharedMessageRow[]);
  }, [applySnapshotList]);

  // ===== Realtime 구독 =====
  useEffect(() => {
    if (!enabled) return;
    setConnectionStatus("connecting");

    const channel = supabase
      .channel("shared_messages_changes")
      .on(
        "postgres_changes",
        { event: "UPDATE", schema: "public", table: "shared_messages" },
        (payload) => {
          const row = payload.new as SharedMessageRow;
          setStates((prev) => {
            const current = prev[row.message_no];
            if (!current) return prev;
            return { ...prev, [row.message_no]: applyRemoteEvent(current, rowToSnapshot(row)) };
          });
        },
      )
      .subscribe((status) => {
        if (status === "SUBSCRIBED") {
          setConnectionStatus("connected");
          void refresh(); // 요구사항 7절 — 최초/재연결 시 전체 재조회로 누락 이벤트 복구
        } else if (status === "CHANNEL_ERROR" || status === "TIMED_OUT") {
          setConnectionStatus("reconnecting");
        } else if (status === "CLOSED") {
          setConnectionStatus("disconnected");
        }
      });

    return () => {
      void supabase.removeChannel(channel);
    };
  }, [enabled, refresh]);

  // 브라우저가 절전/백그라운드에서 복귀했을 때도 정합성을 재확인한다(요구사항 7절).
  useEffect(() => {
    if (!enabled) return;
    const onVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onVisible);
    };
  }, [enabled, refresh]);

  const beginEdit = useCallback((messageNo: number) => {
    setStates((prev) => ({ ...prev, [messageNo]: beginEditLogic(prev[messageNo]) }));
  }, []);

  const endEdit = useCallback((messageNo: number) => {
    setStates((prev) => ({ ...prev, [messageNo]: endEditLogic(prev[messageNo]) }));
  }, []);

  const discardEdit = useCallback((messageNo: number) => {
    setStates((prev) => ({ ...prev, [messageNo]: discardEditLogic(prev[messageNo]) }));
  }, []);

  const loadLatestAndDiscardEdit = useCallback((messageNo: number) => {
    setStates((prev) => ({ ...prev, [messageNo]: loadLatestLogic(prev[messageNo]) }));
  }, []);

  const keepLocalAndDiscardRemote = useCallback((messageNo: number) => {
    setStates((prev) => ({ ...prev, [messageNo]: keepLocalLogic(prev[messageNo]) }));
  }, []);

  // 모바일에서는 제목 입력 기능을 제공하지 않는다(요구사항) — RPC는 여전히
  // p_title 파라미터를 필수로 받으므로 항상 null을 보낸다. SQL의
  // update_shared_message가 "title = coalesce(p_title, title)"로 처리하므로,
  // null을 보내면 서버에 이미 저장된 title(과거 PC에서 입력된 값 등)이 그대로
  // 유지되고 지워지지 않는다 — DB 컬럼/기존 데이터는 건드리지 않는다.
  const saveMessage = useCallback(async (messageNo: number, content: string) => {
    const state = statesRef.current[messageNo];
    const baseRevision = state.baseRevision ?? state.revision;
    setStates((prev) => ({ ...prev, [messageNo]: markSaving(prev[messageNo]) }));

    const { data, error } = await supabase.rpc("update_shared_message", buildUpdateParams(messageNo, content, baseRevision));

    if (error) {
      const message = error.message ?? "";
      if (message.startsWith("REVISION_CONFLICT:")) {
        setStates((prev) => ({ ...prev, [messageNo]: markConflict(prev[messageNo]) }));
        throw new SaveConflictError(message.split(":").slice(1).join(":").trim());
      }
      if (message.startsWith("PERMISSION_DENIED:")) {
        throw new SavePermissionError(message.split(":").slice(1).join(":").trim());
      }
      throw new Error(message || "저장 중 오류가 발생했습니다.");
    }

    const row = data as SharedMessageRow;
    setStates((prev) => ({ ...prev, [messageNo]: markSaved(prev[messageNo], rowToSnapshot(row)) }));
  }, []);

  // 요구사항 10절 — 관리자 전용 강제 저장. base_revision 비교 없이 항상 성공한다
  // (서버가 fn_is_admin()으로 재검증 — 일반 직원이 호출하면 PERMISSION_DENIED).
  // 이 함수를 호출하는 버튼 자체를 일반 직원 화면에는 만들지 않는다(App.tsx의
  // role 분기 참고) — 이 함수가 있다는 사실만으로 권한이 생기지 않는다.
  const forceSaveMessage = useCallback(async (messageNo: number, content: string) => {
    setStates((prev) => ({ ...prev, [messageNo]: markSaving(prev[messageNo]) }));

    const { data, error } = await supabase.rpc("force_update_shared_message", buildForceUpdateParams(messageNo, content));

    if (error) {
      const message = error.message ?? "";
      if (message.startsWith("PERMISSION_DENIED:")) {
        throw new SavePermissionError(message.split(":").slice(1).join(":").trim());
      }
      throw new Error(message || "강제 저장 중 오류가 발생했습니다.");
    }

    const row = data as SharedMessageRow;
    setStates((prev) => ({ ...prev, [messageNo]: markSaved(prev[messageNo], rowToSnapshot(row)) }));
  }, []);

  return {
    states, connectionStatus, lastUpdatedAt, refresh, saveMessage, forceSaveMessage,
    beginEdit, endEdit, discardEdit, loadLatestAndDiscardEdit, keepLocalAndDiscardRemote,
  };
}
