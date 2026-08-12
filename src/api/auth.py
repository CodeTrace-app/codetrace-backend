"""인증 엔드포인트."""

import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth import Ctx, create_token, current_user, get_db, hash_password, verify_password
from src.db.models import Organization, User
from src.schemas import LoginRequest, MeOut, OrganizationOut, SessionOut, SignupRequest, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

DEMO_SLUG = "demo"
DEMO_EMAIL = "demo@codetrace.app"


def _session(user: User, org: Organization | None, read_only: bool = False) -> SessionOut:
    return SessionOut(
        access_token=create_token(user, read_only=read_only),
        read_only=read_only,
        user=UserOut.of(user),
        organization=OrganizationOut.of(org) if org else None,
    )


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)) -> SessionOut:
    """회원가입.

    이 시점에는 조직이 없다. 응답의 organization이 null이면
    프론트는 조직 생성 화면으로 보낸다.
    """
    exists = db.scalar(select(User).where(User.email == payload.email))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 가입된 이메일입니다")

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        name=payload.name,
        role="admin",  # 조직을 만드는 사람이 관리자가 된다
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _session(user, None)


@router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> SessionOut:
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "이메일 또는 비밀번호가 올바르지 않습니다")

    org = db.get(Organization, user.organization_id) if user.organization_id else None
    return _session(user, org)


@router.post("/demo")
def demo_session(db: Session = Depends(get_db)) -> SessionOut:
    """로그인 없이 들어오는 데모 세션.

    데모 조직의 organization_id를 가진 읽기 전용 세션을 발급한다.
    격리 규칙을 우회하는 경로를 만들지 않고 일반 세션과 같은 길을 지난다.
    데모 데이터(레포·인덱스)는 별도로 준비한다.
    """
    org = db.scalar(select(Organization).where(Organization.slug == DEMO_SLUG))
    if org is None:
        org = Organization(name="Acme Corp (데모)", slug=DEMO_SLUG, plan="team", is_demo=True)
        db.add(org)
        db.flush()

    user = db.scalar(select(User).where(User.email == DEMO_EMAIL))
    if user is None:
        user = User(
            organization_id=org.id,
            email=DEMO_EMAIL,
            # 이 계정으로는 로그인할 수 없다. 데모 세션 발급으로만 쓰인다.
            password_hash=hash_password(secrets.token_urlsafe(32)),
            name="데모 사용자",
            role="member",
        )
        db.add(user)
    db.commit()
    db.refresh(user)
    db.refresh(org)

    return _session(user, org, read_only=True)


@router.get("/me")
def me(ctx: Ctx = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    """새로고침 시 세션 복원."""
    user = db.get(User, ctx.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "사용자를 찾을 수 없습니다")

    org = db.get(Organization, user.organization_id) if user.organization_id else None
    return MeOut(
        read_only=ctx.read_only,
        user=UserOut.of(user),
        organization=OrganizationOut.of(org) if org else None,
    )
