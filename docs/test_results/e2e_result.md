# E2E 20개 시나리오 실행 결과

각 시나리오의 세부 절차는 `docs/e2e_realtime_test_plan.md`를 참고한다. 계정은
별칭만 사용한다. 아래 템플릿을 시나리오마다 채운다(완료된 시나리오만 표를
채우고, 그렇지 않은 시나리오는 "PENDING"으로 둔다).

| # | 시나리오 | 날짜/시각 | 계정 | 사전조건 | 수행단계 | 기대결과 | 실제결과 | 판정 | 로그/스크린샷 | 발견문제 | 수정여부 | 재시험 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 모바일 1번 수정 → PC 반영 | PENDING | | | | | | PENDING | | | | |
| 2 | PC 2번 수정 → 모바일 반영 | PENDING | | | | | | PENDING | | | | |
| 3 | 동시 3번 수정 → 한쪽만 성공 | PENDING | | | | | | PENDING | | | | |
| 4 | PC 네트워크 단절 | PENDING | | | | | | PENDING | | | | |
| 5 | 단절 중 모바일 4번 수정 | PENDING | | | | | | PENDING | | | | |
| 6 | PC 네트워크 복구 | PENDING | | | | | | PENDING | | | | |
| 7 | 재연결 후 4번 자동 복구 | PENDING | | | | | | PENDING | | | | |
| 8 | PC 프로그램 재시작 | PENDING | | | | | | PENDING | | | | |
| 9 | 시작 시 1~12 정합성 복구 | PENDING | | | | | | PENDING | | | | |
| 10 | 모바일 백그라운드 후 복귀 | PENDING | | | | | | PENDING | | | | |
| 11 | 관리자 강제 저장 성공 | PENDING | | | | | | PENDING | | | | |
| 12 | 일반 직원 강제 저장 차단 | PENDING | | | | | | PENDING | | | | |
| 13 | 자동발송 직전 모바일 수정 | PENDING | | | | | | PENDING | | | | |
| 14 | 수정된 최신 메시지로 발송 | PENDING | | | | | | PENDING | | | | |
| 15 | 서버 장애 + block 정책 | PENDING | | | | | | PENDING | | | | |
| 16 | 서버 장애 + cached 정책 | PENDING | | | | | | PENDING | | | | |
| 17 | 8시간 장시간 연결 | PENDING | | | | | | PENDING | | | | |
| 18 | Windows 절전 후 복귀 | PENDING | | | | | | PENDING | | | | |
| 19 | 모바일 Wi-Fi↔LTE 전환 | PENDING | | | | | | PENDING | | | | |
| 20 | 종료 후 thread/process 잔존 여부 | PENDING | | | | | | PENDING | | | | |

판정: PASS / FAIL / BLOCKED 중 하나. 17은 `long_run_8h_result.md`, 18은
`sleep_wake_result.md`, 19는 `network_switch_result.md`의 상세 결과를
요약해서 이 표에도 옮겨 적는다(중복 기록이지만 20개 시나리오 표에서 한눈에
보기 위함).
