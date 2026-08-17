<div align="center">

# Code Trace — Backend

### 신입 개발자를 위한 B2B 코드 온보딩 도구

코드가 **왜 그렇게 작성되었는지**(커밋·PR 이력 인덱싱)와
**수정 시 영향 범위**(구문 트리 파싱)를 보여준다.

[![LikeLion](https://img.shields.io/badge/멋쟁이사자처럼-14기_해커톤-FF7F00?style=flat-square)]()
[![Frontend](https://img.shields.io/badge/Frontend-Repo-green?style=flat-square)](https://github.com/CodeTrace-app/codetrace-frontend)

### 🔗 [서비스 바로가기](https://codetrace-frontend.vercel.app) · [API 문서](https://codetrace-backend-hq4u.onrender.com/docs)

</div>

---

## 🛠️ 기술 스택

![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python_3.12-3776AB?style=flat-square&logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=flat-square&logo=postgresql&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white)
tree-sitter · SQLAlchemy

## 📂 프로젝트 구조

```
📦 backend
├── src/
│   ├── main.py          ← FastAPI 앱, CORS, 헬스체크
│   ├── config.py        ← 환경설정 (.env 로드)
│   ├── auth.py          ← 인증 의존성 (현재 사용자·조직 주입)
│   ├── db/
│   │   ├── models.py    ← DB 모델 전체
│   │   ├── query.py     ← organization_id 강제 조회 래퍼
│   │   ├── session.py   ← 엔진·세션
│   │   └── init.py      ← 테이블 생성·리셋
│   └── llm/             ← LLM 호출 단일 진입점 (provider 교체 가능)
├── tests/               ← 격리 규칙·권한 경계 검증
├── docs/
│   └── api-spec.md      ← ⭐ API 명세 — 프론트·백엔드 계약의 유일한 기준
├── docker-compose.yml   ← 로컬 PostgreSQL
├── Dockerfile           ← 배포 이미지 (Render)
├── render.yaml          ← 배포 정의
├── local/               ← 개인 파일 (커밋 제외). GitHub App 개인키 등
├── requirements.txt
└── .env.example
```

## 🚀 실행 방법

```bash
# 1. PostgreSQL 실행 (Docker Desktop 필요)
docker compose up -d

# 2. 가상환경 + 의존성
py -3.12 -m venv .venv           # macOS/Linux: python3 -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 3. 환경변수
copy .env.example .env           # macOS/Linux: cp .env.example .env

# 4. 서버 실행 (테이블은 시작할 때 자동 생성된다)
uvicorn src.main:app --reload
```

- API 문서(자동 생성): http://localhost:8000/docs
- 헬스체크: http://localhost:8000/health
- 테스트: `pytest`

### DB 스키마를 바꿨을 때

마이그레이션 도구를 쓰지 않는다. 모델을 고쳤으면 **DB를 리셋**한다.

```bash
python -m src.db.init --reset
```

데이터는 사라지지만 인덱싱을 다시 돌리면 복구된다.
**모델을 바꾼 PR에는 그 사실을 본문에 반드시 적는다.** 다른 팀원도 리셋해야 한다.

**배포 DB는 리셋하지 않는다.** 데모 데이터와 인덱싱 결과가 사라지고 다시 채우는 데 몇 분 걸린다.
컬럼을 더하는 변경이면 `ALTER TABLE`로 그 컬럼만 넣는다.

> ⚠️ **컬럼 추가는 배포보다 먼저 한다.**
> 컬럼은 옛 코드가 무시하므로 미리 넣어도 아무 문제가 없다. 반대로 새 코드가 먼저 뜨면
> 시작할 때 `reset_stuck_indexing()`이 없는 컬럼을 읽으려다 실패해 **서버가 기동조차 하지 못한다.**
>
> ```
> psycopg.errors.UndefinedColumn: column repos.progress_label does not exist
> Application startup failed. Exiting.
> ```
>
> 이 순서를 지키지 않아 실제로 한 번 배포가 죽었다. Render가 이전 버전을 내리지 않아
> 서비스는 살아 있었지만, 새 배포는 컬럼을 넣고 다시 올려야 했다.

### 데모 세션 데이터 넣기

랜딩의 "데모 체험"은 데모 조직을 읽기 전용으로 보여준다.
그 조직에 레포가 없으면 빈 대시보드가 뜨므로 미리 넣어둔다.

```bash
python -m src.demo
```

데모 조직·사용자를 만들고, 레포를 등록하고, 인덱싱을 끝까지 돌린다.
여러 번 돌려도 레포가 늘지 않는다 — 다시 인덱싱할 뿐이다.

레포 주소는 `DEMO_REPO`, GitHub App 설치 ID는 `DEMO_INSTALLATION_ID`로 바꿀 수 있다.
설치 ID를 안 주면 같은 계정에 이미 연동된 조직에서 찾아 쓴다.

파서가 바뀌면 저장된 인덱스가 옛 결과이므로 이 명령을 다시 돌린다.

## 🔐 API 만들 때 (백엔드 공통 규칙)

조직 ID를 요청 본문이나 쿼리로 받지 않는다. 항상 인증 의존성에서 가져온다.

```python
from fastapi import Depends
from src.auth import Ctx, current_user, writable_user, admin_user, get_db
from src.db.query import query

@router.get("/repos")                                   # 읽기 (데모 세션도 허용)
def list_repos(ctx: Ctx = Depends(current_user), db=Depends(get_db)):
    return db.scalars(query(Repo, ctx.organization_id)).all()

@router.post("/repos")                                  # 쓰기 (데모 세션 403)
def add_repo(ctx: Ctx = Depends(writable_user), ...): ...

@router.get("/admin/query-logs")                        # 조직 관리자 전용
def logs(ctx: Ctx = Depends(admin_user), ...): ...
```

- 조회는 `query()` 래퍼만 쓴다. raw 쿼리·`select()` 직접 사용 금지.
- 새 모델에는 `organization_id`가 반드시 있어야 한다. 빠지면 테스트가 실패한다.

## 📡 API 명세

**[docs/api-spec.md](docs/api-spec.md)가 유일한 기준이다.**
명세를 바꾸려면 코드보다 문서를 먼저 수정하고 프론트 담당에게 알린다.

## 🌿 브랜치 전략

| 브랜치 | 용도 |
|:---:|:---|
| `main` | 배포용 (심사 URL과 연동) |
| `develop` | 개발 통합 — **PR은 여기로** |
| `feat/기능명` | 기능 개발 (예: `feat/auth`, `feat/graph-api`) |
| `fix/버그명` | 버그 수정 |

```
1. develop에서 브랜치 생성   →  git checkout -b feat/auth develop
2. 작업 후 커밋              →  git commit -m "feat: 로그인 API 구현"
3. develop으로 PR 생성       →  이슈 번호 연결 (close #N)
4. CI 통과 + PM 확인 후 머지
```

⚠️ `develop`·`main` 직접 푸시 금지 — 반드시 PR로.

## 👥 팀원

| 역할 | 담당 | GitHub |
|:---:|:---:|:---:|
| PM · 백엔드 | | [@dlwpgur3554](https://github.com/dlwpgur3554) |
| 분석 (파싱·영향 범위) | | [@juhyeong929](https://github.com/juhyeong929) |
| 데이터 (수집·LLM·PR봇) | | [@kyeongmin0212](https://github.com/kyeongmin0212) |

---

<div align="center">

**🦁 멋쟁이사자처럼 대학 14기 해커톤 🦁**

</div>
