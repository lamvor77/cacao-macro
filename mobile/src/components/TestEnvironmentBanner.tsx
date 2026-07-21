// Test Environment Deployment & E2E Validation Sprint 1절 — 운영 배포와
// 테스트 배포를 화면에서 명확히 구분한다. VITE_APP_ENV가 "test"일 때만
// 렌더링되며, 값이 없으면(운영 배포) 아무것도 렌더링하지 않는다.
export const isTestEnvironment =
  (import.meta.env.VITE_APP_ENV as string | undefined)?.trim().toLowerCase() === "test";

export function TestEnvironmentBanner() {
  if (!isTestEnvironment) {
    return null;
  }
  return <div className="test-environment-banner">⚠ TEST ENVIRONMENT</div>;
}
