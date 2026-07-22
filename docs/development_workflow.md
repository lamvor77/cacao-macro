# 운영/개발 워크플로 매뉴얼

## 프로젝트 구성

| 용도 | Supabase 프로젝트 | Vercel 환경 | PC `.env` |
|---|---|---|---|
| 운영 | `cacao-macro` (`kdyxxkltafeuucijiyzp`) | Production | `APP_ENV=production` |
| 개발/테스트 | `cacao-macro-dev` (신규 생성 예정) | Preview | `APP_ENV=test` |

---

## 1. 운영 전환 체크리스트

### Supabase

- [ ] 기존 프로젝트(`nojdwuoronqmvpdptvlr`) 삭제 — `Project Settings → General` 맨 아래
- [ ] 프로젝트 표시 이름을 `cacao-macro`로 변경 — `Project Settings → General`
- [ ] Google OAuth Redirect URL 확인 — `Authentication → URL Configuration → Redirect URLs`(PC 콜백 패턴 + 모바일 Production 도메인)
- [ ] Site URL 확인 — `Authentication → URL Configuration → Site URL`
- [ ] Auth 설정 확인 — `Authentication → Providers → Google`(활성화, Client ID/Secret 입력됨)
- [ ] 테스트 계정 삭제 — `docs/production_switch_execution.md` 7절 SQL로
- [ ] 테스트 메시지 삭제/초기화 — 필요 시 위와 동일 문서 참고

### Vercel

- [ ] Production Environment Variables 수정 — `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`를 `cacao-macro` 값으로
- [ ] Redeploy — Production 재배포(환경변수는 재배포해야 반영됨)

### PC

- [ ] 최신 exe(v1.2.1) 배포
- [ ] `.env` 확인 — `SUPABASE_URL`/`SUPABASE_ANON_KEY`가 `cacao-macro`, `APP_ENV=production`
- [ ] 로그인
- [ ] 관리자 승인
- [ ] Legacy 메시지 저장
- [ ] Shared 메시지 저장
- [ ] Realtime 확인

---

## 2. 운영 확인 — 5분 스모크 테스트

1. PC 로그인
2. 메시지 저장
3. 모바일 조회
4. 메시지 수정
5. 실시간 반영
6. 발송
7. 관리자 승인

7개 전부 통과하면 운영 전환 완료로 간주한다. 하나라도 실패하면 배포를
멈추고 원인을 먼저 확인한다.

---

## 3. 새 개발 환경 구축 — `cacao-macro-dev`

운영이 안정화된 뒤, 아래 순서로 운영과 완전히 분리된 개발용 프로젝트를
만든다.

1. **생성**: Supabase에서 새 프로젝트 생성, 이름 `cacao-macro-dev`
2. **SQL 적용** (SQL Editor, 순서대로 전체 실행):
   1. `docs/sql/phase2b_schema.sql`
   2. `docs/sql/phase4_admin_rpc.sql`
   3. `docs/sql/shared_messages_realtime.sql`
3. **Auth 설정**: `Authentication → Providers → Google` 활성화
4. **OAuth**: 기존 Google OAuth 클라이언트에 `cacao-macro-dev`의 콜백 URL 추가(신규 클라이언트 발급 불필요) — `Authentication → URL Configuration → Redirect URLs`에 등록
5. **Environment Variables**: `cacao-macro-dev`의 URL/anon key 확보(값은 기록·공유 시 항상 마스킹)
6. **PC 개발환경**: `test-runtime/.env`(또는 별도 dev 폴더)를 `cacao-macro-dev`로 연결, `APP_ENV=test` 유지
7. **Vercel Preview**: Preview 환경변수를 `cacao-macro-dev`로 설정(Production과 분리 유지)

완료 후 `docs/sql/phase2b_schema.sql` 8절 방식으로 개발용 관리자 1명을
수동 지정한다.

---

## 4. 앞으로의 운영 원칙

- **운영은 `cacao-macro`, 개발은 `cacao-macro-dev`** — 두 프로젝트는 항상 분리 상태를 유지한다.
- **새 기능은 `dev → 테스트 → 운영` 순서만 허용한다.** 이 순서를 건너뛰지 않는다.
- **운영 DB에서 직접 SQL 작업을 금지한다.** 스키마 변경은 항상 `docs/sql/`의 파일을 dev에서 먼저 검증한 뒤 운영에 적용한다.
- **운영에서 먼저 개발하지 않는다.** 새 기능/실험적 변경은 dev에서 시작한다.
- **운영에서 테스트하지 않는다.** RLS/RPC 쓰기 테스트, 부하 테스트 등은 dev에서만 수행한다(`scripts/check_rls_rpc_permissions.py`는 `APP_ENV=test`가 아니면 자동으로 실행을 거부한다).
- **운영 데이터를 개발로 복사하지 않는다.** dev가 필요로 하는 데이터는 dev 안에서 직접 만든다.
