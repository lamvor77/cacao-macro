# 라이선스 배포 가이드 (License Externalization Sprint)

이 문서는 `cacao_macro.exe`를 실제 사용자에게 배포하고, 이후 라이선스
기간을 갱신하는 절차를 설명한다. 대상 독자는 빌드/배포를 담당하는
관리자다(최종 사용자는 이 문서를 볼 필요가 없다).

## 1. 배경 — 왜 외부 파일 방식인가

과거(License Externalization Sprint 이전)에는 `license_build.json`이
PyInstaller `datas`로 exe 내부에 포함되어 있었다. 이 방식은 라이선스
기간을 한 번이라도 바꾸려면 **매번 exe를 재빌드**해야 했고, 재빌드 없이
파일만 교체하는 것이 구조적으로 불가능했다(exe가 실행될 때마다
`sys._MEIPASS`라는 새 임시 폴더에 압축 해제되는데, 그 안의 내용은 빌드
시점에 고정되어 이후 변경할 방법이 없기 때문).

이 스프린트에서 다음과 같이 구조를 바꿨다:

- `license_build.json`은 더 이상 exe 내부에 포함되지 않는다. 항상
  **exe와 같은 폴더**에 있는 외부 파일로만 읽는다
  (`core/license_manager.py`의 `get_external_license_path()`).
- `.env` 로딩 기준 경로도 `sys.executable`(exe 파일 위치) 기준으로
  통일했다(`config/settings.py`의 `get_runtime_base_dir()`) — 실행
  위치(현재 작업 디렉터리, 바탕화면 바로가기 등)가 달라도 항상 동일하게
  동작한다.
- 서명 알고리즘(HMAC-SHA256)과 메시지 형식(`BUILD|{start}|{end}`)은
  **변경하지 않았다** — 이번 스프린트는 파일 배치/조회 방식만 바꿨다.

결과적으로 **최초 1회만 재빌드**하면, 이후 라이선스 갱신은 exe를 다시
빌드하지 않고 `license_build.json` 파일만 교체하면 된다.

## 2. 배포 폴더 구성 (`dist/`)

최종 사용자에게 배포하는 폴더에는 다음 3가지가 **exe와 같은 폴더**에
있어야 한다.

```
dist/
├── cacao_macro.exe        ← 빌드된 실행 파일
├── .env                    ← LICENSE_SECRET_KEY + Supabase 설정만 포함
└── license_build.json      ← 서명된 사용 기간(start_date/end_date/signature)
```

`.env`는 관리자 로컬에서 쓰는 `.env`와 **다르다** — 반드시 아래 값만
포함해야 한다.

| 포함 여부 | 키 | 이유 |
|---|---|---|
| ✅ 포함 | `SUPABASE_ENABLED`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_OAUTH_CALLBACK_PORTS` 등 | 프로그램 실행에 필요한 클라우드 동기화 설정 |
| ✅ 포함 | `LICENSE_SECRET_KEY` | 실행 시점에 `license_build.json`의 서명을 재검증하는 데 필요 |
| ✅ 포함(고정값) | `LICENSE_ADMIN_UI_ENABLED=false` | 최종 사용자에게 라이선스 발급 UI를 노출하지 않는다 |
| ❌ 제외 | `LICENSE_ADMIN_PASSWORD` | 관리자 전용 값 — 최종 사용자 배포본에는 애초에 불필요(발급 UI 자체가 꺼져 있음) |
| ❌ 제외 | `service_role` 계열 키 | 이 프로젝트는 애초에 어떤 `.env`에도 이 값을 두지 않는다(anon key + RLS만 사용) |

`logs/`, `storage/`는 프로그램이 처음 실행될 때 exe 옆에 자동
생성되므로 미리 만들어 배포할 필요는 없다.

## 3. 라이선스 갱신 절차 (재빌드 없음)

기존 사용자의 라이선스 기간을 연장/변경할 때는 아래 순서만 따르면
된다. **exe는 절대 다시 빌드하지 않는다.**

1. 관리자 본인 PC에서(exe가 아니라 `python main.py`로 실행하는 개발
   모드) 프로그램의 "관리자 모드" 버튼을 눌러 `gui/panels/admin_panel.py`
   화면으로 들어간다(`LICENSE_ADMIN_UI_ENABLED=true`, `LICENSE_ADMIN_PASSWORD`
   가 설정된 관리자 로컬 `.env`가 필요하다).
2. 관리자 비밀번호 확인 후 새 시작일/종료일을 입력해 라이선스를
   발급한다 — 이 과정에서 새 `license_build.json`이 프로젝트 루트에
   저장된다(`LicenseManager.save_build_license()`).
3. 새로 생성된 `license_build.json`을, 사용자에게 이미 배포된
   `dist/license_build.json`(또는 사용자 PC의 exe 옆 파일)과 **교체**한다.
4. 사용자는 프로그램을 재시작하기만 하면 새 기간이 즉시 적용된다.

교체 전에는 반드시 기존 `license_build.json`을 백업한다(§5 참고).
같은 `LICENSE_SECRET_KEY`로 서명된 것이어야 하므로, 배포된 `.env`의
`LICENSE_SECRET_KEY` 값을 바꾸지 않는 한 이 절차만으로 충분하다.

## 4. 오류별 대응

`check_build_license()`가 실패하면 아래 5가지 사유 중 하나로 분류되어
사용자에게 대화상자로 표시되고 로그(`logs/YYYY-MM-DD.log`)에도 남는다.

| 표시 메시지 | 원인 | 조치 |
|---|---|---|
| 라이선스 파일이 없습니다. | exe 옆에 `license_build.json`이 없음 | 파일을 exe와 같은 폴더에 배치 |
| 라이선스 파일 형식이 올바르지 않습니다. | JSON 파싱 실패 또는 `start_date`/`end_date`/`signature` 필드 누락 | §3 절차로 재발급한 정상 파일로 교체 |
| 라이선스 서명이 올바르지 않습니다. | 파일이 변조되었거나, exe 옆 `.env`의 `LICENSE_SECRET_KEY`가 발급 시 사용한 키와 다름 | `.env`의 `LICENSE_SECRET_KEY`가 정확한지 확인 |
| 라이선스가 만료되었습니다. / 시작일이 아직 되지 않았습니다. | 정상 동작(기간 검증) | §3 절차로 새 기간의 파일 발급·교체 |
| 라이선스 검증 설정이 누락되었습니다. | exe 옆 `.env`에 `LICENSE_SECRET_KEY`가 없거나 비어 있음/`CHANGE_ME_`로 시작하는 예제값임 | `.env`에 실제 키 값을 설정 |

## 5. 백업 절차

- 새 라이선스를 배포하기 **전에** 기존 `license_build.json`을 날짜가
  포함된 이름으로 백업해 둔다(예: `backup/license-YYYYMMDD/license_build.json.bak`).
- `LICENSE_SECRET_KEY`를 변경하는 경우(키 로테이션)에는 그 키로 서명된
  **모든** 배포본의 `license_build.json`을 새 키로 다시 발급해야 한다
  (키가 다르면 기존 파일의 서명이 전부 무효가 된다) — 반드시 사전에
  영향받는 배포본 목록을 파악하고 진행한다.
- `backup/` 폴더는 비밀정보(과거 키로 서명된 파일 등)를 포함할 수 있어
  `.gitignore`에 등록되어 있다 — 항상 로컬 전용으로 취급한다.

## 6. 절대 커밋하면 안 되는 파일

아래는 모두 `.gitignore`에 이미 등록되어 있다. 실수로 `git add -f` 등으로
강제 추가하지 않도록 주의한다.

- `.env`, `dist/.env` (모든 비밀값)
- `license_build.json`, `dist/license_build.json` (서명값 자체는 비밀이
  아니지만, 배포 파일을 저장소에 둘 이유가 없고 관리 혼선을 막기 위해 제외)
- `backup/` (과거 키/라이선스 백업)
- `build/`, `dist/` (빌드 산출물)
- `storage/`, `logs/` (실행 중 생성되는 로컬 데이터)

## 7. HMAC 방식의 한계와 향후 개선 방향

현재 방식(HMAC-SHA256, 대칭키)은 배포된 `.env`에 **검증용 키**
(`LICENSE_SECRET_KEY`)가 그대로 들어 있다는 구조적 한계가 있다. 즉
이론적으로 이 키를 추출할 수 있는 사용자는 자기 PC에서 유효한
`license_build.json`을 직접 서명해 발급할 수 있다. 이는 "PC 잠금
없이, 관리자가 파일 배포로만 접근을 통제한다"는 이 프로젝트의 전제(
`core/license_manager.py` 상단 주석 참고)와 일치하는 수준의 보안이며,
일반적인 업무용 배포 시나리오에서는 허용 가능한 트레이드오프다.

더 강한 보증이 필요해지면(예: 키 추출 시도를 막아야 하는 경우),
**비대칭키(예: Ed25519) 서명**으로 전환하는 것을 고려할 수 있다 — 이
경우 배포본에는 검증 전용 공개키만 포함하고, 개인키는 관리자 PC를
벗어나지 않는다. 이번 스프린트에서는 사용자 지시에 따라 이 전환을
진행하지 않았다(기존 HMAC 설계와 서명 알고리즘을 바꾸지 않는 것이
이번 스프린트의 명시적 제약이었다) — 필요 시 별도 스프린트로 진행한다.
