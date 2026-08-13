"""조직 생성."""

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth import Ctx, create_token, get_db, writable_user
from src.db.models import Organization, User
from src.schemas import OrganizationCreatedOut, OrganizationCreateRequest, OrganizationOut

router = APIRouter(prefix="/organizations", tags=["organizations"])


def make_slug(name: str, db: Session) -> str:
    """조직 식별자를 만든다.

    조직명이 한글이면 남는 글자가 없으므로 기본값을 쓴다.
    뒤에 짧은 무작위 문자열을 붙여 다른 조직과 겹치지 않게 한다.
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "org"
    for _ in range(10):
        slug = f"{base}-{secrets.token_hex(2)}"
        if db.scalar(select(Organization).where(Organization.slug == slug)) is None:
            return slug
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "식별자를 만들지 못했습니다")


@router.post("", status_code=status.HTTP_201_CREATED)
def create_organization(
    payload: OrganizationCreateRequest,
    ctx: Ctx = Depends(writable_user),
    db: Session = Depends(get_db),
) -> OrganizationCreatedOut:
    """가입 직후 조직을 만든다. 사용자당 하나만 가질 수 있다."""
    # 사용자 행을 잠근 뒤 확인한다. 잠그지 않으면 더블클릭 같은 동시 요청에서
    # 두 요청이 모두 "조직 없음"을 통과해 조직이 두 개 만들어지고,
    # 먼저 발급된 토큰의 조직 정보가 DB와 어긋난다.
    user = db.scalar(select(User).where(User.id == ctx.user_id).with_for_update())
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "사용자를 찾을 수 없습니다")
    if user.organization_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 소속된 조직이 있습니다")

    org = Organization(name=payload.name, slug=make_slug(payload.name, db), plan="starter")
    db.add(org)
    db.flush()

    user.organization_id = org.id
    user.role = "admin"
    db.commit()
    db.refresh(org)
    db.refresh(user)

    return OrganizationCreatedOut(
        organization=OrganizationOut.of(org),
        access_token=create_token(user),
    )
