# Code Trace API 명세

> **이 문서가 프론트·백엔드 간 유일한 계약이다** (CLAUDE.md §2).
> 명세를 바꾸려면 코드보다 이 문서를 먼저 수정하고 프론트 담당에게 알린다.
> 각 엔드포인트의 예시 JSON은 프론트가 목데이터로 그대로 복사해 사용한다.

---

## 0. 공통 규칙

- 기본 URL: `{VITE_API_URL}/api/v1` (아래 경로는 전부 이 뒤에 붙는다)
- 인증: `Authorization: Bearer <access_token>` 헤더. 🔓 표시된 것만 인증 불필요.
- 날짜: ISO 8601 UTC (`2026-08-10T12:00:00Z`)
- 에러 형식 (FastAPI 기본):
  ```json
  { "detail": "에러 메시지" }
  ```
  - `401` 토큰 없음·만료 → 프론트는 로그인 화면으로
  - `403` 권한 없음 (데모 세션 차단, 관리자 전용, 플랜 한도)
  - `404` 리소스 없음
- **데모 세션**: `POST /auth/demo`로 발급받는 읽기 전용 토큰. 데모 조직의 organization_id로
  일반 세션과 동일한 격리 규칙을 통과한다. 🚫데모 표시가 있는 엔드포인트는 데모 세션에서 `403`.
- 역할: `admin` | `member` 2단계 (조직 생성자가 admin).

### `GET /health` 🔓 (v1 밖, 루트 경로)

배포 확인용. **응답 200** `{ "status": "ok" }`

---

## 1. 인증·조직

### `POST /auth/signup` 🔓

회원가입. 성공 시 바로 로그인 상태가 되며, `organization`이 `null`이면
프론트는 조직 생성 화면으로 보낸다.

**요청**
```json
{ "email": "kim@acme.dev", "password": "hunter22", "name": "김팀장" }
```

**응답 201**
```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "user": { "id": 1, "email": "kim@acme.dev", "name": "김팀장", "role": "admin" },
  "organization": null
}
```

- `409` 이미 가입된 이메일

### `POST /auth/login` 🔓

**요청**
```json
{ "email": "kim@acme.dev", "password": "hunter22" }
```

**응답 200**
```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "user": { "id": 1, "email": "kim@acme.dev", "name": "김팀장", "role": "admin" },
  "organization": { "id": 1, "name": "에이크미", "slug": "acme-x1y2", "plan": "starter" }
}
```

- `401` 이메일 또는 비밀번호 불일치

### `POST /auth/demo` 🔓

랜딩의 "데모 체험" CTA. 데모 조직의 읽기 전용 세션을 발급한다.

**요청** 없음 · **응답 200**
```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "read_only": true,
  "user": { "id": 0, "email": "demo@codetrace.app", "name": "데모 사용자", "role": "member" },
  "organization": { "id": 99, "name": "Acme Corp (데모)", "slug": "demo", "plan": "team" }
}
```

### `GET /auth/me`

새로고침 시 세션 복원. **응답 200** — 로그인 응답과 동일 형태 (`access_token` 제외).

### `POST /organizations` 🚫데모

가입 직후 조직 생성. 조직 식별자(slug)가 발급된다. 사용자당 1개 (다중 소속 범위 외).

**요청**
```json
{ "name": "에이크미" }
```

**응답 201**
```json
{
  "organization": { "id": 1, "name": "에이크미", "slug": "acme-x1y2", "plan": "starter" },
  "access_token": "eyJhbGciOi..."
}
```

- `409` 이미 조직이 있는 사용자
- **새 토큰을 함께 돌려준다.** 가입 시점에 받은 토큰에는 조직이 없어서
  그대로 두면 이후 API 호출이 빈 결과를 받는다. 프론트는 이 토큰으로 교체한다.

---

## 2. 연동 설정 (GitHub App)

### `GET /integrations`

연동 설정 화면 카드 목록. GitHub 외 3개는 항상 `coming_soon` (클릭 차단).

**응답 200**
```json
{
  "github": { "status": "connected", "installation_id": 12345678, "account": "acme-payments" },
  "gitlab": { "status": "coming_soon" },
  "jira":   { "status": "coming_soon" },
  "slack":  { "status": "coming_soon" }
}
```

- `github.status`: `"not_connected"` | `"connected"`

### `GET /integrations/github/install-url` 🚫데모

GitHub App 설치 페이지 URL. 프론트는 이 URL로 새 창을 연다.
설치 완료 후 GitHub이 백엔드 콜백으로 리다이렉트하며, 백엔드가 installation_id를
저장하고 연동 설정 화면으로 돌려보낸다.

**응답 200**
```json
{ "url": "https://github.com/apps/codetrace-app/installations/new?state=eyJhbGciOi..." }
```

### `GET /integrations/github/callback` 🔓(state로 검증, 프론트가 호출하지 않음)

GitHub App 설치 완료 후 **GitHub이 사용자 브라우저를 직접 리다이렉트**하는 콜백. 프론트가
fetch로 호출하는 API가 아니므로 Authorization 헤더가 없다 — `install-url` 발급 시 서버가
조직 식별자를 서명해 넣은 `state`를 그대로 돌려받아 요청자를 식별한다.

**쿼리 파라미터** (GitHub이 채워서 리다이렉트)
```
?installation_id=12345678&setup_action=install&state=<install-url 발급 시 받은 값 그대로>
```

- `setup_action`: `"install"` | `"update"` → installation_id·계정명을 저장.
  `"request"`(조직 승인 대기) → 저장하지 않고 그대로 통과.
- 처리 후 **302/307 리다이렉트**: `{FRONTEND_URL}/settings/integrations`
- `400` state 위조·만료 · `404` state의 조직이 존재하지 않음

### `GET /integrations/github/repos` 🚫데모

설치된 GitHub App이 접근 가능한 레포 목록. 인덱싱 대상 선택 UI(S-FWXUHO)에 사용.

**응답 200**
```json
{
  "repos": [
    { "github_full_name": "acme-payments/acme-payment-service", "private": true,  "already_added": true },
    { "github_full_name": "acme-payments/acme-admin-web",       "private": true,  "already_added": false }
  ]
}
```

- `409` GitHub App 미설치 상태

---

## 3. 레포·인덱싱

### `GET /repos`

대시보드 레포 카드 목록. **인덱싱 중(`collecting`/`parsing`)인 레포가 있을 때만
프론트가 5초 간격 폴링**으로 갱신하고, 전부 `done`/`failed`면 폴링을 중단한다.

**응답 200**
```json
{
  "summary": {
    "github_account": "acme-payments",
    "github_connected": true,
    "repo_count": 4,
    "commit_count": 1284,
    "review_comment_count": 342,
    "last_indexed_at": "2026-08-10T09:30:00Z"
  },
  "repos": [
    {
      "id": 1,
      "name": "acme-payment-service",
      "github_full_name": "acme-payments/acme-payment-service",
      "default_branch": "main",
      "language": "Python",
      "indexing_status": "parsing",
      "progress": { "current": 142, "total": 218, "label": "파일 파싱" },
      "last_indexed_at": null,
      "stats": { "files": 87, "functions": 342, "commits": 418, "prs": 96 }
    },
    {
      "id": 2,
      "name": "acme-admin-web",
      "github_full_name": "acme-payments/acme-admin-web",
      "default_branch": "main",
      "language": "TypeScript",
      "indexing_status": "done",
      "progress": null,
      "last_indexed_at": "2026-08-10T09:30:00Z",
      "stats": { "files": 45, "functions": 120, "commits": 210, "prs": 41 }
    }
  ]
}
```

- `summary`는 대시보드 상단 카드 3개(연동 계정 / 인덱싱된 레포 / 수집된 커밋)에 쓴다.
- `indexing_status`: `"collecting"`(수집 중) → `"parsing"`(파싱 중) → `"done"`(완료) | `"failed"`
- `progress`: 진행 중일 때만 값이 있고 `done`·`failed`면 `null`.
  `label`은 지금 무엇을 세고 있는지를 알려준다 — 커밋 수집 · PR 수집 · 파일 파싱 · 근거 연결 · 배경 요약.
  **한 상태 안에 단계가 여럿이다.** `collecting`에 커밋·PR 수집이, `parsing`에 파일 파싱·근거 연결·배경
  요약이 들어 있고, 단계가 바뀔 때마다 `current`가 0으로 돌아간다. 화면이 `label` 없이 퍼센트만 보여주면
  100%에서 0%로 되돌아가는 것처럼 보이므로 반드시 함께 표기한다.
  퍼센트는 프론트가 `current/total`로 계산한다. 표기 예: "파일 파싱 · 65%", "근거 연결 · 12 / 40"
  **단위를 지어내 붙이지 않는다.** 세는 대상이 단계마다 달라 "커밋 23 / 198"처럼 쓰면 틀린 값이 된다.
- `language`: 레포의 대표 언어. 표시용 문자열이며 지원 언어 판별과는 무관하다.
- `failed`면 카드에 "재인덱싱" 버튼만 노출 (에러 화면을 띄우지 않는다)
- `stats`는 `done` 이전엔 수집된 만큼만 (0일 수 있음)
- "최근 인덱싱 결과" 영역은 별도 API가 아니라 이 목록을 `last_indexed_at` 내림차순으로 추려서 쓴다.

### `POST /repos` 🚫데모

인덱싱 대상 레포 추가. **선택 즉시 인덱싱 시작** (S-FWXUHO).

**요청**
```json
{ "github_full_name": "acme-payments/acme-payment-service" }
```

**응답 201** — `GET /repos`의 카드 1개와 동일 형태 (`indexing_status: "collecting"`)

- `403` 플랜 레포 한도 초과 (`detail`에 "Starter 플랜은 3개까지..." 안내 문구)
- `409` 이미 추가된 레포
- `409` GitHub App 미설치 (연동 없이는 인덱싱할 수 없다)
- **추가 즉시 백그라운드 인덱싱이 시작된다.** 응답은 기다리지 않고 바로 돌아오며,
  진행 상태는 `GET /repos`의 `indexing_status`·`progress`로 확인한다.

### `POST /repos/{repo_id}/reindex` 🚫데모

수동 재인덱싱 (증분 아님, 전체 다시).

**응답 202**
```json
{ "id": 1, "indexing_status": "collecting" }
```

- `409` 이미 인덱싱 진행 중
- `409` GitHub App 미설치

---

## 4. 코드 탐색기

### `GET /repos/{repo_id}/tree`

좌측 파일트리. 인덱싱된 default_branch 기준.

**응답 200**
```json
{
  "root": [
    {
      "path": "src", "name": "src", "type": "dir",
      "children": [
        { "path": "src/payment.py",  "name": "payment.py",  "type": "file", "language": "python" },
        { "path": "src/constants.py","name": "constants.py","type": "file", "language": "python" }
      ]
    },
    { "path": "README.md", "name": "README.md", "type": "file", "language": null }
  ]
}
```

- `language`: `"python"` | `"typescript"` | `"javascript"` | `null`(미지원 — 뷰어에서 하이라이트 없이 표시)
- `409` 인덱싱 미완료 (`indexing_status`가 `done`이 아님)

### `GET /repos/{repo_id}/file?path=src/payment.py`

중앙 코드뷰어. 읽기 전용.

**응답 200**
```json
{
  "path": "src/payment.py",
  "language": "python",
  "content": "import httpx\n\nTIMEOUT_SECONDS = 10\n\ndef process_payment(order_id, amount, retry=3):\n    ...\n",
  "truncated": false,
  "functions": [
    { "name": "process_payment", "start_line": 5,  "end_line": 42, "kind": "function" },
    { "name": "refund_payment",  "start_line": 45, "end_line": 60, "kind": "function" },
    { "name": "Refund",          "start_line": 63, "end_line": 70, "kind": "class"    }
  ]
}
```

- `truncated: true`면 대용량 파일이 잘린 것 — 뷰어 하단에 "일부만 표시됨" 안내
- `functions`는 파서가 추출한 심볼 범위 — 클릭 시 맥락·영향 범위 해석에 사용
- `kind`는 `"function"` \| `"class"` (이슈 #25). 클래스만 있고 함수가 없는 파일에서
  목록이 비지 않도록 클래스도 담는다. 필드가 늘었을 뿐 기존 항목의 모양은 그대로다 —
  프론트가 `kind`를 무시해도 동작한다. 목록 라벨은 "함수"보다 "심볼"이 정확하다
- 상수는 담지 않는다. 클릭해서 볼 본문이 없다

### `GET /repos/{repo_id}/context?path=src/payment.py&line=12`

우측 탭1 "맥락". `line`이 걸친 **가장 가까운(가장 안쪽) 함수**로 해석한다 (S-ZEZFED).
함수 밖(모듈 레벨)이면 파일 단위 맥락으로 응답.

**응답 200 — 근거 있음 (`status: "ok"`)**
```json
{
  "function": { "name": "process_payment", "path": "src/payment.py", "start_line": 5, "end_line": 42 },
  "status": "ok",
  "summary": "2024년 11월 PG사 타임아웃 장애(#PR 41) 이후 재시도 3회와 멱등키 검증이 추가된 함수. 2025년 6월 타임아웃이 3초에서 10초로 조정되었고, 현재는 모든 결제 요청이 이 함수를 단일 경로로 통과한다.",
  "evidence": [
    {
      "kind": "commit",
      "sha": "a1b2c3d",
      "title": "fix: 결제 타임아웃 3s→10s 상향",
      "author": "kimdev",
      "date": "2025-06-14T02:11:00Z",
      "url": "https://github.com/acme-payments/acme-payment-service/commit/a1b2c3d"
    },
    {
      "kind": "pr",
      "number": 41,
      "title": "결제 재시도 로직 추가",
      "date": "2024-11-02T08:00:00Z",
      "url": "https://github.com/acme-payments/acme-payment-service/pull/41",
      "review_excerpt": "재시도만 붙이면 중복 결제 위험이 있어요. 멱등키 검증이 먼저 필요합니다."
    }
  ],
  "evidence_truncated": false,
  "parent_module": null
}
```

**응답 200 — 근거 없음 (`status: "no_history"`, S-UBXNLW)**
```json
{
  "function": { "name": "format_krw", "path": "src/utils.py", "start_line": 3, "end_line": 6 },
  "status": "no_history",
  "summary": null,
  "evidence": [],
  "evidence_truncated": false,
  "parent_module": { "path": "src", "name": "src" }
}
```

- 프론트: "변경 이력 없음" 명시 + `parent_module`로 이동 경로 제공. 빈 화면 금지.
- **`no_history`여도 `evidence`가 비어 있지 않을 수 있다.** 포맷팅·주석 같은 무의미한 커밋만 있는
  함수가 여기 해당한다. 근거는 화면에 보여주되 요약은 만들지 않는다. 빈 배열만 가정하지 말 것.

**응답 200 — 근거 상충 (`status: "conflicting"`)**
```json
{
  "function": { "name": "verify_token", "path": "src/auth.py", "start_line": 18, "end_line": 47 },
  "status": "conflicting",
  "summary": "타임아웃을 두고 상반된 결정이 있다. PG사 권장값에 맞춰 10초로 올린 변경과, 커넥션 풀이 고갈된다는 이유로 3초로 되돌린 변경이 함께 남아 있다. 어느 쪽이 현재 기준인지는 수집된 이력만으로 확정할 수 없다.",
  "evidence": [
    { "kind": "commit", "sha": "a1b2c3d", "title": "fix: 타임아웃 3s→10s 상향", "author": "kimdev", "date": "2025-06-14T02:11:00Z", "url": "https://github.com/acme-payments/acme-payment-service/commit/a1b2c3d" },
    { "kind": "commit", "sha": "e4f5g6h", "title": "fix: 타임아웃 10s→3s 원복", "author": "leedev", "date": "2025-08-02T05:00:00Z", "url": "https://github.com/acme-payments/acme-payment-service/commit/e4f5g6h" }
  ],
  "evidence_truncated": false,
  "parent_module": null
}
```

**`status` 값**
- `"ok"` 정상
- `"no_history"` 근거 부족 — 추측 서술 금지, summary는 null
- `"conflicting"` 근거 상충 — summary가 양쪽을 나란히 서술하고 evidence에 양쪽 모두 포함
- 근거 과다 시 서버가 최근 근거 **12건**까지만 보내고 `evidence_truncated: true`.
  `conflicting`이면 상충하는 양쪽 근거는 자르기에서 제외해 항상 포함한다.

**`conflicting` 판정 기준** (오탐을 막기 위해 좁게 잡는다. 기본값은 `ok`)
- 상충으로 본다: 두 근거가 **같은 결정에 대해 서로를 부정하는 이유**를 대고 있고, 그 뒤에 어느 쪽으로
  정리됐는지 알려주는 근거가 없을 때.
- 상충이 아니다: 값이 A→B로 바뀐 단순한 시간순 변경 / 도입과 제거가 같은 이슈·PR을 공유하는 계획된 전환 /
  리뷰에서 제기됐다가 답변으로 해소된 우려 / 포맷팅·주석·의존성 변경 / 서로 다른 층위를 보완하는 변경.

**요약 생성 실패 시**
- LLM 호출이 실패하면 `summary`는 `null`로 내리고 `status`는 근거 유무에 따른 값을 그대로 유지한다.
  근거가 있는 함수를 `no_history`로 바꾸지 않는다. 실패한 요약은 저장하지 않으므로 다음 조회에서 다시 시도된다.
- 요약은 인덱싱 중에 미리 생성해 저장한다. 이 API는 저장된 요약을 읽기만 하므로 LLM 지연에 묶이지 않는다.

### `GET /repos/{repo_id}/graph?path=src/payment.py&function=process_payment`

우측 탭2 "영향 범위 그래프". **깊이 2 고정** (S-QGBSHN). 파서 결과만 사용 — 추측 없음.

**응답 200**
```json
{
  "root": { "id": "src/payment.py::process_payment", "name": "process_payment", "path": "src/payment.py", "kind": "function" },
  "nodes": [
    { "id": "src/api/checkout.py::checkout",        "name": "checkout",        "path": "src/api/checkout.py", "kind": "function", "depth": 1, "direction": "caller", "reference_count": 12 },
    { "id": "src/payment.py::TIMEOUT_SECONDS",      "name": "TIMEOUT_SECONDS", "path": "src/payment.py",      "kind": "constant", "depth": 1, "direction": "callee", "reference_count": 5 },
    { "id": "src/pg/client.py::PgClient.request",   "name": "PgClient.request","path": "src/pg/client.py",    "kind": "function", "depth": 1, "direction": "callee", "reference_count": 3 },
    { "id": "src/api/subscribe.py::renew",          "name": "renew",           "path": "src/api/subscribe.py","kind": "function", "depth": 2, "direction": "caller", "reference_count": 1 }
  ],
  "edges": [
    { "source": "src/api/checkout.py::checkout",  "target": "src/payment.py::process_payment",  "type": "call" },
    { "source": "src/payment.py::process_payment","target": "src/payment.py::TIMEOUT_SECONDS",  "type": "constant" },
    { "source": "src/payment.py::process_payment","target": "src/pg/client.py::PgClient.request","type": "call" },
    { "source": "src/api/subscribe.py::renew",    "target": "src/api/checkout.py::checkout",    "type": "call" }
  ],
  "total_nodes": 4,
  "truncated": false
}
```

- 노드 `id` 규칙: `"파일경로::함수명"` (메서드는 `클래스.메서드`, 상수는 상수명)
- `kind`: `"function"` | `"constant"` | `"class"`
- `edge.type` (연결 유형 4가지): `"call"` | `"import"` | `"constant"` | `"inheritance"`
  프론트는 이 값을 **색 + 텍스트 두 가지로 함께 표기**한다 (색맹·흑백 대응).
- `direction`: root 기준 `"caller"`(이 함수를 참조) | `"callee"`(이 함수가 참조)
- `reference_count`: 그 심볼이 레포 전체에서 참조되는 횟수.
  **15개 초과 시 이 값 내림차순으로 정렬한 뒤 접는다**(S-TQFUEH). 노드에도 이 숫자를 표기한다.
- 서버는 깊이 2까지 전부 반환 (상한 100노드, 초과 시 `truncated: true` — 그래프 하단에 "일부만 표시됨" 안내). **15개 초과 접기는 프론트 처리** — 15개까지 표시하고 나머지는 "더 보기 · N곳 접힘".
- 렌더링은 **위→아래 세로 계층 카드 리스트**(CSS). 그래프 라이브러리를 쓰지 않는다.
- 노드 클릭 → 코드뷰어 해당 위치 이동 + 맥락 탭 동기화 (프론트 동작, `path`·`id`로 충분)

---

## 5. PR 경고 이력

웹훅이 PR을 검사한 결과는 DB에 저장되고, 이 API로 조회한다 (화면 9번, P2).

### `GET /pr-warnings?repo_id=1&page=1`

`repo_id`는 선택. 없으면 조직의 전체 레포.

**응답 200**
```json
{
  "items": [
    {
      "id": 8,
      "repo": "acme-payment-service",
      "pr_number": 132,
      "pr_title": "결제 타임아웃 설정 변경",
      "pr_url": "https://github.com/acme-payments/acme-payment-service/pull/132",
      "author": "kimnewbie",
      "created_at": "2026-08-10T04:12:00Z",
      "warnings": [
        {
          "change_type": "signature_changed",
          "symbol": "src/payment.py::process_payment",
          "detail": "파라미터가 (order_id, amount, retry)에서 (order_id, amount)로 바뀌었습니다",
          "impacted": [
            { "symbol": "src/api/checkout.py::checkout", "path": "src/api/checkout.py", "line": 27, "type": "call" },
            { "symbol": "src/api/subscribe.py::renew",   "path": "src/api/subscribe.py", "line": 55, "type": "call" }
          ]
        }
      ]
    }
  ],
  "page": 1,
  "per_page": 20,
  "total": 8
}
```

- `change_type`: `"signature_changed"` | `"deleted"` | `"renamed"` | `"constant_changed"`
  (경고 대상은 이 4가지뿐. 내부 로직·주석·타입 힌트·기본값 변경은 경고하지 않는다)
- `impacted[].type`: 연결 유형 4가지와 동일
- **목록 화면이다. 그래프는 코드 탐색기에만 둔다.**
- 각 항목에서 탐색기로 이동하는 링크는 아래 §8의 쿼리 파라미터 규격을 쓴다.
- **이 목록에는 현재 경고가 있는 PR만 남는다.** 처음부터 경고가 없던 PR은 기록하지 않고,
  경고가 있다가 재검사(`synchronize`)로 전부 해소된 PR은 이력에서 지운다 — 경고 0건짜리
  항목이 쌓이면 정작 봐야 할 경고를 찾기 어려워진다. 해소 사실은 PR 코멘트로 알린다(§8).
- `impacted[].symbol`이 `<파일경로>::<module>`로 끝나면(`impacted[].type`이 `"import"`일 때만
  나온다) 그 파일을 import했다는 뜻이지 그 이름의 함수가 아니다. 화면에는 `<module>`을 그대로
  보이지 말고 "`<파일경로>` 파일이 import (`<line>`행)"처럼 파일 단위 문장으로 바꿔서 표시하고,
  함수가 아니므로 탐색기 함수 링크(§8)도 걸지 않는다. 저장 형식 자체는 바꾸지 않는다 —
  식별자 규칙을 바꾸면 파서·그래프에도 영향이 가므로 표시할 때만 바꾼다 (이슈 #61, PR 코멘트도
  같은 규칙).

---

## 6. 관리자 설정

### `GET /admin/query-logs?page=1` 🚫데모 · admin 전용

질의 이력 테이블. 90일 보존 후 자동 삭제.

**응답 200**
```json
{
  "items": [
    {
      "id": 132,
      "user_name": "김신입",
      "action": "context_view",
      "repo": "acme-payment-service",
      "target": "src/payment.py::process_payment",
      "created_at": "2026-08-10T05:21:00Z"
    },
    {
      "id": 131,
      "user_name": "김신입",
      "action": "graph_view",
      "repo": "acme-payment-service",
      "target": "src/payment.py::process_payment",
      "created_at": "2026-08-10T05:20:12Z"
    }
  ],
  "page": 1,
  "per_page": 20,
  "total": 132
}
```

- `action`: `"context_view"` | `"graph_view"`
- `403` member가 호출한 경우

### `GET /admin/plan` 🚫데모 · admin 전용

**응답 200**
```json
{ "plan": "starter", "price_krw": 50000, "repo_limit": 3, "repos_used": 1 }
```

---

## 7. 구독 문의

### `POST /inquiries` 🔓

요금제 화면의 신청 = 문의 접수 (결제 연동 없음).

**요청**
```json
{ "organization_name": "에이크미", "contact_name": "김팀장", "contact": "010-1234-5678", "plan": "team" }
```

**응답 201**
```json
{ "id": 7, "message": "문의가 접수되었습니다. 1영업일 내 연락드립니다." }
```

---

## 8. GitHub 웹훅

### `POST /webhooks/github` 🔓(서명으로 검증)

- **웹훅 방식으로 구현한다. GitHub Actions·CI 방식이 아니다.**
- 구독 이벤트: `pull_request` — `opened`, `synchronize`만 처리
- `X-Hub-Signature-256` 서명 검증 필수. 불일치 시 `401`.
- 처리: 변경 파일만 base/head 리비전에서 **즉시 재파싱**해 비교 (저장된 인덱스로 판별 금지)
- 판별 결과는 **DB에 저장**한다 (§5 PR 경고 이력 화면의 원본).
- **PR당 코멘트는 1개만 유지한다.** `synchronize`로 다시 검사할 때 새 코멘트를 달지 말고
  기존 코멘트를 수정한다 (코멘트 ID를 저장해둔다).
- **응답 204** (본문 없음)

### 경고 코멘트의 웹 화면 링크 규격 (합의됨 — 프론트·백엔드 공통)

```
{FRONTEND_URL}/explorer?repo=<repo_id>&fn=<파일경로::함수명>&tab=impact
```

- `fn` 값은 그래프 노드 `id`와 같은 형식(`src/payment.py::process_payment`)이며 URL 인코딩한다.
- `tab`: `impact`(영향 범위) | `context`(맥락). 생략 시 `context`.
- 프론트는 이 파라미터로 진입 시 해당 파일을 열고, 해당 함수를 선택 상태로 두고, 지정된 탭을 연다.

---

## 9. 확정된 판단 (2026-08-10)

- `indexing_status`에 `failed` 포함 — 실패 시 카드가 "파싱 중"에 멈춰 보이는 것 방지.
  카드에는 재인덱싱 버튼만 노출 (에러 화면 금지 규칙 준수).
- 레포 삭제 API 없음 — 데모 세션은 읽기 전용이라 불필요. 팀 내부 실수는 DB에서 직접 정리.
  필요해지면 이 문서에 먼저 추가한 뒤 구현.
- 폴링 5초, 인덱싱 중인 레포가 있을 때만.
- 그래프 서버 상한 100노드 + `truncated` 플래그. 데모 레포는 100노드를 넘지 않게 설계.
- 그래프 노드에 `reference_count` 포함 — 15개 초과 시 정렬·표기 기준.
- PR 경고는 DB에 저장하고 `/pr-warnings`로 조회. 화면은 목록 형태(P2).
- 탐색기 진입 링크 규격 `?repo=&fn=&tab=` 확정 (PR 코멘트·경고 이력 화면 공통).
- 시안 반영: `/repos`에 대시보드 상단 요약(`summary`)과 진행률(`progress`), 대표 언어(`language`) 추가.
- 영향 범위 그래프는 그래프 라이브러리 없이 CSS 카드 리스트로 구현한다 (시안 형태가 세로 카드 목록).
