// TestEnvironmentBanner.tsx의 isTestEnvironment 플래그 테스트.
// 모듈 최상단에서 import.meta.env를 한 번만 읽으므로(Python config/settings.py의
// IS_TEST_ENVIRONMENT와 동일한 패턴), 값을 바꿔가며 확인하려면 vi.resetModules()로
// 매번 새로 import해야 한다.
import { afterEach, describe, expect, it, vi } from "vitest";

async function loadWithEnv(value: string | undefined) {
  vi.resetModules();
  if (value === undefined) {
    vi.unstubAllEnvs();
  } else {
    vi.stubEnv("VITE_APP_ENV", value);
  }
  return import("./TestEnvironmentBanner");
}

describe("isTestEnvironment", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("VITE_APP_ENV=test이면 true", async () => {
    const mod = await loadWithEnv("test");
    expect(mod.isTestEnvironment).toBe(true);
  });

  it("VITE_APP_ENV=TEST(대문자)여도 true", async () => {
    const mod = await loadWithEnv("TEST");
    expect(mod.isTestEnvironment).toBe(true);
  });

  it("VITE_APP_ENV가 없으면 false(운영 배포 기본값)", async () => {
    const mod = await loadWithEnv(undefined);
    expect(mod.isTestEnvironment).toBe(false);
  });

  it("VITE_APP_ENV=production이면 false", async () => {
    const mod = await loadWithEnv("production");
    expect(mod.isTestEnvironment).toBe(false);
  });
});
