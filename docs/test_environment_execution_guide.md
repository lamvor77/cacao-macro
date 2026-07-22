# 테스트 환경 배포 & E2E 검증 — 실행 가이드 (사용자용)

> ⚠️ **2026-07 운영 전환 이후 재검토 필요**: 이 문서가 전제하는 "운영과
> 완전히 분리된 테스트 프로젝트"였던 `cacao-macro-test`(`kdyxxkltafeuucijiyzp`)가
> 운영으로 승격됐고, 기존 운영 프로젝트(`nojdwuoronqmvpdptvlr`)는 삭제됐다.
> 즉 지금 이 저장소에는 이 문서가 요구하는 "운영과 분리된 테스트 프로젝트"가
> 더 이상 없다. 앞으로 이 문서의 절차를 다시 쓰려면(예: 새 테스트 프로젝트를
> 만들 때) 0번 준비물부터 새로 진행하고, 아래 10행의 프로젝트 참조도 새
> 운영 프로젝트 기준으로 갱신할 것.

이 문서는 Test Environment Deployment & E2E Validation Sprint에서 **사람이 직접
수행해야 하는 모든 단계**를 순서대로 정리한 것이다. Claude는 Supabase 계정
생성, Google OAuth 로그인 동의, Windows 절전 전환, 실제 휴대폰 네트워크 전환,
8시간 연속 관찰, 실제 카카오톡 발송을 대신 수행할 수 없으므로, 아래 단계를
직접 실행한 뒤 각 `docs/test_results/*.md` 파일에 결과를 채우거나, 결과를
대화로 알려주면 Claude가 대신 정리해 넣는다.

**절대 원칙**: 아래 어떤 단계도 운영 Supabase 프로젝트(현재 `kdyxxkltafeuucijiyzp` —
위 경고 참고)에 대해 실행하지 않는다. 모든 스크립트는 실행 전 `APP_ENV=test` 또는
`SUPABASE_ENVIRONMENT=test`가 설정되어 있는지, URL이 알려진 운영 호스트가
아닌지 확인하는 안전장치가 있다(설정 안 돼 있으면 스크립트가 스스로 중단한다).

---

## 0. 준비물 체크리스트

- [ ] 테스트 전용 Supabase 프로젝트(운영과 완전히 별개, 새로 생성 또는 이미 보유)
- [ ] 테스트 프로젝트 URL / anon key (service_role key는 이 스프린트 어디에도 필요 없다)
- [ ] Google OAuth 클라이언트(테스트 프로젝트용, 또는 기존 것을 재사용하며 Redirect URL만 추가)
- [ ] 테스트 계정 4개: employee_a, employee_b, admin_a, disabled_user (실제 이메일은 문서에 기록하지 않는다 — 별칭만 사용)
- [ ] Vercel/Netlify 계정 또는 내부 테스트 서버(모바일 Preview 배포용)
- [ ] 승인된 카카오톡 테스트 전용 단톡방 1개(운영 단톡방 아님)

---

## 1. 테스트 Supabase 프로젝트 준비

1. https://supabase.com 에서 새 프로젝트 생성(또는 기존 별도 테스트 프로젝트 사용).
2. 프로젝트 URL과 anon key를 복사해 둔다(SQL Editor > Settings > API에서 확인).
3. `docs/sql/phase2b_schema.sql`이 먼저 적용되어 있어야 한다(`app_users`, `fn_can_edit()`, `fn_is_admin()`, `fn_is_approved()` 등). 새 프로젝트라면 Supabase SQL Editor에서 이 파일 전체를 먼저 실행한다.
4. `docs/sql/phase4_admin_rpc.sql`도 필요하면 함께 적용한다(관리자 승인/차단 RPC).

## 2. shared_messages SQL 적용 (섹션 3)

1. `docs/sql/shared_messages_realtime.sql` 파일 전체를 SQL Editor에 붙여넣고 실행한다.
   - 적용 전: 파일을 `backup/sql-backup-<날짜>/shared_messages_realtime.sql`로 복사해 두고, SHA-256을 기록한다:
     ```powershell
     Get-FileHash docs\sql\shared_messages_realtime.sql -Algorithm SHA256
     ```
   - 적용 대상 프로젝트 URL이 운영이 아닌지 **반드시 2회** 확인한다(주소창 URL과 Settings > API의 프로젝트 참조 ID 둘 다 확인).
   - 적용 시각을 기록한다.
2. 오류가 없는지 확인한다. 일부 구문만 적용된 상태(예: 테이블은 생겼는데 RPC는 실패)가 아닌지 SQL Editor의 실행 로그를 끝까지 확인한다.
3. **같은 SQL을 다시 한번 그대로 실행**한다(idempotency 확인). 두 번째 실행도 치명적 오류 없이 끝나야 한다(`raise notice`류의 안내 메시지는 정상).
4. 결과를 `docs/test_results/schema_check_result.md`의 상단 "SQL 적용 기록" 표에 채운다.

## 3. 스키마 점검 스크립트 실행 (섹션 4)

테스트 프로젝트용 `.env`를 준비한다(운영 `.env`와는 별도 파일 — 아래 6절 test-runtime 폴더 참고). 최소한 다음이 필요하다:

```
APP_ENV=test
SUPABASE_ENVIRONMENT=test
SUPABASE_ENABLED=true
SUPABASE_URL=<테스트 프로젝트 URL>
SUPABASE_ANON_KEY=<테스트 프로젝트 anon key>
```

```powershell
python scripts\check_shared_messages_schema.py
# 승인된 테스트 계정으로 로그인해 access token을 얻었다면(Supabase 대시보드 Authentication
# 또는 앱 로그인 후 진단화면에서 확인 가능한 세션) 데이터 수준까지 확인:
python scripts\check_shared_messages_schema.py --access-token <employee_a의 access token>
```

출력 전체를 `docs/test_results/schema_check_result.md`에 붙여넣는다(민감정보는 이미 마스킹되어 출력되지만, access token 값 자체는 절대 복사해 넣지 않는다 — 명령만 기록).

Realtime publication은 REST로 완전히 확인할 수 없으므로 Supabase 대시보드에서 직접 확인한다:
- Database > Replication > `supabase_realtime` publication에 `shared_messages`가 포함되어 있는지
- `shared_messages` 테이블의 Replica Identity가 FULL인지 (Database > Tables > shared_messages > Edit)

## 4. 테스트 사용자 준비 (섹션 5)

Supabase 대시보드 Authentication에서 employee_a/employee_b/admin_a/disabled_user 4개 계정을 만들고(또는 Google 로그인으로 최초 1회 로그인시켜 `app_users`에 자동 생성되게 한 뒤), SQL Editor에서 상태/역할을 지정한다:

```sql
update app_users set status = 'approved', role = 'editor' where email = '<employee_a 이메일>';
update app_users set status = 'approved', role = 'editor' where email = '<employee_b 이메일>';
update app_users set status = 'approved', role = 'admin'  where email = '<admin_a 이메일>';
-- disabled_user는 status를 'pending' 또는 'blocked'로 둔다(기본값이 pending이므로 보통 아무 것도 안 해도 됨).
```

각 계정으로 로그인해 access token을 얻는다(가장 쉬운 방법: PC 프로그램 또는 모바일에 해당 계정으로 로그인한 뒤, 브라우저 개발자 도구 Application 탭에서 Supabase 세션의 `access_token` 값을 복사 — **이 값을 문서나 로그에 붙여넣지 않는다**, 아래 스크립트 실행 시 환경변수로만 잠깐 사용한다).

```powershell
$env:TEST_EMPLOYEE_A_TOKEN = "<employee_a access token>"
$env:TEST_EMPLOYEE_B_TOKEN = "<employee_b access token>"
$env:TEST_ADMIN_TOKEN      = "<admin_a access token>"
$env:TEST_DISABLED_TOKEN   = "<disabled_user access token>"
python scripts\check_rls_rpc_permissions.py --message-no 12
```

이 스크립트는 `APP_ENV=test`가 아니거나 URL이 운영으로 알려진 호스트면 즉시 중단한다. 정상 실행되면 각 역할이 무엇을 할 수 있고 없는지 `[PASS]/[FAIL]` 목록을 출력하고, 성공한 쓰기 테스트는 스크립트가 자동으로 원본 내용을 복원한다. 출력 전체를 `docs/test_results/rls_rpc_result.md`에 붙여넣는다.

실행이 끝나면 반드시 토큰 환경변수를 지운다:
```powershell
Remove-Item Env:TEST_EMPLOYEE_A_TOKEN, Env:TEST_EMPLOYEE_B_TOKEN, Env:TEST_ADMIN_TOKEN, Env:TEST_DISABLED_TOKEN
```

## 5. Google OAuth Redirect URL 설정

Supabase 대시보드 Authentication > URL Configuration에서 Redirect URLs에 다음을 추가한다(PC 프로그램의 로컬 콜백 + 모바일 Preview 도메인):
- `http://127.0.0.1:*/oauth/**` (PC, 이미 기존 문서에 있는 패턴 재사용)
- `https://<모바일 Preview 도메인>/**`

## 6. 테스트 실행 폴더 준비 (섹션 6)

운영 EXE와 절대 섞이지 않도록 분리된 폴더를 만든다:

```powershell
python -m PyInstaller cacao_macro.spec --noconfirm
python scripts\setup_test_runtime.py --target test-runtime
```

`test-runtime\.env.example`을 `test-runtime\.env`로 복사한 뒤 위 3절의 값(APP_ENV=test 포함)을 채운다. `test-runtime\cacao_macro.exe`를 실행한다.

확인할 것(체크리스트, 결과는 `docs/test_results/environment_check.md`에 기록):
- [ ] 창 제목/제목표시줄에 "TEST ENVIRONMENT" 표시
- [ ] 라이선스 정상 인식
- [ ] Google 로그인 정상(테스트 계정)
- [ ] Supabase 인증 정상
- [ ] shared_messages 1~12 로딩
- [ ] Realtime 상태가 "실시간 연결됨"
- [ ] 마지막 동기화 시각 표시
- [ ] 진단 화면 상단에 "환경: TEST ENVIRONMENT" 표시
- [ ] 진단 화면 message source 섹션 표시
- [ ] 작업 관리자에서 cacao_macro.exe 관련 스레드가 비정상적으로 여러 개 뜨지 않음
- [ ] 레거시 메시지 시스템(30초 폴링) 여전히 동작
- [ ] `test-runtime\storage`에만 데이터가 쌓이고 운영 storage는 변화 없음

## 7. 모바일 Preview 배포 (섹션 7)

```powershell
cd mobile
npm run build
```

Vercel/Netlify에 Preview로 배포(각 서비스의 CLI 또는 대시보드 사용). 환경변수:
```
VITE_APP_ENV=test
VITE_SUPABASE_URL=<테스트 프로젝트 URL>
VITE_SUPABASE_ANON_KEY=<테스트 프로젝트 anon key>
```

확인할 것 → `docs/test_results/environment_check.md`에 기록:
- [ ] 화면 상단에 빨간 "TEST ENVIRONMENT" 배너
- [ ] 로그인/승인 직원 접근/비활성 직원 차단
- [ ] 1~12 메시지 목록, Realtime 연결됨
- [ ] PWA 설치, 서비스워커 등록
- [ ] 새로고침/로그아웃/토큰 만료 처리

## 8. E2E 20개 시나리오 (섹션 8)

`docs/e2e_realtime_test_plan.md`의 20개 시나리오를 실제로 수행하고, 결과를 `docs/test_results/e2e_result.md`에 시나리오별로 기록한다(계정은 별칭만, 실제 결과/PASS-FAIL-BLOCKED/문제/수정여부 포함).

## 9. 실제 카카오톡 발송 테스트 (섹션 9)

승인된 테스트 단톡방에서만 진행한다. 테스트 메시지 형식:
```
[TEST]
자동화 검증 메시지
message_no: N
revision: N
발송시각: HH:MM:SS
```
block/cached 정책을 각각 `test-runtime\.env`의 `MESSAGE_SEND_OFFLINE_POLICY`로 바꿔가며 재현한다(정책 변경 후 프로그램 재시작 필요). 결과를 `docs/test_results/kakao_send_result.md`에 기록한다. **테스트 종료 후 반드시 해당 message_no를 원래 내용으로 RPC를 통해 복원**한다(모바일/PC 편집 화면에서 정상 저장하면 자동으로 history에 남는다).

## 10. 8시간 장시간 테스트 (섹션 10)

```powershell
Get-Process -Name cacao_macro | Select-Object Id   # PID 확인
python scripts\health_snapshot.py --pid <PID> --log-file test-runtime\logs\<최신 로그파일> --output docs\test_results\long_run_8h_raw.csv --interval-seconds 300
```

이 스크립트를 별도 터미널에서 8시간 동안 켜 두면 5분 간격으로 메모리/스레드/CPU/재연결 여부를 CSV에 자동 기록한다(Ctrl+C로 언제든 중단 가능, 그때까지 기록은 남는다). 8시간 후 CSV를 열어 시작 대비 rss_mb/num_threads 추이를 확인하고, 결과 요약(지속 상승 여부, thread 누적 여부)을 `docs/test_results/long_run_8h_result.md`에 기록한다.

## 11. Sleep/Wake 테스트 (섹션 11)

`health_snapshot.py`를 켜 둔 채로 사용자 스펙 11절의 1~10단계를 그대로 수행한다. 복귀 후 SUBSCRIBED로 돌아오는 데 걸린 시간을 진단 화면/로그에서 확인해 `docs/test_results/sleep_wake_result.md`에 기록한다(60초 이내 PASS, 60초 이상 WARN, 미복구 FAIL).

## 12. 네트워크 전환 테스트 (섹션 12)

PC 네트워크 어댑터 비활성화/복구, 모바일 Wi-Fi↔LTE 전환 절차를 스펙 12절대로 수행하고 `docs/test_results/network_switch_result.md`에 기록한다.

## 13~14. 문제 처리와 회귀 테스트

발견한 문제는 P0/P1/P2로 분류해 나에게 알려주면, P0/P1은 최소 수정 후 다시:
```powershell
python -m unittest discover -s tests
python -m PyInstaller cacao_macro.spec --noconfirm
cd mobile; npm run typecheck; npm test; npm run build
```
을 재실행해 회귀 여부를 확인한다.

## 15. 결과를 Claude에게 전달하는 방법

각 `docs/test_results/*.md` 파일을 직접 채워도 되고, 실행한 명령의 출력과 관찰 결과를 대화로 붙여넣어도 된다(토큰/이메일/실제 메시지 본문은 붙여넣지 않는다) — Claude가 해당 파일에 정리해 넣고, 최종적으로 `docs/test_results/release_readiness_result.md`에 READY/CONDITIONAL READY/NOT READY 판정을 근거와 함께 작성한다.
