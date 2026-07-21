import { useEffect, useState } from "react";
import type { useSharedMessages } from "../hooks/useSharedMessages";
import { SaveConflictError, SavePermissionError } from "../hooks/useSharedMessages";
import { STATUS_LABELS_KO } from "../syncLogic";

interface Props {
  shared: ReturnType<typeof useSharedMessages>;
  messageNo: number;
  onBack: () => void;
  isAdmin: boolean;
}

export function MessageEditPage({ shared, messageNo, onBack, isAdmin }: Props) {
  const state = shared.states[messageNo];
  const [title, setTitle] = useState(state.title ?? "");
  const [content, setContent] = useState(state.content);
  const [saveError, setSaveError] = useState<string | null>(null);

  // 화면에 처음 들어왔을 때만 서버 값으로 입력창을 채운다 — 이후 Realtime으로
  // state.content가 바뀌어도(다른 사람 수정) 입력창을 조용히 덮어쓰지 않는다
  // (요구사항 10절 — 대신 아래 "다른 직원이 수정함" 배너로 안내한다).
  useEffect(() => {
    shared.beginEdit(messageNo);
    setTitle(state.title ?? "");
    setContent(state.content);
    return () => shared.endEdit(messageNo);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messageNo]);

  const handleSave = async () => {
    setSaveError(null);
    try {
      await shared.saveMessage(messageNo, content, title || null);
      onBack();
    } catch (e) {
      if (e instanceof SaveConflictError) {
        setSaveError("다른 사용자가 먼저 저장했습니다. 최신 내용을 확인한 뒤 다시 저장해 주세요.");
      } else if (e instanceof SavePermissionError) {
        setSaveError("이 메시지를 저장할 권한이 없습니다.");
      } else {
        setSaveError(e instanceof Error ? e.message : "저장 중 오류가 발생했습니다.");
      }
    }
  };

  const handleCancel = () => {
    shared.discardEdit(messageNo);
    onBack();
  };

  const handleReloadLatest = () => {
    shared.loadLatestAndDiscardEdit(messageNo);
    setTitle(shared.states[messageNo].title ?? "");
    setContent(shared.states[messageNo].content);
  };

  const handleKeepMine = () => {
    shared.keepLocalAndDiscardRemote(messageNo);
  };

  // 요구사항 10절 — 관리자에게만 노출한다(isAdmin=false면 이 함수를 호출할 버튼
  // 자체가 렌더링되지 않는다, 아래 JSX 참고). 최종 방어는 항상 서버(RPC의
  // fn_is_admin())다 — 이 조건은 편의 기능일 뿐이다.
  const handleForceSave = async () => {
    const confirmed = window.confirm(
      "서버의 최신 메시지를 덮어씁니다.\n다른 직원이 수정한 내용이 사라질 수 있습니다.",
    );
    if (!confirmed) return;
    setSaveError(null);
    try {
      await shared.forceSaveMessage(messageNo, content, title || null);
      onBack();
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : "강제 저장 중 오류가 발생했습니다.");
    }
  };

  return (
    <div className="page">
      <header className="page-header">
        <button className="ghost-button" onClick={onBack}>← 목록</button>
        <span className="status-chip">{STATUS_LABELS_KO[state.status]}</span>
      </header>

      <h1>메시지 {messageNo}</h1>
      <div className="muted small">
        마지막 수정: {state.updatedByName || "-"}
        {state.updatedAt ? ` · ${new Date(state.updatedAt).toLocaleString("ko-KR")}` : ""} · rev {state.revision}
      </div>

      {state.pendingRemote && (
        <div className="conflict-banner">
          <p>다른 직원이 이 메시지를 수정했습니다.</p>
          <div className="conflict-actions">
            <button onClick={() => alert(state.pendingRemote?.content || "(빈 메시지)")}>최신 내용 확인</button>
            <button onClick={handleKeepMine}>현재 작성 내용 유지</button>
            <button
              onClick={() => {
                navigator.clipboard?.writeText(content).catch(() => undefined);
                handleReloadLatest();
              }}
            >
              작성 내용 복사 후 최신 불러오기
            </button>
            {isAdmin && (
              <button className="force-save-button" onClick={() => void handleForceSave()}>
                내 내용으로 강제 저장
              </button>
            )}
          </div>
        </div>
      )}

      <label className="field-label" htmlFor="msg-title">제목(선택)</label>
      <input
        id="msg-title"
        className="text-input"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        maxLength={200}
      />

      <label className="field-label" htmlFor="msg-content">내용</label>
      <textarea
        id="msg-content"
        className="text-area"
        value={content}
        onChange={(e) => setContent(e.target.value)}
        rows={10}
      />

      {saveError && <p className="error-text">{saveError}</p>}

      <div className="edit-actions">
        <button className="ghost-button" onClick={handleReloadLatest}>최신 내용 다시 불러오기</button>
        <button className="ghost-button" onClick={handleCancel}>취소</button>
        <button className="primary-button" onClick={() => void handleSave()}>저장</button>
      </div>
    </div>
  );
}
