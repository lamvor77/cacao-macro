# 운영 전환 실행 — cacao-macro-test → cacao-macro (프로젝트 승격 방식)

**결정**: 기존 운영 프로젝트(`nojdwuoronqmvpdptvlr`)는 삭제하고, 테스트
프로젝트(`kdyxxkltafeuucijiyzp`, 표시 이름을 `cacao-macro`로 변경)를 그대로
운영으로 쓴다. 스키마/RPC/RLS/Realtime은 이미 검증된 상태이므로 손대지
않는다.

---

## 이번에 실제로 처리한 것 / 처리하지 못한 것

**코드/로컬 설정 — 완료**

| 파일 | 변경 내용 |
|---|---|
| `.env`(루트) | `APP_ENV`/`SUPABASE_ENVIRONMENT` → `production` 추가, `SUPABASE_URL`/`SUPABASE_ANON_KEY` → `kdyxxkltafeuucijiyzp` 값으로 교체, `LICENSE_SECRET_KEY`는 그대로 |
| `dist/.env` | 동일하게 교체(참고: 이 파일은 git 추적 대상이 아닌 로컬 전용 파일) |
| `scripts/check_rls_rpc_permissions.py` | `_PRODUCTION_HOST_DENYLIST`를 `nojdwuoronqmvpdptvlr` → `kdyxxkltafeuucijiyzp`로 교체(단순 삭제 아님 — 아래 참고) |
| `tests/test_check_rls_rpc_permissions.py` | 위 변경에 맞춰 테스트 값 수정, 재실행 통과 확인(9/9) |
| `docs/test_environment_execution_guide.md` | 옛 프로젝트 참조 수정 + 상단에 "재검토 필요" 경고 추가 |
| `docs/production_cutover_plan.md`, `production_project_readiness_check.md`, `production_upgrade_plan.md` | 전부 "폐기됨" 표시 + 이 문서로 리다이렉트 |

**⚠️ 안전장치 관련 판단**: `_PRODUCTION_HOST_DENYLIST`는 단순 삭제가 아니라
새 운영 프로젝트 ref로 교체했다 — 이 목록을 그냥 비우면 "쓰기 테스트
스크립트가 운영에 실수로 실행되는 것을 막는" 안전장치 자체가 사라지기
때문이다. 문자 그대로 "제거"만 하면 이 프로젝트 전체가 지켜온 "운영에는
쓰기 테스트를 하지 않는다" 원칙이 깨진다고 판단해 교체 쪽을 선택했다.

**Supabase/Vercel 프로젝트 자체 — 제가 할 수 없음(API/대시보드 접근 권한 없음)**

아래는 사용자가 직접 처리해야 한다:

1. Supabase 대시보드에서 `nojdwuoronqmvpdptvlr` 프로젝트 삭제(`Project Settings → General` 맨 아래 "Delete Project", 프로젝트명 재입력 확인 필요)
2. `kdyxxkltafeuucijiyzp` 프로젝트의 표시 이름을 `Project Settings → General`에서 `cacao-macro`로 변경
3. Vercel 대시보드 → 프로젝트 → `Settings → Environment Variables`에서 **Production** 환경의 `VITE_SUPABASE_URL`/`VITE_SUPABASE_ANON_KEY`를 `kdyxxkltafeuucijiyzp` 값으로 변경(Preview/Development는 건드리지 않음 — 이미 같은 프로젝트를 보고 있었다면 변경 불필요)
4. 배포 대상 각 PC의 실제 `.env`(이 저장소 밖, 각 PC에 개별 설치된 파일)도 동일하게 갱신

---

## ⚠️ 짚고 넘어가야 할 점 — test-runtime을 지금부터 쓰면 안 됨

`test-runtime/.env`는 여전히 `kdyxxkltafeuucijiyzp`를 가리키고 **`APP_ENV=test`
배지도 그대로 뜬다.** 하지만 이 프로젝트가 방금 운영이 됐으므로, **지금부터
`test-runtime`으로 실행하는 모든 것은 실제 운영 데이터를 대상으로 하는
것**이다("테스트"라는 이름과 배지만 남아있을 뿐 실제로는 운영). 이 저장소가
지금까지 지켜온 "운영에는 쓰기 테스트를 하지 않는다" 원칙과 정면으로
충돌하므로:

- 앞으로 `scripts/check_rls_rpc_permissions.py` 같은 **쓰기 테스트 스크립트를
  `test-runtime`/`kdyxxkltafeuucijiyzp` 대상으로 실행하지 말 것**(위 denylist
  교체로 이제 이 스크립트 자체도 이 프로젝트를 감지하면 자동 중단한다)
- 앞으로 정말 격리된 테스트가 필요하면 **완전히 새로운 Supabase 프로젝트**를
  만들고 `test-runtime/.env`를 그 프로젝트로 옮길 것 — 지금 당장은 건드리지
  않았다(사용자 지시 범위 밖)

---

## 7. 테스트 계정·테스트 데이터 정리 (스키마/RPC/RLS/Realtime은 건드리지 않음)

**먼저 읽기 전용으로 현재 사용자 목록을 확인**한다(SQL Editor):

```sql
select u.id, u.email, u.status, u.role, u.created_at
from public.app_users u
order by u.created_at;
```

이 목록에서 실제 운영 인원이 아닌 계정(예: `test-%@example.com` 패턴,
또는 `employee_a`/`employee_b`/`admin_a`/`disabled_user` 별칭으로 만들었던
테스트 계정)을 사람이 직접 식별한다 — 이메일 패턴은 프로젝트마다 다를 수
있어 자동으로 지울 수 없다.

식별한 뒤, 대상 계정을 `auth.users`에서 삭제한다(`app_users`는
`ON DELETE CASCADE`로 자동 삭제됨):

```sql
-- 식별한 테스트 계정 ID/이메일로 직접 바꿔서 실행 — 한 번에 여러 명이면
-- IN (...)에 나열. 반드시 위 조회 결과로 실제 테스트 계정임을 확인한 뒤 실행.
delete from auth.users where email in (
  '실제로_확인한_테스트_이메일_1',
  '실제로_확인한_테스트_이메일_2'
);
```

`phase2b_rls_tests.sql`을 이 프로젝트에서 실행한 적이 있다면
`docs/sql/phase2b_rls_tests_cleanup.sql`을 그대로 실행해도 된다(안전장치로
`test-%@example.com` 패턴이 아닌 행이 하나라도 있으면 자동 중단됨).

테스트 중 `shared_messages`에 실제로 저장해 본 내용이 있고 운영 시작 전
비워두고 싶다면(선택 — 굳이 지우지 않고 그대로 첫 운영 메시지로 써도 무방):

```sql
-- 확인 먼저(어떤 메시지가 시드가 아닌 실제 값으로 바뀌었는지)
select message_no, content, revision, update_source from public.shared_messages order by message_no;
```

내용을 비우고 싶은 번호가 있다면 앱의 정상 저장 경로(관리자 계정으로 PC/
모바일에서 직접 편집·저장, 또는 `force_update_shared_message` RPC)로
처리한다 — 테이블을 직접 `UPDATE`하지 않는다(감사 이력 일관성 유지).

**절대 실행하지 않을 것**: `DROP TABLE`/`DROP FUNCTION`/`DROP POLICY`/
`alter publication ... drop table` 등 스키마·RPC·RLS·Realtime을 변경하는
어떤 SQL도 이번 정리 범위가 아니다.

---

## 8. 최종 확인 체크리스트

- [ ] PC 로그인 — Google 로그인 성공
- [ ] 관리자 승인 — 관리자 계정이 `app_users`에서 `status='approved', role='admin'`으로 확인되고, 다른 사용자를 승인할 수 있는지
- [ ] 메시지 저장 및 조회 — PC에서 메시지 저장 → 새로고침 후 정상 조회
- [ ] 모바일 조회 — Vercel Production 배포본에서 로그인 후 메시지 목록 정상 표시
- [ ] PC·모바일 Realtime 동기화 — 한쪽에서 저장 시 다른 쪽 화면에 실시간 반영
- [ ] 메시지 수정 및 발송 — 수정 후 저장, 실제 발송 흐름까지 정상 동작
