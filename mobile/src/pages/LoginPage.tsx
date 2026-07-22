import { useAuth } from "../hooks/useAuth";

interface Props {
  auth: ReturnType<typeof useAuth>;
}

export function LoginPage({ auth }: Props) {
  return (
    <div className="centered-page">
      <h1>단톡방 매크로</h1>
      <p className="subtitle">내부 직원 전용 — 승인된 계정만 접근할 수 있습니다.</p>
      <button className="primary-button" onClick={() => void auth.signInWithGoogle()}>
        Google로 로그인
      </button>
      {auth.error && <p className="error-text">{auth.error}</p>}
    </div>
  );
}
