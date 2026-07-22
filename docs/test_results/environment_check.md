# 테스트 환경 사전 점검 결과

작성 기준: `docs/test_environment_execution_guide.md` 1~7절.

## 프로젝트 구분 확인

| 항목 | 값 | 확인 |
|---|---|---|
| PC 운영 `.env`의 SUPABASE_URL | `nojdwuoronqmvpdptvlr.supabase.co` | **운영(production) 프로젝트로 확인됨 — 사용자 확인, 2026-07-19** |
| 이번 스프린트 테스트 대상 프로젝트 | (미정) | PENDING — 사용자가 별도 테스트 프로젝트 생성/지정 필요 |
| 테스트 프로젝트 URL이 운영과 다른가 | PENDING | 테스트 프로젝트 확정 후 확인 |

## 체크리스트 (실행 가이드 6~7절 기준)

| 항목 | 상태 |
|---|---|
| 테스트 Auth 사용자 준비(employee_a/employee_b/admin_a/disabled_user) | PENDING |
| employee 계정 2개 준비 | PENDING |
| admin 계정 1개 준비 | PENDING |
| 직원 profile/권한 테이블 구조 | 코드 검토로 확인됨 — `docs/sql/phase2b_schema.sql`의 `app_users`(status/role) 사용, shared_messages 쪽에서 새로 만들지 않음 |
| fn_can_edit() | 코드 검토로 확인됨 — `phase2b_schema.sql:194`, 인자 없음, security definer, `role in ('editor','admin')` |
| fn_is_admin() | 코드 검토로 확인됨 — `phase2b_schema.sql:188`, `role = 'admin'` |
| Realtime 활성화 여부(테스트 프로젝트) | PENDING — SQL 적용 후 Supabase 대시보드에서 확인 |
| Postgres Changes 사용 가능 여부 | PENDING |
| Google OAuth Redirect URL 설정 | PENDING |
| 모바일 Preview URL | PENDING |
| PC `.env` 테스트 프로젝트 연결 여부 | 아직 연결 안 함 — `test-runtime/.env`를 별도로 준비해야 함(운영 `.env`와 분리) |

## TEST ENVIRONMENT 표시 구현 (Claude가 코드로 준비 완료)

| 항목 | 상태 |
|---|---|
| `APP_ENV`/`SUPABASE_ENVIRONMENT=test` 인식 (`config/settings.py IS_TEST_ENVIRONMENT`) | 구현 완료, 단위 테스트 통과(`tests/test_environment_flag.py`) |
| PC 창 제목표시줄 "TEST ENVIRONMENT" 배지 | 구현 완료(`gui/main_window.py`) — 실제 실행 확인은 PENDING |
| PC 진단 화면 "환경: TEST ENVIRONMENT" 표시 | 구현 완료(`services/diagnostics_service.py`, `gui/panels/diagnostics_panel.py`), 단위 테스트 통과 |
| 모바일 상단 "TEST ENVIRONMENT" 배너 (`VITE_APP_ENV=test`) | 구현 완료(`mobile/src/components/TestEnvironmentBanner.tsx`), 단위 테스트 통과 |
| 운영 환경에서는 표시가 전혀 나타나지 않음 | 기본값(APP_ENV 미설정)이 false이므로 확인됨(단위 테스트) |

## 다음 단계

1. 별도 테스트 Supabase 프로젝트 생성/지정
2. `docs/test_environment_execution_guide.md` 2~7절 순서대로 진행
3. 위 PENDING 항목을 채운 뒤 이 파일을 갱신
