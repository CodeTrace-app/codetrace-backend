"""API 라우터.

모든 엔드포인트는 /api/v1 아래에 붙는다 (docs/api-spec.md §0).
"""

from fastapi import APIRouter

from src.api import (
    admin,
    auth,
    context,
    explorer,
    inquiries,
    integrations,
    organizations,
    pr_warnings,
    repos,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(organizations.router)
api_router.include_router(integrations.router)
api_router.include_router(repos.router)
api_router.include_router(context.router)
api_router.include_router(explorer.router)
api_router.include_router(pr_warnings.router)
api_router.include_router(inquiries.router)
api_router.include_router(admin.router)
