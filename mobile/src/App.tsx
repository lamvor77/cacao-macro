import { useState } from "react";
import { useAuth } from "./hooks/useAuth";
import { useSharedMessages } from "./hooks/useSharedMessages";
import { LoginPage } from "./pages/LoginPage";
import { AccessDeniedPage } from "./pages/AccessDeniedPage";
import { MessageListPage } from "./pages/MessageListPage";
import { MessageEditPage } from "./pages/MessageEditPage";
import { isSupabaseConfigured } from "./supabaseClient";
import { canForceSave } from "./types";
import { TestEnvironmentBanner } from "./components/TestEnvironmentBanner";

function AppContent() {
  const auth = useAuth();
  const [selectedMessageNo, setSelectedMessageNo] = useState<number | null>(null);
  // employee/admin만 shared_messages를 구독한다 — 로그인 전/미승인 상태에서는
  // 불필요한 연결을 시도하지 않는다.
  const canAccess = auth.role === "employee" || auth.role === "admin";
  const shared = useSharedMessages(canAccess);

  if (!isSupabaseConfigured) {
    return (
      <div className="centered-page">
        <h1>설정 오류</h1>
        <p className="subtitle">VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY가 설정되지 않았습니다.</p>
      </div>
    );
  }

  if (auth.loading) {
    return <div className="centered-page">불러오는 중...</div>;
  }

  if (auth.session === null) {
    return <LoginPage auth={auth} />;
  }

  if (!canAccess) {
    const status = auth.profile?.status ?? "unknown";
    return (
      <AccessDeniedPage
        status={status === "pending" || status === "blocked" ? status : "unknown"}
        onLogout={() => void auth.signOut()}
      />
    );
  }

  if (selectedMessageNo !== null) {
    return (
      <MessageEditPage
        shared={shared}
        messageNo={selectedMessageNo}
        onBack={() => setSelectedMessageNo(null)}
        isAdmin={canForceSave(auth.role)}
      />
    );
  }

  return (
    <MessageListPage
      shared={shared}
      onSelect={setSelectedMessageNo}
      onLogout={() => void auth.signOut()}
      userEmail={auth.profile?.email ?? auth.session.user.email ?? ""}
    />
  );
}

export default function App() {
  // Test Environment Deployment & E2E Validation Sprint 1절 — 로그인 전/후,
  // 화면 종류와 무관하게 항상 최상단에 표시되도록 AppContent 바깥에 둔다.
  return (
    <>
      <TestEnvironmentBanner />
      <AppContent />
    </>
  );
}
