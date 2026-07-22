import { MAX_MESSAGE_NO, MIN_MESSAGE_NO, STATUS_LABELS_KO } from "../syncLogic";
import type { useSharedMessages } from "../hooks/useSharedMessages";
import { ConnectionStatusBadge } from "../components/ConnectionStatusBadge";

interface Props {
  shared: ReturnType<typeof useSharedMessages>;
  onSelect: (messageNo: number) => void;
  onLogout: () => void;
  userEmail: string;
}

function truncate(text: string, max = 40): string {
  const trimmed = text.trim();
  if (!trimmed) return "(빈 메시지)";
  return trimmed.length > max ? `${trimmed.slice(0, max)}…` : trimmed;
}

export function MessageListPage({ shared, onSelect, onLogout, userEmail }: Props) {
  const numbers = Array.from(
    { length: MAX_MESSAGE_NO - MIN_MESSAGE_NO + 1 },
    (_, i) => MIN_MESSAGE_NO + i,
  );

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>메시지 1~12</h1>
          <div className="muted small">{userEmail}</div>
        </div>
        <div className="header-actions">
          <ConnectionStatusBadge status={shared.connectionStatus} />
          <button className="ghost-button" onClick={() => void shared.refresh()}>새로고침</button>
          <button className="ghost-button" onClick={onLogout}>로그아웃</button>
        </div>
      </header>

      {shared.lastUpdatedAt && (
        <div className="muted small" style={{ marginBottom: 8 }}>
          마지막 업데이트: {new Date(shared.lastUpdatedAt).toLocaleTimeString("ko-KR")}
        </div>
      )}

      <ul className="message-list">
        {numbers.map((n) => {
          const state = shared.states[n];
          return (
            <li key={n} className="message-list-item" onClick={() => onSelect(n)}>
              <div className="message-list-item-top">
                <span className="message-no">메시지 {n}</span>
                <span className={`status-chip status-${state.status}`}>{STATUS_LABELS_KO[state.status]}</span>
              </div>
              <div className="message-preview">{truncate(state.content)}</div>
              <div className="muted small">
                {state.updatedByName ? `${state.updatedByName} · ` : ""}
                {state.updatedAt ? new Date(state.updatedAt).toLocaleString("ko-KR") : "-"} · rev {state.revision}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
