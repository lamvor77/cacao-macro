// PC 쪽 services/shared_message_service.py의 dataclass와 대응한다 — 컬럼 이름은
// docs/sql/shared_messages_realtime.sql과 정확히 일치해야 한다.

export interface SharedMessageRow {
  id: string;
  message_no: number;
  title: string | null;
  content: string;
  revision: number;
  is_active: boolean;
  updated_at: string;
  updated_by: string | null;
  updated_by_name: string | null;
  update_source: string;
  created_at: string;
}

// public.app_users(Phase 2B) — 모바일도 같은 테이블/승인 흐름을 그대로 쓴다
// (docs/sql/shared_messages_realtime.sql 상단 "역할 매핑" 주석 참고).
export interface AppUserProfile {
  id: string;
  email: string;
  display_name: string | null;
  status: "pending" | "approved" | "blocked";
  role: "viewer" | "editor" | "admin";
}

export type MobileRole = "employee" | "admin" | "disabled";

/** app_users 행을 이번 스프린트 스펙의 employee/admin/disabled 어휘로 매핑한다. */
export function toMobileRole(profile: AppUserProfile | null): MobileRole {
  if (profile === null || profile.status !== "approved") return "disabled";
  if (profile.role === "admin") return "admin";
  if (profile.role === "editor") return "employee";
  return "disabled"; // viewer는 이번 스프린트에서 "수정 권한 없음" = 접근 불가로 취급
}

/** 요구사항 10절 — 강제 저장 버튼 노출 여부. 순수 함수로 분리해 컴포넌트
 * 렌더링 없이도(테스트 도구 추가 없이) 권한 판단 로직 자체를 검증할 수 있게
 * 한다. 최종 방어는 항상 서버(force_update_shared_message RPC의
 * fn_is_admin())이며, 이 함수는 UI 노출 여부만 결정한다. */
export function canForceSave(role: MobileRole): boolean {
  return role === "admin";
}
