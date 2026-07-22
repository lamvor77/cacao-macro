// useSharedMessages.ts가 실제 RPC 호출에 사용하는 파라미터 생성 순수 함수 테스트.
// 이 함수들은 supabaseClient를 import하지 않는 syncLogic.ts에 있다(훅 자체는
// useState/useEffect/supabase를 쓰므로 렌더링 없이 테스트하지 않는 기존 관례).
// "모바일이 title을 서버로 보내지 않는다"는 요구사항을 이 두 순수 함수로 검증한다.
import { describe, expect, it } from "vitest";
import { buildForceUpdateParams, buildUpdateParams } from "../syncLogic";

describe("buildUpdateParams", () => {
  it("p_title은 항상 null이다(제목 입력 기능 제거)", () => {
    const params = buildUpdateParams(3, "새 내용", 5);
    expect(params.p_title).toBeNull();
  });

  it("title 인자 자체를 받지 않는다(시그니처에 없음)", () => {
    // TypeScript가 컴파일 타임에 이미 3개 인자만 허용하지만, 런타임으로도
    // 반환값에 title 관련 다른 키가 섞여 들어오지 않는지 확인한다.
    const params = buildUpdateParams(1, "x", 1);
    expect(Object.keys(params).sort()).toEqual(
      ["p_base_revision", "p_content", "p_message_no", "p_title", "p_update_source"].sort(),
    );
  });

  it("나머지 필드는 그대로 전달된다", () => {
    const params = buildUpdateParams(7, "hello", 42);
    expect(params.p_message_no).toBe(7);
    expect(params.p_content).toBe("hello");
    expect(params.p_base_revision).toBe(42);
    expect(params.p_update_source).toBe("mobile");
  });
});

describe("buildForceUpdateParams", () => {
  it("p_title은 항상 null이다", () => {
    const params = buildForceUpdateParams(2, "강제 저장 내용");
    expect(params.p_title).toBeNull();
  });

  it("나머지 필드는 그대로 전달된다", () => {
    const params = buildForceUpdateParams(9, "content");
    expect(params.p_message_no).toBe(9);
    expect(params.p_content).toBe("content");
    expect(params.p_update_source).toBe("admin_force");
  });
});
