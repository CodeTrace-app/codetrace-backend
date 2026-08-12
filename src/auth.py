"""인증·인가 공통 모듈.

API를 만드는 모든 담당자는 여기 있는 의존성을 써서 현재 사용자·조직을 받는다.
직접 토큰을 파싱하거나 organization_id를 요청 본문에서 받지 않는다.

사용 예:
    @router.get("/repos")
    def list_repos(ctx: Ctx = Depends(current_user), db: Session = Depends(get_db)):
        rows = db.scalars(query(Repo, ctx.organization_id)).all()

    @router.post("/repos")
    def add_repo(ctx: Ctx = Depends(writable_user), ...):   # 데모 세션 차단
        ...

    @router.get("/admin/query-logs")
    def logs(ctx: Ctx = Depends(admin_user), ...):          # 관리자만
        ...
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import User
from src.db.session import SessionLocal

ALGORITHM = "HS256"
TOKEN_TTL = timedelta(days=7)

_bearer = HTTPBearer(auto_error=False)
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


@dataclass(frozen=True)
class Ctx:
    """요청을 보낸 사용자의 신원. 모든 조회는 이 organization_id로 격리된다.

    organization_id는 조직을 만들기 전 사용자에게만 None이다.
    그 상태에서는 query() 래퍼를 통과하는 데이터가 없다.
    """

    user_id: int
    organization_id: int | None
    role: str
    read_only: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hash_password(raw: str) -> str:
    return _pwd.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return _pwd.verify(raw, hashed)


def create_token(user: User, read_only: bool = False) -> str:
    """로그인·회원가입·데모 세션 발급이 공통으로 쓰는 토큰 생성기.

    데모 세션도 실제 사용자·조직으로 발급한다. read_only 플래그만 다르다.
    """
    payload = {
        "sub": str(user.id),
        "org": user.organization_id,
        "role": user.role,
        "ro": read_only,
        "exp": datetime.now(timezone.utc) + TOKEN_TTL,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Ctx:
    """읽기 API의 기본 의존성. 토큰이 없거나 만료면 401."""
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증이 필요합니다")
    try:
        payload = jwt.decode(cred.credentials, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "토큰이 유효하지 않습니다")

    org = payload.get("org")
    return Ctx(
        user_id=int(payload["sub"]),
        organization_id=int(org) if org is not None else None,
        role=payload.get("role", "member"),
        read_only=bool(payload.get("ro", False)),
    )


def writable_user(ctx: Ctx = Depends(current_user)) -> Ctx:
    """쓰기 API용. 데모 세션은 403.

    레포 추가·재인덱싱·관리자 설정 등 데모에서 막아야 하는 곳에 쓴다.
    """
    if ctx.read_only:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "데모 세션에서는 사용할 수 없습니다")
    return ctx


def admin_user(ctx: Ctx = Depends(writable_user)) -> Ctx:
    """조직 관리자 전용. 질의 이력·요금제 조회 등에 쓴다."""
    if not ctx.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "조직 관리자만 사용할 수 있습니다")
    return ctx
