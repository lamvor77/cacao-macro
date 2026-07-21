import type { ConnectionStatus } from "../hooks/useSharedMessages";

// 요구사항 8절 — PC/모바일 모두 기술 용어(enum 값) 대신 사용자 친화적 문구만
// 노출한다. 모바일 문구는 스펙에 명시된 4가지("연결됨/연결 중/오프라인/다시
// 연결 중")를 그대로 사용한다.
const LABELS: Record<ConnectionStatus, string> = {
  connecting: "연결 중",
  connected: "연결됨",
  reconnecting: "다시 연결 중",
  disconnected: "오프라인",
};

const COLORS: Record<ConnectionStatus, string> = {
  connecting: "#5BA4E8",
  connected: "#4CAF50",
  reconnecting: "#E6A100",
  disconnected: "#B71C1C",
};

export function ConnectionStatusBadge({ status }: { status: ConnectionStatus }) {
  return (
    <span className="connection-badge" style={{ color: COLORS[status] }}>
      ● {LABELS[status]}
    </span>
  );
}
