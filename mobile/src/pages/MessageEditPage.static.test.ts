// MessageEditPage.tsx에 제목 입력 UI가 없는지 정적으로 확인한다(요구사항 —
// "제목 입력란, 라벨, 안내 문구를 삭제"). 컴포넌트 렌더링 테스트 도구를
// 새로 추가하지 않고, 소스 텍스트 자체를 검사하는 방식으로 회귀를 막는다
// (PC 쪽 tests/test_check_shared_messages_schema.py의 AST 기반 정적 검사와
// 같은 원칙 — 실행 없이 소스 구조를 검증). Node의 fs 모듈 대신 Vite의 ?raw
// 임포트를 써서 @types/node 등 새 의존성을 추가하지 않는다.
import { describe, expect, it } from "vitest";
import source from "./MessageEditPage.tsx?raw";

describe("MessageEditPage — 제목 UI 제거 확인", () => {
  it("제목 입력 필드(id=msg-title)가 없다", () => {
    expect(source as string).not.toContain("msg-title");
  });

  it("'제목' 라벨/안내 문구가 없다", () => {
    expect(source as string).not.toMatch(/제목\s*\(선택\)/);
  });

  it("title을 useState로 관리하지 않는다", () => {
    expect(source as string).not.toMatch(/useState\(state\.title/);
  });

  it("saveMessage/forceSaveMessage 호출에 title 인자를 넘기지 않는다", () => {
    expect(source as string).not.toMatch(/saveMessage\(messageNo, content, title/);
    expect(source as string).not.toMatch(/forceSaveMessage\(messageNo, content, title/);
  });
});
