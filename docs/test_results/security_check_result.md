# 보안 점검 결과

## 정적 스캔 (이번 스프린트 신규/수정 파일 대상)

검사 패턴: `service_role`, `SERVICE_ROLE_KEY`, JWT 패턴(`eyJ...`), 실제 사용자 이메일, 운영 프로젝트 호스트 문자열.

| 대상 | 결과 |
|---|---|
| `scripts/check_rls_rpc_permissions.py`, `scripts/setup_test_runtime.py`, `scripts/health_snapshot.py` | service_role 미사용 확인. 토큰은 CLI 인자/환경변수로만 받고 어디에도 출력/기록하지 않음(코드 검토로 확인) |
| `config/settings.py`(IS_TEST_ENVIRONMENT 추가분) | 하드코딩된 비밀값 없음 |
| `services/diagnostics_service.py`, `gui/panels/diagnostics_panel.py` | 환경 표시 텍스트만 추가, `FORBIDDEN_KEYWORDS` 자체 검사(`tests/test_diagnostics_service.py::TestCopyTextNoSecrets`)로 이미 회귀 방지됨 |
| `gui/main_window.py`(TEST ENVIRONMENT 배지) | UI 라벨 추가만, 비밀값 없음 |
| `mobile/src/components/TestEnvironmentBanner.tsx`, `App.tsx` | anon key 이외 값 사용 안 함, VITE_ 접두사 규칙 준수 |
| `nojdwuoronqmvpdptvlr` 문자열이 등장하는 위치 | `scripts/check_rls_rpc_permissions.py`의 운영 프로젝트 차단 목록(`_PRODUCTION_HOST_DENYLIST`)과 이를 검증하는 테스트뿐 — 실제 접속에 사용되지 않음(의도된 안전장치) |
| JWT 패턴(`eyJ...`) 하드코딩 | 없음 |
| 실제 이메일/전화번호 | 없음 |

## 안전장치 검증 (자동 테스트로 확인됨)

| 항목 | 확인 방법 | 결과 |
|---|---|---|
| 운영 호스트 감지 시 스크립트 즉시 중단 | `tests/test_check_rls_rpc_permissions.py::TestProductionGuard` | PASS(단위 테스트 5건) |
| APP_ENV/SUPABASE_ENVIRONMENT=test 미설정 시 중단 | 위와 동일 | PASS |
| 진단 텍스트에 금지어(`FORBIDDEN_KEYWORDS`) 미포함 | `tests/test_diagnostics_service.py::TestCopyTextNoSecrets` | PASS |

## 미실행 항목 (실제 테스트 프로젝트 필요)

- 모바일 Preview 배포 산출물의 소스맵/시크릿 스캔: 실제 배포 전이라 대상 없음. `mobile` production build는 기존과 동일 방식(anon key만 VITE_ 노출)이므로 이전 스프린트에서 확인한 것과 동일한 결론이 적용될 것으로 예상되나, 실제 테스트 배포본에 대해 다시 확인 필요(PENDING).
- 실제 access token을 사용한 `check_rls_rpc_permissions.py` 실행 로그에 토큰이 섞여 나오지 않는지 실제 실행으로 재확인(PENDING — 코드 검토로는 미포함 확인됨).

## 종합 판정

정적 검토 기준 **문제 없음**. 실제 테스트 프로젝트 연결 후 재확인 필요 항목은 PENDING으로 남김.
