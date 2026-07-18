# Phase 2D — Windows 데스크톱 Google OAuth 로그인

Phase 2C까지는 `AuthService.login_with_google()`이 `NotImplementedError`를 던지는
스텁이어서, 클라우드 동기화가 항상 "로그인 필요" 상태에서 멈췄다. 이번 단계에서
실제 브라우저 기반 Google 로그인(Supabase 공식 PKCE 흐름)을 구현했다.

---

## 0. API 검증 방법 (추측 아님)

구현 전 실제 설치된 `supabase==2.31.0`(`supabase_auth` 패키지)의
`SyncGoTrueClient` 소스를 직접 읽고 다음을 확인했다:

- `sign_in_with_oauth(credentials)`가 `flow_type="pkce"`(현재 `SyncClientOptions`의
  기본값)일 때 PKCE `code_verifier`/`code_challenge`를 **클라이언트 인스턴스 자체의
  내부 저장소**에 자동 생성/보관한다는 것 — 즉 `exchange_code_for_session()`을
  **같은 Client 인스턴스**로 호출하면 `code_verifier`를 우리가 직접 넘기지 않아도
  SDK가 알아서 그 값을 찾아 쓴다(소스: `_get_url_for_provider`, `exchange_code_for_session`).
- `set_session()`이 만료된 세션을 자동으로 refresh한다는 것(소스 확인).
- `sign_out()`이 `.admin.sign_out(access_token, scope)`를 부르지만 이 호출은
  service_role이 아니라 **사용자 본인의 access_token**을 Authorization으로 써서
  `/auth/v1/logout`을 호출하는, 일반 사용자도 쓸 수 있는 엔드포인트라는 것(소스 확인
  — service_role key가 전혀 필요 없다).
- `postgrest.exceptions.APIError.code`가 Postgres 오류 코드(RLS 위반 시 `"42501"`)를
  담고 있어, 이를 순수 네트워크 오류와 구분하는 데 썼다는 것.

Supabase 대시보드 설정 관련 사실(아래 2절)은 Supabase 공식 문서
(`supabase.com/docs/guides/auth/redirect-urls`, `.../social-login/auth-google`)를
직접 조회해 확인했다.

---

## 1. 로그인 흐름

```
"Google로 로그인" 버튼 클릭 (별도 스레드에서 실행 — 메인 UI 스레드를 막지 않음)
    │
    ▼
AuthService.login_with_google()
    │
    ├─ 1) OAuthCallbackServer 생성 — 127.0.0.1의 OS가 골라준 임시 포트에 바인딩
    │     (고정 포트 하드코딩 없음. 무작위 토큰을 경로에 심어 "state" 역할을 한다 —
    │      아래 "state/PKCE 검증 설계" 참고)
    │
    ├─ 2) client.auth.sign_in_with_oauth({"provider":"google","options":{"redirect_to":...}})
    │     → PKCE code_verifier/code_challenge를 SDK가 내부에서 자동 생성·보관, 인가 URL 반환
    │
    ├─ 3) webbrowser.open(인가_URL) — 시스템 기본 브라우저에서 Google 로그인 화면 표시
    │
    ├─ 4) OAuthCallbackServer.wait_for_callback(timeout=120초) — 블로킹 대기
    │     (이 시점부터 브라우저가 리디렉션할 때까지 이 스레드만 멈춘다, UI는 정상)
    │
    ├─ 5) code 수신 → client.auth.exchange_code_for_session({"auth_code":code,
    │     "code_verifier":"", "redirect_to":...}) — 빈 문자열을 넘기면 2)번에서
    │     저장해둔 verifier를 SDK가 자동으로 찾아 씀
    │
    ├─ 6) 세션을 Windows DPAPI로 암호화해 storage/cloud_sync/session.dat에 저장
    │
    └─ 7) AuthResult(success=True/False, ...) 반환
    │
    ▼
MainWindow._on_login_result() (메인 스레드로 self.after(0, ...)를 통해 되돌아옴)
    │
    ├─ 성공 → CloudSyncCoordinator.on_login_success() 호출 → 즉시 초기 동기화 재개
    └─ 실패(취소/시간초과/오류) → 로그에 기록 + 실패 팝업 1회 (반복 팝업 없음)
```

### state / PKCE 검증 설계

Supabase의 `sign_in_with_oauth()`는 우리에게 별도의 `state` 파라미터를 노출하지
않는다(SDK가 Google과의 왕복은 자체적으로 처리한다). 대신 이 프로젝트는:

1. **PKCE code_verifier**: Postgres/Supabase 서버가 `exchange_code_for_session`
   시점에 검증한다(SDK가 자동 관리) — 요청 원칙 1/2/6("PKCE verifier를 검증한다")을
   서버 쪽에서 만족한다.
2. **자체 무작위 토큰 경로**: `OAuthCallbackServer`가 매 로그인 시도마다
   `secrets.token_urlsafe(24)`로 만든 토큰을 콜백 경로에 심고
   (`/oauth/callback/<token>`), `hmac.compare_digest`로 정확히 일치하는 요청만
   처리한다. 이 프로그램이 시작한 로그인 시도가 아닌 요청(추측/재생 시도, 동시에
   실행 중인 다른 프로세스의 우연한 접근)은 전부 404로 무시된다 — 이것이 로컬
   콜백 서버 레벨에서의 "state" 역할을 한다.

두 장치를 합치면 순수 OAuth `state` 파라미터 하나보다 오히려 더 강한 보호가 된다
(공격자는 verifier도, 경로 토큰도 둘 다 몰라야 한다).

---

## 2. Supabase 프로젝트 설정 (대시보드)

**운영 프로젝트에 바로 적용하지 말고, 먼저 테스트/스테이징 프로젝트에서 검증할 것**
(Phase 2B 검증 때와 동일한 원칙).

### 2.1 Google Cloud Console

1. [Google Auth Platform 콘솔](https://console.cloud.google.com/auth/clients)에서
   OAuth 클라이언트 ID 생성 — 애플리케이션 유형: **웹 애플리케이션**.
2. **승인된 리디렉션 URI**에 Supabase의 콜백 URL을 등록한다:
   `https://<프로젝트-ref>.supabase.co/auth/v1/callback`
   (정확한 값은 아래 2.2 Google 공급자 설정 화면에 표시되는 값을 그대로 복사할 것 —
   추측해서 입력하지 말 것).
3. 클라이언트 ID/Secret을 발급받는다. **이 Secret은 Supabase 대시보드에만
   입력한다 — 데스크톱 앱 코드/저장소에는 절대 넣지 않는다** (원칙 20).

### 2.2 Supabase 대시보드

1. **Authentication → Providers → Google**로 이동해 활성화.
2. Google Cloud Console에서 발급받은 Client ID/Secret 입력.
3. **Authentication → URL Configuration**(`.../auth/url-configuration`)로 이동해
   **Redirect URLs**에 데스크톱 콜백을 허용하는 패턴을 추가한다.

### 2.3 Redirect URL 패턴 — 두 가지 옵션

Supabase의 Redirect URL 매칭은 glob 패턴이며, 구분자는 `.`과 `/`만이다(`:`은
구분자가 아니다) — 공식 문서(`supabase.com/docs/guides/auth/redirect-urls`)에서
직접 확인했다. 다만 문서의 예시는 전부 **고정 포트**(`localhost:3000/**`)만
보여주고, "포트 번호 자체에 와일드카드"를 쓰는 예시는 없었다.

- **옵션 A (권장, 별도 설정 없음)**: `http://127.0.0.1:*/oauth/**`를 Redirect
  URLs에 등록한다. 프로그램은 기본적으로 OS가 골라주는 임의 포트를 쓰므로 매번
  주소가 달라지는데, 이 패턴이 이론상(문서의 구분자 규칙에 따르면) 어떤 포트든
  매칭되어야 한다. **다만 이 정확한 형태가 Supabase 공식 예시에 없으므로, 실제
  대시보드에 저장한 뒤 반드시 2번 항목의 수동 테스트로 검증할 것.**
- **옵션 B (와일드카드가 안 될 때의 대안)**: `.env`의
  `SUPABASE_OAUTH_CALLBACK_PORTS`에 고정 포트 몇 개(예: `53682,53683,53684`)를
  지정하면 프로그램이 그중 사용 가능한 포트를 골라 쓴다. 이 경우 Redirect
  URLs에는 그 포트들을 하나씩 정확히 등록한다:
  `http://127.0.0.1:53682/oauth/**`, `http://127.0.0.1:53683/oauth/**`, ...
  경로 뒷부분(`/oauth/**`)은 매 로그인마다 무작위 토큰이 붙으므로 반드시
  와일드카드(`**`)로 등록해야 한다.

### 2.4 최초 관리자 지정

Phase 2B와 동일하게, 최초 로그인한 계정은 `app_users`에 `pending`/`viewer`로
자동 생성된다. 관리자 승인이 되어 있지 않으면 아무도 승인할 수 없는 문제를
피하기 위해, 최초 1명은 SQL로 수동 승인해야 한다
(`docs/sql/phase2b_schema.sql` 8절 참고).

### 2.5 테스트 사용자 / 프로젝트 분리

- Google OAuth 동의 화면이 "테스트" 모드면 등록된 테스트 사용자 계정만 로그인할
  수 있다 — Google Cloud Console의 OAuth 동의 화면 설정에서 테스트 사용자를
  추가해야 한다.
- **운영 Supabase 프로젝트와 테스트 프로젝트를 분리해서 쓸 것.** `.env`의
  `SUPABASE_URL`/`SUPABASE_ANON_KEY`를 바꿔치기하는 것만으로 어느 프로젝트를
  쓸지 전환된다.

---

## 3. 권한별 동작 요약

| app_users 상태/역할 | CloudState | pull(읽기) | push(업로드) |
|---|---|---|---|
| `pending` | `APPROVAL_PENDING`("관리자 승인 대기") | 시도 안 함 | 시도 안 함 |
| `blocked` | `BLOCKED`("접근 차단됨") | 시도 안 함 | 시도 안 함 |
| `approved` + `viewer` | `CONNECTED_READ_ONLY`("읽기 전용") | 가능 | 시도 안 함(로컬 dirty여도) |
| `approved` + `editor`/`admin` | `CONNECTED`("동기화 완료") | 가능 | 가능 |

모든 경우에 **로컬 JSON 저장과 카카오톡 자동발송은 로그인/권한과 무관하게 항상
정상 동작한다** — `notify_local_save()`가 로컬 저장을 먼저, 무조건 수행한 뒤에만
권한을 확인해 클라우드 업로드 여부를 결정한다.

---

## 4. 401/403(권한 거부)과 네트워크 오류의 구분

`CloudSyncService`가 `postgrest.exceptions.APIError`를 잡아 `.code == "42501"`
(Postgres insufficient_privilege, 즉 RLS 위반)이면 `error_code="permission_denied"`로,
그 외(연결 실패 등)는 기존대로 `"connection_error"`로 분류한다.
`CloudSyncCoordinator`는 이 둘을 서로 다른 로그 문구로 남긴다 — 대부분의 경우
`_resolve_profile_or_halt()`가 pull/push 시도 자체를 사전에 막으므로,
`permission_denied`는 "확인 시점과 실제 쓰기 시점 사이에 관리자가 권한을 바꾼"
드문 경쟁 상태에서만 나타난다.

---

## 5. 로그아웃

`AuthService.logout()`은 (1) `client.auth.sign_out()`으로 서버 세션을
best-effort 폐기(실패해도 무시)하고, (2) 로컬 DPAPI 세션 파일을 삭제한다.
`CloudSyncCoordinator.on_logout()`은 상태를 즉시 `LOGIN_REQUIRED`로 바꾼다 —
폴링 스레드 자체는 종료하지 않는다(다음 폴링 틱부터 `is_logged_in()` 검사에서
자동으로 건너뛰어지므로 실질적으로 멈춘 것과 같고, 재로그인 시 스레드를 다시
만들 필요가 없어 더 단순하다).

---

## 6. 테스트 방법

```powershell
python -m unittest discover -s tests -v
```

실제 Google/Supabase에는 연결하지 않는다:

- `tests/test_oauth_callback_server.py` — 실제 127.0.0.1 소켓으로 콜백 서버
  자체를 검증(경로 토큰 불일치 차단, code/error 수신, 타임아웃, 포트 동적 할당).
- `tests/test_auth_service_oauth.py` — `client.auth`를 흉내내는 fake로
  로그인 전체 흐름(URL 생성 → 실제 로컬 서버로 브라우저 리디렉션을 흉내낸
  HTTP 요청 → 세션 교환 → DPAPI 저장), refresh 성공/실패, 로그 노출 여부를 검증.
- `tests/test_cloud_sync_coordinator.py`의 `TestAppUserGating` — pending/
  blocked/viewer/editor/admin별 동작, 로그인 성공 시 재동기화, 로그아웃 시
  상태 전환을 검증.
- `tests/test_auth_service.py` — 세션 로드/만료/DPAPI 저장/로그아웃(Phase 2C부터).

### UI 응답성(메인루프가 멈추지 않는지) 수동 검증

`login_with_google()`을 1초 지연시키는 fake로 바꿔치기하고 실제 CustomTkinter
`mainloop()`를 구동하며 20ms 간격 heartbeat가 로그인 대기 중에도 계속 fire하는지
확인했다 — 1.3초 동안 43회 heartbeat 발생(정상), 팝업/버튼 상태 복원도 정상
확인. 자동화된 unittest로 만들기보다 1회성 수동 스크립트로 확인했다(GUI
이벤트루프 타이밍 테스트는 unittest로 안정적으로 재현하기 어려움).

---

## 7. 실제 Supabase 테스트 프로젝트에서의 수동 확인 순서

1. `.env`에 테스트 프로젝트의 `SUPABASE_ENABLED=true`, `SUPABASE_URL`,
   `SUPABASE_ANON_KEY` 설정 후 `python main.py` 실행.
2. 상단 "Google로 로그인" 클릭 → 브라우저에서 Google 계정으로 로그인.
3. 로그인 완료 페이지("로그인이 완료되었습니다...")가 뜨고 프로그램으로
   돌아오면, Supabase 대시보드 **Authentication → Users**에 방금 로그인한
   계정이 생성되었는지 확인.
4. **Table Editor → app_users**에 같은 계정이 `status=pending`으로 자동
   생성되었는지 확인 (프로그램 상태 라벨도 "관리자 승인 대기"로 표시되어야 함).
5. SQL Editor 또는 관리자 화면에서 해당 행을 `status='approved', role='editor'`로
   변경.
6. 프로그램에서 상태가 "관리자 승인 대기" → (다음 폴링 또는 재로그인 시)
   "동기화 완료"로 바뀌는지 확인.
7. 클라우드에 메시지가 있다면 pull되어 UI에 반영되는지 확인.
8. 메시지 1개를 수정하고 저장 버튼 클릭 → 잠시 후 상태가 "동기화 완료"로
   유지되는지, Table Editor의 `messages`에 반영됐는지 확인.
9. **Table Editor → message_history**에 방금 수정 건이 자동으로 기록됐는지 확인
   (수정자=방금 로그인한 계정, 변경 전/후 값 포함).
10. "로그아웃" 클릭 → 상태가 "로그인 필요"로 바뀌는지 확인.
11. 프로그램을 완전히 종료했다가 다시 실행 → 로그인 없이도 로컬 데이터로 즉시
    열리고(원칙 19), 만약 세션이 아직 유효했다면(로그아웃 안 하고 종료한
    경우) 자동으로 세션이 복원되는지 확인.

---

## 8. 아직 남은 항목 / 알려진 제약

- **와일드카드 Redirect URL(`http://127.0.0.1:*/oauth/**`)이 실제로 동작하는지는
  Supabase 공식 예시에 없어 100% 보증할 수 없다** — 반드시 실 프로젝트에서
  1회 로그인 시도로 검증할 것. 안 되면 `SUPABASE_OAUTH_CALLBACK_PORTS`로 고정
  포트 목록을 쓰는 옵션 B로 전환한다.
- 로그인 성공/실패 여부와 무관하게 `get_app_user_profile()`이 매 폴링/저장마다
  네트워크 호출을 몇 차례(세션 검증 1~2회 + app_users 조회 1회) 하므로, 이론상
  더 캐싱할 여지가 있다 — 30초 폴링 주기에서는 무시할 수준이라 이번 단계에서는
  최적화하지 않았다.
- PC 전용 계정을 고정으로 쓸지, 실행자별 개별 로그인을 쓸지는
  `docs/PHASE2B_ROLES_AUTH_SPEC.md` 5.3절의 미해결 질문 그대로 남아있다 — 이번
  구현은 어느 쪽이든 동일하게 동작한다(로그인한 계정 기준으로 app_users를 조회).
- 세션 갱신 실패 시 사용자에게 "재로그인이 필요합니다" 같은 명시적 안내는 상태
  라벨("로그인 필요")로만 표시되고 별도 알림은 없다 — 요구사항상 반복 팝업을
  피해야 하므로 의도된 설계다.
