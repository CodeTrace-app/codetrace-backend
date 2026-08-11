from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings

app = FastAPI(title="Code Trace API")

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
