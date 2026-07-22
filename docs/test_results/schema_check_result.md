# SQL 적용 및 스키마 점검 결과

작성 기준: `docs/test_environment_execution_guide.md` 2~3절.

## SQL 적용 기록 (섹션 3)

| 항목 | 값 |
|---|---|
| 적용 대상 환경 | test |
| 적용 대상 프로젝트(마스킹) | PENDING |
| 백업 파일 위치 | PENDING (예: `backup/sql-backup-<날짜>/shared_messages_realtime.sql`) |
| 적용한 SQL 파일 SHA-256 | PENDING |
| 최초 적용 시각 | PENDING |
| 최초 적용 오류 여부 | PENDING |
| 재실행(idempotency) 시각 | PENDING |
| 재실행 시 치명적 오류 여부 | PENDING |

## scripts/check_shared_messages_schema.py 실행 결과 (섹션 4)

실행 명령:
```
python scripts/check_shared_messages_schema.py
python scripts/check_shared_messages_schema.py --access-token <마스킹>
```

출력(민감정보 제거됨, 아래에 실행 결과를 그대로 붙여넣는다):

```
PENDING — 아직 실행되지 않음
```

| 검사 항목 | PASS/WARN/FAIL |
|---|---|
| shared_messages 테이블 | PENDING |
| shared_message_history 테이블 | PENDING |
| 예상 컬럼 | PENDING |
| 1~12번 행 | PENDING |
| message_no 중복 없음 | PENDING |
| revision 모두 1 이상 | PENDING |
| SELECT 권한 | PENDING |
| update_shared_message RPC | PENDING |
| force_update_shared_message RPC | PENDING |

## Realtime publication 수동 확인 (Supabase 대시보드)

| 항목 | 결과 |
|---|---|
| supabase_realtime publication에 shared_messages 포함 | PENDING |
| Replica Identity = FULL | PENDING |
| UPDATE 이벤트 실제 수신 가능(수동 테스트) | PENDING |

## 조치 내용

(발견된 문제와 수정 내용을 여기에 기록)
