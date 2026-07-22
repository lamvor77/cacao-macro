# RLS / RPC 권한 매트릭스 결과

작성 기준: `docs/test_environment_execution_guide.md` 4절, `scripts/check_rls_rpc_permissions.py`.

계정은 별칭만 사용한다(employee_a, employee_b, admin_a, disabled_user) — 실제 이메일은 기록하지 않는다.

## 기대 동작 (스펙 5절 기준, 코드/SQL 검토로 사전 확인됨)

| 역할 | shared_messages SELECT | update_shared_message | force_update_shared_message | 직접 UPDATE | INSERT | DELETE | history SELECT | history 쓰기 |
|---|---|---|---|---|---|---|---|---|
| employee_a | 허용 | 허용 | 거부 | 거부 | 거부 | 거부 | 허용 | 거부 |
| employee_b | 허용 | 허용 | 거부 | 거부 | 거부 | 거부 | 허용 | 거부 |
| admin_a | 허용 | 허용 | 허용 | 거부 | 거부 | 거부 | 허용 | 거부 |
| disabled_user | 거부 | 거부 | 거부 | 거부 | 거부 | 거부 | 거부 | 거부 |

## 실제 실행 결과

실행 명령: `python scripts/check_rls_rpc_permissions.py --message-no 12`

```
PENDING — 아직 실행되지 않음(테스트 프로젝트 + 4개 테스트 계정 준비 후 실행)
```

| 역할 | 실행 여부 | PASS | FAIL | 비고 |
|---|---|---|---|---|
| employee_a | PENDING | | | |
| employee_b | PENDING | | | |
| admin_a | PENDING | | | |
| disabled_user | PENDING | | | |

## 발견한 문제 / 수정 내용

(없으면 "없음"으로 기록)
