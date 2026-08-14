"""관리자 설정 (이슈 #42, #30).

조직 관리자만 접근한다. 데모 세션은 admin_user 의존성에서 먼저 막힌다.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.auth import Ctx, admin_user, get_db
from src.db.models import Organization, QueryLog, Repo
from src.db.query import query
from src.db.session import SessionLocal
from src.schemas import PlanOut, QueryLogListOut, QueryLogOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# 요금제 화면과 같은 값을 쓴다 (Starter 5만 / Team 12만 / Business 문의).
# Business는 금액을 정하지 않고 문의로 받으므로 0으로 둔다.
PLAN_PRICE_KRW = {"starter": 50_000, "team": 120_000, "business": 0}

_QUERY_LOG_PER_PAGE = 20
_QUERY_LOG_RETENTION = timedelta(days=90)


def cleanup_old_query_logs() -> int:
    """90일 지난 질의 이력을 지운다 (api-spec §6). 서버가 뜰 때 한 번 돈다.

    스케줄러가 없는 규모라 매 조회마다 지우면 낭비다. reset_stuck_indexing()과
    같은 자리(main.py의 lifespan)에서 재시작마다 한 번 도는 것으로 충분하다.
    """
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - _QUERY_LOG_RETENTION
        deleted = db.execute(delete(QueryLog).where(QueryLog.created_at < cutoff)).rowcount
        if deleted:
            logger.info("90일 지난 질의 이력 %d건을 지웠습니다", deleted)
            db.commit()
        return deleted
    finally:
        db.close()


@router.get("/plan")
def get_plan(ctx: Ctx = Depends(admin_user), db: Session = Depends(get_db)) -> PlanOut:
    """현재 요금제와 레포 사용량."""
    # 가입만 하고 조직을 만들지 않은 사용자는 organization_id가 없다.
    if ctx.organization_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "조직을 찾을 수 없습니다")

    org = db.get(Organization, ctx.organization_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "조직을 찾을 수 없습니다")

    repos_used = db.scalar(
        select(func.count()).select_from(Repo).where(Repo.organization_id == ctx.organization_id)
    )

    return PlanOut(
        plan=org.plan,
        price_krw=PLAN_PRICE_KRW.get(org.plan, 0),
        repo_limit=org.repo_limit,
        repos_used=repos_used or 0,
    )


@router.get("/query-logs")
def list_query_logs(
    page: int = Query(1, ge=1),
    ctx: Ctx = Depends(admin_user),
    db: Session = Depends(get_db),
) -> QueryLogListOut:
    """질의 이력. 맥락·그래프 조회 시 남는다(src/api/context.py의 record_query_log)."""
    if ctx.organization_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "조직을 찾을 수 없습니다")

    stmt = query(QueryLog, ctx.organization_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(
            stmt.order_by(QueryLog.created_at.desc())
            .offset((page - 1) * _QUERY_LOG_PER_PAGE)
            .limit(_QUERY_LOG_PER_PAGE)
        )
    )

    return QueryLogListOut(
        items=[
            QueryLogOut(
                id=row.id, user_name=row.user_name, action=row.action,
                repo=row.repo_name, target=row.target, created_at=row.created_at,
            )
            for row in rows
        ],
        page=page,
        per_page=_QUERY_LOG_PER_PAGE,
        total=total,
    )
