interface Props {
  status: "pending" | "blocked" | "unknown";
  onLogout: () => void;
}

const MESSAGES: Record<Props["status"], string> = {
  pending: "관리자 승인을 기다리고 있습니다. 승인 후 다시 로그인해 주세요.",
  blocked: "이 계정은 접근이 차단되었습니다. 관리자에게 문의해 주세요.",
  unknown: "계정 정보를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.",
};

export function AccessDeniedPage({ status, onLogout }: Props) {
  return (
    <div className="centered-page">
      <h1>접근할 수 없습니다</h1>
      <p className="subtitle">{MESSAGES[status]}</p>
      <button className="ghost-button" onClick={onLogout}>로그아웃</button>
    </div>
  );
}
