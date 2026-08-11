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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
