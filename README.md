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
│   ├── main.py          ← FastAPI 앱, CORS
│   ├── config.py        ← 환경설정 (.env 로드)
│   ├── db/              ← DB 세션, organization_id 강제 query 래퍼
│   └── llm/             ← LLM 호출 단일 진입점 (provider 교체 가능)
├── docs/
│   └── api-spec.md      ← ⭐ API 명세 — 프론트·백엔드 계약의 유일한 기준
├── docker-compose.yml   ← 로컬 PostgreSQL
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

# 4. 서버 실행
uvicorn src.main:app --reload
```

- API 문서(자동 생성): http://localhost:8000/docs
- 헬스체크: http://localhost:8000/health

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

| 이름 | 역할 | GitHub |
|:---:|:---:|:---:|
|  | PM · 백엔드 |  |
|  | 분석 (파싱·영향 범위) |  |
|  | 데이터 (수집·LLM·PR봇) |  |

---

<div align="center">

**🦁 멋쟁이사자처럼 대학 14기 해커톤 🦁**

</div>
