# 카카오톡 단톡방 자동 메시지 전송 프로그램

Windows PC용 카카오톡 PC버전 자동 메시지 전송 프로그램. 선택한 단톡방에 메시지
1~12를 그룹(A/B/C/D)으로 나누어 운영시간(08:00~19:00) 동안 매시 0/15/30/45분에
자동 발송한다.

## 실행 방법

```powershell
pip install -r requirements.txt
python main.py
```

카카오톡 PC버전이 실행 중이어야 하며, Windows 환경 전용이다(win32gui/win32crypt 등
Windows API에 의존).

## 프로젝트 구조

```
project/
├── main.py              진입점 (중복실행 방지 → 로그 설정 → 라이선스 확인 → GUI 실행)
├── config/               상수/환경설정 (settings.py, cloud_settings.py, version.py)
├── core/                 카카오톡 제어, 메시지 전송, 자동발송 스케줄러, 동기화 상태 기계
├── gui/                  CustomTkinter 기반 UI
├── services/             클라우드 동기화/백업/진단/공유 메시지 서비스 계층
├── storage/              로컬 JSON 저장/불러오기
├── mobile/               내부 직원용 모바일 메시지 편집 PWA (Vite+React+TS, 별도 프로젝트)
├── scripts/              릴리스 패키징 등 빌드 도구
├── tests/                단위 테스트 (mock/fake만 사용, 실제 Supabase 연결 없음)
├── docs/                 설계 문서
└── logs/                 실행 로그
```

## 클라우드 동기화 (선택 기능, Phase 2)

기본값은 비활성화(`SUPABASE_ENABLED=false`)이며, 이 경우 프로그램은 클라우드
기능이 전혀 없던 것과 동일하게 로컬 JSON만으로 동작한다. 클라우드 동기화를
켜면 메시지 1~12를 Supabase에 백업/공유하고, 여러 기기 간 동기화할 수 있다.
상단의 "Google로 로그인" 버튼으로 로그인하며, 관리자 승인 전(`pending`)이거나
차단(`blocked`)된 계정은 클라우드 동기화가 자동으로 비활성 상태로 표시된다
(로컬 저장/자동발송에는 영향 없음). 자세한 내용은
[docs/PHASE2C_DESKTOP_INTEGRATION.md](docs/PHASE2C_DESKTOP_INTEGRATION.md)와
[docs/PHASE2D_GOOGLE_OAUTH.md](docs/PHASE2D_GOOGLE_OAUTH.md) 참고.

### 환경변수 설정

`.env.example`을 `.env`로 복사한 뒤 값을 채운다(`.env`는 git에 커밋하지 않음):

```
SUPABASE_ENABLED=false
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SYNC_INTERVAL_SECONDS=30
SUPABASE_DEVICE_ID=
```

- `SUPABASE_ANON_KEY`만 사용한다. `service_role` key는 절대 데스크톱 앱에 넣지 않는다.
- 실제 접근 제어는 Supabase RLS(Row Level Security)와 로그인 세션으로 이루어진다.

## 모바일 메시지 편집 (내부 직원용, Mobile 실시간 동기화 스프린트)

`mobile/` 폴더의 별도 웹(PWA)에서 1~12번 메시지의 제목/내용을 조회·수정할 수
있다. PC 프로그램의 실행 방식과 카카오톡 발송 방식은 전혀 바뀌지 않는다 —
모바일은 메시지 "내용"만 편집하고, 실제 발송은 항상 PC에서만 기존 방식대로
수행한다. PC와 모바일은 Supabase `shared_messages` 테이블을 통해 Realtime으로
양방향 동기화된다(30초/60초 폴링 없음 — 실제 변경이 있을 때만 이벤트가 온다).

자세한 내용은 [docs/mobile_message_editor.md](docs/mobile_message_editor.md),
동기화 구조는 [docs/realtime_sync.md](docs/realtime_sync.md),
DB 마이그레이션은 [docs/database_migration.md](docs/database_migration.md) 참고.

## 테스트

```powershell
python -m unittest discover -s tests -v
```

모든 테스트는 fake/mock으로 동작하며 실제 Supabase 프로젝트에 연결하지 않는다.

모바일(`mobile/`)은 별도 Node.js 프로젝트다:

```powershell
cd mobile
npm install
npm run typecheck
npm test
npm run build
```

## 문서

- [docs/PHASE2_CLOUD_SPEC.md](docs/PHASE2_CLOUD_SPEC.md) — Phase 2 전체 설계(로컬 서비스 계층, Phase 2A)
- [docs/PHASE2B_ROLES_AUTH_SPEC.md](docs/PHASE2B_ROLES_AUTH_SPEC.md) — 역할/인증/이력 설계 (Phase 2B)
- [docs/PHASE2C_DESKTOP_INTEGRATION.md](docs/PHASE2C_DESKTOP_INTEGRATION.md) — PC 프로그램 실연동 (Phase 2C)
- [docs/PHASE2D_GOOGLE_OAUTH.md](docs/PHASE2D_GOOGLE_OAUTH.md) — Google 로그인(PKCE) 구현 + Supabase 설정 방법 (Phase 2D)
- [docs/license_deployment_guide.md](docs/license_deployment_guide.md) — 라이선스 배포/갱신 절차
- [docs/mobile_message_editor.md](docs/mobile_message_editor.md) — 모바일 접속/사용/직원 계정 승인 방법
- [docs/realtime_sync.md](docs/realtime_sync.md) — PC↔모바일 Realtime 동기화 구조, 충돌/오프라인 처리
- [docs/database_migration.md](docs/database_migration.md) — shared_messages 스키마 적용 + 초기 데이터 이전
- [docs/operations_guide.md](docs/operations_guide.md) — 운영 전반(백업/복구/진단/배포/롤백)
- [docs/troubleshooting.md](docs/troubleshooting.md) — 자주 발생하는 문제와 해결 방법
