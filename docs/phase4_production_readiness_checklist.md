# Production Readiness Sprint — 테스트 체크리스트

작성일: 2026-07-18. 각 항목 옆에 담당 주체와 현재 상태를 표시한다.
"자동화(Claude)"는 이 세션에서 직접 실행 가능한 항목, "사용자 필요"는 실제
Google 계정 로그인, Supabase SQL Editor, 또는 실제 Windows GUI 클릭 조작처럼
Claude가 이 환경에서 직접 수행할 수 없는 항목이다(아래 [실행 주체 관련 중요
안내] 참고).

## 인증/권한

- [ ] Google Login — **사용자 필요** (실제 브라우저 OAuth 동의 화면)
- [ ] 최초 로그인 (app_users pending 자동 생성) — **사용자 필요** + SQL 적용 선행
- [ ] 승인 대기(pending) 상태 확인 — **사용자 필요** + SQL 적용 선행
- [ ] 승인(approve) — **사용자 필요** + SQL 적용 선행
- [ ] Role 변경 — **사용자 필요** + SQL 적용 선행
- [ ] Admin 승격 — **사용자 필요** + SQL 적용 선행
- [ ] 마지막 Admin 보호 — 자동화(Claude) 가능(Fake 기준, `test_local_pending_vs_conflict.py`류 아님 — `docs/sql/phase4_admin_rpc_test_plan.md` D-3 절차) / **실제 검증은 사용자 필요**
- [ ] 자기 강등 방지 — 자동화(Claude) 로직 검증 완료(`test_admin_ui_permissions.py` test_9) / **실제 RPC 검증은 사용자 필요**
- [ ] 자기 차단 방지 — 자동화(Claude) 로직 검증 완료(`test_admin_ui_permissions.py` test_8) / **실제 RPC 검증은 사용자 필요**
- [ ] 차단 — **사용자 필요** + SQL 적용 선행
- [ ] 차단 해제 — **사용자 필요** + SQL 적용 선행
- [ ] Audit Log 생성 — **사용자 필요** + SQL 적용 선행
- [ ] Audit Log 조회 — **사용자 필요** + SQL 적용 선행
- [ ] Editor 권한 — 자동화(Claude) 로직 검증 완료(`test_cloud_sync_coordinator.py` test_14 등) / 실제 계정 검증은 사용자 필요
- [ ] Viewer 권한 — 자동화(Claude) 로직 검증 완료(`test_cloud_sync_coordinator.py` test_13) / 실제 계정 검증은 사용자 필요
- [ ] Pending 권한 — 자동화(Claude) 로직 검증 완료(`test_cloud_sync_coordinator.py` test_11) / 실제 계정 검증은 사용자 필요
- [ ] Blocked 권한 — 자동화(Claude) 로직 검증 완료(`test_cloud_sync_coordinator.py` test_12) / 실제 계정 검증은 사용자 필요

## 클라우드 동기화

- [ ] Cloud Sync — 자동화(Claude): 기존 회귀 스위트 재실행 가능
- [ ] Offline — 자동화(Claude): 기존 회귀 스위트 재실행 가능
- [ ] Startup Sync — 자동화(Claude): 기존 회귀 스위트 재실행 가능(초기 동기화 관련 테스트)
- [ ] Conflict — 자동화(Claude): 기존 회귀 스위트 재실행 가능(LOCAL_PENDING/CONFLICT 구분 포함)
- [ ] Message Restore — 부분: `fn_restore_message` RPC는 SQL에 존재하나 Python 클라이언트/UI 미구현 — **범위 확인 필요**(아래 안내 참고)

## 배포/운영

- [x] License — **완료(License Externalization Sprint)**: `license_build.json`이
  더 이상 exe 내부(PyInstaller datas)에 포함되지 않고 exe와 같은 폴더의 외부
  파일로만 조회된다(`sys._MEIPASS` 폴백 없음, `core/license_manager.py`의
  `get_external_license_path()`). `.env` 로딩도 `sys.executable` 기준으로
  통일되어 CWD에 의존하지 않는다(`config/settings.py`의
  `get_runtime_base_dir()`). 이번 스프린트에서 승인된 **단 1회** 재빌드를
  수행했고, 재빌드된 실제 exe에서 (1) 다른 CWD에서 실행 시 정상 검증,
  (2) exe를 건드리지 않고 `license_build.json`만 교체(정상→만료→정상 복원)
  했을 때 매번 올바르게 반영됨을 확인했다(exe SHA-256 전 과정 불변 확인).
  이후 라이선스 갱신은 재빌드 없이 파일 교체만으로 가능 — 절차는
  `docs/license_deployment_guide.md` 참고. 실제 발급 UI 클릭 자체는 여전히
  **사용자 필요**(관리자 비밀번호 입력 등 대화상자 상호작용).
- [ ] Git — 자동화(Claude): 현재 git 상태/히스토리 점검 가능(이전 세션에서 이미 정리됨)
- [x] EXE — **완료**: PyInstaller 설치 확인됨(6.20.0), 이번 스프린트에서 실제
  재빌드 성공(`dist/cacao_macro.exe`), 내부 아카이브에 `license_build.json`
  없음을 확인함. 배포 시 `dist/` 폴더에 exe와 함께 `.env`/`license_build.json`
  을 수동 배치해야 하며 절차는 `docs/license_deployment_guide.md` 참고.
- [ ] 실제 Windows 테스트 — **사용자 필요** (Claude는 마우스/키보드로 GUI를 조작할 수 없음). 단, 이번 스프린트에서 자동화된 프로세스 실행/종료 및 로그 확인(라이선스 검증 성공/실패, CWD 독립성)은 완료함 — 버튼 클릭·시각적 확인·Google 로그인 등은 여전히 미완료.
- [ ] 업데이트 후 기존 DB 유지 — **사용자 필요** + SQL 적용 선행(마이그레이션이 기존 `app_users`/`messages` 데이터를 보존하는지 실제 데이터로 확인)

## [실행 주체 관련 중요 안내]

이 체크리스트 25개 항목 중 상당수가 다음 이유로 Claude가 이 세션에서 직접
"실제로" 수행할 수 없다 — 각 항목 옆에 "사용자 필요"로 표시했다.

1. **`docs/sql/phase4_admin_rpc.sql` 적용(목표 1)** — Claude는 Supabase SQL
   Editor에 접근할 수 없고, 이 환경에는 `supabase` CLI도 `psql`도 설치되어
   있지 않다(확인함). `.env`에는 의도적으로 anon key만 있고 DDL을 실행할 수
   있는 DB 비밀번호/service_role은 없다(이 프로젝트 전체의 보안 원칙이자
   이번 스프린트에서도 절대 추가하면 안 되는 값). 즉 SQL 적용은 **사용자가
   Supabase 대시보드에서 직접 실행**해야 하는 단계다 — 이 단계가 끝나야 목표
   2/3/4/8과 체크리스트의 인증/권한 실제 검증 항목 대부분이 가능해진다.
2. **Google Login(목표와 체크리스트 공통)** — 실제 OAuth 동의 화면은 브라우저
   상호작용이 필요하다. Claude는 이 세션에서 브라우저를 열어 로그인을 완료할
   수 없다.
3. **실제 Windows 테스트(목표 10)** — Claude는 Bash/파일 편집 도구만 쓸 수
   있고, 실제 GUI 창을 마우스로 클릭하거나 눈으로 화면을 보며 확인할 수
   없다. `python main.py`를 백그라운드로 실행해 크래시 여부·로그는 확인할 수
   있지만, "버튼이 잘 보이는지", "다이얼로그가 예쁘게 뜨는지" 같은 시각적
   확인은 불가능하다.

**이 세션에서 지금 바로 실행 가능한 것**: 기존 자동화 회귀 스위트 재실행
(Cloud Sync/Offline/Conflict 관련 목표 5~7 및 다수 권한 로직), git 상태 점검,
EXE 빌드 시도. 이 세 가지부터 먼저 진행하고, 결과를 보고한 뒤 SQL
적용·실제 로그인처럼 사용자 개입이 필요한 단계를 어떻게 진행할지 안내를
요청한다.
