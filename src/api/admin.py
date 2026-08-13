"""관리자 설정 (이슈 #42).

조직 관리자만 접근한다. 데모 세션은 admin_user 의존성에서 먼저 막힌다.
질의 이력 조회는 데이터 담당(#30)이 이 라우터에 추가한다.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.auth import Ctx, admin_user, get_db
from src.db.models import Organization, Repo
from src.schemas import PlanOut

router = APIRouter(prefix="/admin", tags=["admin"])

# 요금제 화면과 같은 값을 쓴다 (Starter 5만 / Team 12만 / Business 문의).
# Business는 금액을 정하지 않고 문의로 받으므로 0으로 둔다.
PLAN_PRICE_KRW = {"starter": 50_000, "team": 120_000, "business": 0}


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
