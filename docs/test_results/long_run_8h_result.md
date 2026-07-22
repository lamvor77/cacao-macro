# 8시간 장시간 테스트 결과

작성 기준: `docs/test_environment_execution_guide.md` 10절, `scripts/health_snapshot.py`.

실행 명령:
```
python scripts/health_snapshot.py --pid <PID> --log-file test-runtime\logs\<파일> --output docs\test_results\long_run_8h_raw.csv --interval-seconds 300
```

원본 CSV: `docs/test_results/long_run_8h_raw.csv` (PENDING — 아직 생성되지 않음)

## 구간별 스냅샷

| 시점 | Realtime 상태 | reconnect_count | 마지막 동기화 시각 | 메모리(RSS MB) | Thread 수 | CPU% | 비고 |
|---|---|---|---|---|---|---|---|
| 시작 직후 | PENDING | | | | | | |
| 1시간 | PENDING | | | | | | |
| 2시간 | PENDING | | | | | | |
| 4시간 | PENDING | | | | | | |
| 6시간 | PENDING | | | | | | |
| 8시간 | PENDING | | | | | | |

## 판정 기준 대비 결과

| 기준 | 결과 |
|---|---|
| 지속적인 메모리 상승 없음 | PENDING |
| Realtime thread 증가 없음 | PENDING |
| 구독 채널 누적 없음 | PENDING |
| 재연결 후 메시지 누락 없음 | PENDING |
| GUI 응답성 유지(멈춤 없음) | PENDING |
| background exception 없음 | PENDING |
| scheduler 정상(운영시간 내 자동발송 정상) | PENDING |
| 메시지 revision 정합성 유지 | PENDING |

## 종합 판정

PENDING (PASS / WARN / FAIL)
