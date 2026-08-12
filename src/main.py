from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api import api_router
from src.config import settings
from src.db.init import create_tables
from src.indexing.runner import reset_stuck_indexing


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 없는 테이블만 생성한다. 컬럼 변경은 반영되지 않으므로
    # 모델을 고쳤으면 `python -m src.db.init --reset`으로 리셋한다.
    create_tables()
    # 재시작으로 끊긴 인덱싱을 정리한다. 두지 않으면 재인덱싱이 영영 막힌다.
    reset_stuck_indexing()
    yield


app = FastAPI(title="Code Trace API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    # 프리뷰 배포는 주소가 매번 바뀌므로 정규식으로 함께 허용한다.
    allow_origin_regex=settings.cors_origin_regex or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 외부 모니터링 도구는 본문 없이 상태만 확인하려고 HEAD를 먼저 보낸다.
# GET만 등록하면 405가 반환되어 장애로 오인된다.
@app.api_route("/health", methods=["GET", "HEAD"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(api_router)
