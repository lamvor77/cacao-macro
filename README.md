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
├── config/               상수/환경설정 (settings.py, cloud_settings.py)
├── core/                 카카오톡 제어, 메시지 전송, 자동발송 스케줄러
├── gui/                  CustomTkinter 기반 UI
├── services/             클라우드 동기화 서비스 계층 (선택 기능, Phase 2)
├── storage/              로컬 JSON 저장/불러오기
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

## 테스트

```powershell
python -m unittest discover -s tests -v
```

모든 테스트는 fake/mock으로 동작하며 실제 Supabase 프로젝트에 연결하지 않는다.

## 문서

- [docs/PHASE2_CLOUD_SPEC.md](docs/PHASE2_CLOUD_SPEC.md) — Phase 2 전체 설계(로컬 서비스 계층, Phase 2A)
- [docs/PHASE2B_ROLES_AUTH_SPEC.md](docs/PHASE2B_ROLES_AUTH_SPEC.md) — 역할/인증/이력 설계 (Phase 2B)
- [docs/PHASE2C_DESKTOP_INTEGRATION.md](docs/PHASE2C_DESKTOP_INTEGRATION.md) — PC 프로그램 실연동 (Phase 2C)
- [docs/PHASE2D_GOOGLE_OAUTH.md](docs/PHASE2D_GOOGLE_OAUTH.md) — Google 로그인(PKCE) 구현 + Supabase 설정 방법 (Phase 2D)
