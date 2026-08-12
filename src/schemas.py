"""API 요청·응답 스키마.

형태는 docs/api-spec.md를 따른다. 명세를 바꾸려면 문서를 먼저 고친다.
"""

from pydantic import BaseModel, EmailStr, Field

from src.db.models import Organization, User


class SignupRequest(BaseModel):
    email: EmailStr
    # bcrypt는 72바이트를 넘는 입력을 처리하지 못한다.
    password: str = Field(min_length=8, max_length=64)
    name: str = Field(min_length=1, max_length=50)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=64)


class OrganizationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    role: str

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls(id=user.id, email=user.email, name=user.name, role=user.role)


class OrganizationOut(BaseModel):
    id: int
    name: str
    slug: str
    plan: str

    @classmethod
    def of(cls, org: Organization) -> "OrganizationOut":
        return cls(id=org.id, name=org.name, slug=org.slug, plan=org.plan)


class SessionOut(BaseModel):
    """로그인·회원가입·데모 세션이 공통으로 돌려주는 형태."""

    access_token: str
    token_type: str = "bearer"
    read_only: bool = False
    user: UserOut
    organization: OrganizationOut | None = None


class OrganizationCreatedOut(BaseModel):
    """조직 생성 응답.

    가입 시점 토큰에는 조직이 없어 그대로 두면 이후 조회가 전부 비어 나온다.
    새 토큰을 함께 돌려주고 프론트가 교체한다.
    """

    organization: OrganizationOut
    access_token: str


class MeOut(BaseModel):
    """세션 복원. 토큰은 이미 클라이언트에 있으므로 돌려주지 않는다."""

    read_only: bool = False
    user: UserOut
    organization: OrganizationOut | None = None
