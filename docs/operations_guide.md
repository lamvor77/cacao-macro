# 운영 가이드 (Release Candidate Sprint 1)

배포/운영을 담당하는 관리자를 위한 문서다. 최종 사용자용 문서는
`docs/release_readme_ko.txt`(릴리스 패키지에는 `README.txt`로 포함됨), 설치
절차는 `INSTALL.md`를 참고한다.

## 목차

1. [라이선스 발급·교체](#1-라이선스-발급교체)
2. [사용자 배포](#2-사용자-배포)
3. [백업·복구](#3-백업복구)
4. [진단정보 해석](#4-진단정보-해석)
5. [로그 확인](#5-로그-확인)
6. [Supabase 장애](#6-supabase-장애)
7. [Google OAuth 장애](#7-google-oauth-장애)
8. [오프라인 사용](#8-오프라인-사용)
9. [충돌 처리](#9-충돌-처리)
10. [신규 버전 배포 절차](#10-신규-버전-배포-절차)
11. [롤백 절차](#11-롤백-절차)
12. [모바일 메시지 편집 운영](#12-모바일-메시지-편집-운영)

---

## 1. 라이선스 발급·교체

상세 절차는 `docs/license_deployment_guide.md`에 이미 정리되어 있다 — 이
문서에서는 다시 쓰지 않고 참조만 한다. 핵심만 요약하면:

- `license_build.json`은 exe 내부가 아니라 exe와 같은 폴더의 **외부 파일**이다.
- 갱신은 새 파일을 발급해 기존 파일과 **교체**하기만 하면 된다 — exe
  재빌드가 필요 없다.
- `LICENSE_SECRET_KEY`를 바꾸지 않는 한, 배포된 `.env`도 그대로 둔다.

## 2. 사용자 배포

`INSTALL.md`의 절차를 그대로 따르면 된다. 배포 패키지 구성은 아래
"[10. 신규 버전 배포 절차](#10-신규-버전-배포-절차)"에서 설명하는
`release/cacao_macro-v{버전}/` 폴더를 그대로 전달하면 된다.

## 3. 백업·복구

- 자동 백업: 프로그램 정상 종료 시, 그날 아직 auto 백업이 없으면 1개
  생성한다(`services/backup_service.py`의 `should_create_auto_backup_today()`).
  하루 최대 1개, 최근 30개까지 자동 보관하고 그 이상은 오래된 것부터
  정리한다(`cleanup_old_backups()`).
- 수동 백업: 프로그램의 "진단정보" → "백업 관리..." → "지금 백업"에서 언제든
  만들 수 있다(일반 사용자도 가능).
- 복구: 같은 화면의 "복구" 버튼 — **운영 관리자만** 수행할 수 있다(비로그인
  오프라인 단독 사용자는 예외적으로 허용 — `gui/panels/backup_panel.py`의
  `can_restore_or_delete()` 참고). 복구 직전 현재 데이터를 자동으로
  `pre_restore` 백업으로 저장하고, 복구 중 어떤 단계에서 실패해도 기존
  데이터는 항상 보존된다(롤백).
- 복구 후 클라우드는 자동으로 덮어쓰지 않는다 — 다음 로그인/동기화 시 기존
  LOCAL_PENDING/CONFLICT 판정 체계가 그대로 적용된다.
- 백업 ZIP에는 `.env`, `license_build.json`, 로그인 세션(`session.dat`) 등
  비밀정보가 **절대 포함되지 않는다**(`services/backup_service.py`의
  화이트리스트 방식 — storage 최상위 `*.json` + `storage/cloud_sync/`의
  `messages.json`/`local_sync_state.json`/`message_cache.json`만 포함).

## 4. 진단정보 해석

"진단정보" 화면(또는 "진단정보 복사" 결과)에서 자주 보는 값의 의미:

| 항목 | 정상 예 | 의미 |
|---|---|---|
| 실행 모드 | EXE | 개발 모드(`python main.py`)로 실행 중이면 "Development" — 배포본에서 이게 보이면 잘못 배포된 것 |
| Supabase 네트워크 상태 | 연결됨 | "확인 안 함"은 오류가 아니다 — 매 새로고침마다 자동으로 네트워크를 확인하지는 않는다(원칙: 조회만으로 부수효과를 만들지 않음) |
| 라이선스 상태 | 정상 | 그 외 값은 `docs/license_deployment_guide.md`의 오류별 대응표 참고 |
| Pending/Conflict | 0 / 0 | 0보다 크면 각각 "클라우드 업로드 대기 중" / "실제 충돌 — 확인 필요" |

## 5. 로그 확인

- 위치: `logs/YYYY-MM-DD.log`(exe와 같은 폴더). "진단정보" → "로그 폴더
  열기" 버튼으로 바로 열 수 있다.
- DEBUG/INFO는 파일에만 기록되고, WARNING 이상만 프로그램 화면 로그창에도
  보인다(`utils/logger_setup.py`).
- 라이선스 실패, 동기화 실패, 백업 실패는 모두 여기 남는다 — 문의를 받으면
  가장 먼저 확인한다.

## 6. Supabase 장애

- `SUPABASE_ENABLED=false`이거나 URL/키가 비어 있으면 프로그램은 클라우드
  기능 없이 로컬 전용으로 정상 동작한다(설계상 의도된 폴백 — 오류가 아님).
- 실제 장애(네트워크 문제, Supabase 다운)가 발생해도 로컬 저장/발송은 영향을
  받지 않는다 — "오프라인 우선" 원칙(§8 참고).
- 화면 오른쪽 위 클라우드 상태 라벨이 "동기화 실패"/"오프라인"으로 바뀐다 —
  legacy messages는 상시 polling이 없으므로, 재시도는 다음 저장/발송 시작·
  종료/15분 조건부 push/수동 새로고침/발송 직전 확인 중 하나가 일어날 때
  자동으로 이루어진다(가장 빠른 방법은 "메시지 새로고침" 버튼).

## 7. Google OAuth 장애

- 로그인 버튼을 눌렀는데 브라우저가 안 열리면: 기본 브라우저 설정을
  확인한다.
- "로그인 시간이 초과되었습니다": 120초 안에 로그인을 완료하지 못한 경우 —
  다시 시도한다.
- 콜백 포트 충돌이 의심되면 `.env`의 `SUPABASE_OAUTH_CALLBACK_PORTS`로 고정
  포트를 지정하고, Supabase 대시보드의 Redirect URLs에도 동일하게
  등록한다(`docs/PHASE2D_GOOGLE_OAUTH.md` 참고).

## 8. 오프라인 사용

이 프로그램은 처음부터 "로컬 우선(offline-first)"으로 설계되었다 —
클라우드 동기화는 항상 선택 기능이다.

- 로그인하지 않아도, 인터넷이 없어도 저장/불러오기/자동 발송 전체가
  정상 동작한다.
- 클라우드 관련 기능(로그인, 동기화 상태, 운영 관리자)만 비활성화되거나
  "미설정"/"오프라인"으로 표시된다.

## 9. 충돌 처리

- CONFLICT는 "같은 메시지를 로컬과 클라우드에서 서로 다르게 수정했고, 어느
  쪽이 최신인지 프로그램이 자동으로 판단할 수 없는" 상태다.
- 진단정보 화면의 Conflict 건수로 발생 여부를 알 수 있다.
- 이번 스프린트에서 충돌 자체의 해결 UI는 변경하지 않았다(Phase 2E 후속
  스프린트에서 이미 구현된 LOCAL_PENDING/CONFLICT 구분 로직을 그대로
  사용한다) — 로컬 백업 복구도 이 판정 체계에 자연스럽게 편입된다(§3 참고).

## 10. 신규 버전 배포 절차

1. `config/version.py`의 `_FALLBACK_VERSION`/`_FALLBACK_CHANNEL`을 새 버전
   번호로 갱신한다(유일한 하드코딩 지점).
2. 전체 회귀 테스트 통과를 확인한다: `python -m unittest discover -s tests`
3. **딱 1번** 재빌드한다: `python -m PyInstaller cacao_macro.spec --noconfirm`
4. `scripts/build_release.py`를 실행해 `release/cacao_macro-v{버전}/` 릴리스
   패키지를 만든다 — exe 해시/크기 기록, `release_manifest.json`/
   `checksums.sha256`/`release/version.json` 생성, `.env.example`/
   `license_build.json`/`README.txt`/`INSTALL.md` 포함까지 자동으로
   수행한다.
5. 패키지 내부에 실제 `.env`, 토큰, 관리자 비밀번호가 없는지 정적 검사한다
   (같은 스크립트가 자동으로 검사하고 결과를 출력한다).
6. 패키지를 배포 대상 PC에 그대로 복사하고, `INSTALL.md`대로 `.env.example`
   → `.env`로 바꿔 실제 값을 채운다.

## 11. 롤백 절차

- **exe 롤백**: 이전 버전의 `release/cacao_macro-v{이전버전}/cacao_macro.exe`로
  교체한다(각 릴리스 패키지를 삭제하지 않고 보관해 둘 것을 권장). 라이선스
  형식(HMAC 서명 방식)이 바뀌지 않는 한 `.env`/`license_build.json`은 그대로
  재사용할 수 있다.
- **데이터 롤백**: 문제가 데이터 쪽이라면 exe를 되돌릴 필요 없이 §3의 백업
  복구 기능만으로 충분한 경우가 많다 — 먼저 이쪽을 시도한다(더 안전하고
  빠르다).
- **Supabase 스키마 롤백**: 이 스프린트는 실제 Supabase SQL을 실행하지
  않았다 — 스키마 변경이 없으므로 별도 롤백 대상이 없다.

## 12. 모바일 메시지 편집 운영

Mobile 실시간 동기화 스프린트에서 추가된 기능이다. 사용자 안내는
[docs/mobile_message_editor.md](mobile_message_editor.md), 기술 구조는
[docs/realtime_sync.md](realtime_sync.md), DB 적용 절차는
[docs/database_migration.md](database_migration.md) 참고 — 여기서는 운영
관점에서 자주 필요한 것만 요약한다.

- 직원 계정 승인/차단은 PC의 "운영 관리자" 화면(Phase 4-2)을 그대로 쓴다 —
  모바일 전용 관리 화면은 없다.
- **퇴사자 계정 비활성화**: 운영 관리자 화면에서 해당 계정을 "차단(block)"
  하면 즉시 모바일/PC 양쪽에서 shared_messages 접근이 막힌다(RLS가
  `fn_is_approved()`를 매 요청마다 재검사하므로, 이미 로그인된 세션이 있어도
  다음 요청부터 차단된다).
- 모바일 배포 파일(`mobile/dist/`)은 이 저장소에 커밋하지 않는다 — 별도
  정적 호스팅에만 올린다.
- `shared_messages` 스키마를 변경할 때는 PC(`services/shared_message_service.py`)와
  모바일(`mobile/src/types.ts`)의 컬럼 정의를 함께 갱신해야 한다
  (docs/database_migration.md §5 참고).
