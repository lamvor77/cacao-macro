// types.ts 테스트 — 역할 매핑 + 강제 저장 권한 판단 순수 로직
import { describe, expect, it } from "vitest";
import { canForceSave, toMobileRole, type AppUserProfile } from "./types";

function profile(overrides: Partial<AppUserProfile> = {}): AppUserProfile {
  return {
    id: "u1", email: "u1@example.com", display_name: null,
    status: "approved", role: "editor",
    ...overrides,
  };
}

describe("toMobileRole", () => {
  it("null profile is disabled", () => {
    expect(toMobileRole(null)).toBe("disabled");
  });

  it("pending status is disabled", () => {
    expect(toMobileRole(profile({ status: "pending" }))).toBe("disabled");
  });

  it("blocked status is disabled", () => {
    expect(toMobileRole(profile({ status: "blocked" }))).toBe("disabled");
  });

  it("approved admin is admin", () => {
    expect(toMobileRole(profile({ role: "admin" }))).toBe("admin");
  });

  it("approved editor is employee", () => {
    expect(toMobileRole(profile({ role: "editor" }))).toBe("employee");
  });

  it("approved viewer is disabled (mobile has no read-only mode)", () => {
    expect(toMobileRole(profile({ role: "viewer" }))).toBe("disabled");
  });
});

describe("canForceSave", () => {
  it("admin can force save", () => {
    expect(canForceSave("admin")).toBe(true);
  });

  it("employee cannot force save", () => {
    expect(canForceSave("employee")).toBe(false);
  });

  it("disabled cannot force save", () => {
    expect(canForceSave("disabled")).toBe(false);
  });
});
