"""API 요청·응답 스키마.

형태는 docs/api-spec.md를 따른다. 명세를 바꾸려면 문서를 먼저 고친다.
"""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from src.db.models import Organization, Repo, User


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


class RepoCreateRequest(BaseModel):
    # "소유자/레포" 형식만 받는다. 이 값이 클론 경로와 GitHub API 경로에 그대로 들어가서,
    # ".."이나 슬래시가 섞이면 의도하지 않은 디렉터리·엔드포인트를 가리킬 수 있다.
    # 각 조각 100자 제한은 Repo.name(String(100))에 맞춘 것이다.
    github_full_name: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")


class RepoProgressOut(BaseModel):
    current: int
    total: int


class RepoStatsOut(BaseModel):
    files: int
    functions: int
    commits: int
    prs: int

    @classmethod
    def of(cls, repo: Repo) -> "RepoStatsOut":
        return cls(files=repo.files_count, functions=repo.functions_count, commits=repo.commits_count, prs=repo.prs_count)


class RepoOut(BaseModel):
    """레포 카드 하나. GET /repos 목록과 POST /repos 응답이 공통으로 쓴다."""

    id: int
    name: str
    github_full_name: str
    default_branch: str
    language: str | None
    indexing_status: str
    progress: RepoProgressOut | None
    last_indexed_at: datetime | None
    stats: RepoStatsOut

    @classmethod
    def of(cls, repo: Repo) -> "RepoOut":
        progress = None
        if repo.progress_total is not None:
            progress = RepoProgressOut(current=repo.progress_current or 0, total=repo.progress_total)
        return cls(
            id=repo.id,
            name=repo.name,
            github_full_name=repo.github_full_name,
            default_branch=repo.default_branch,
            language=repo.language,
            indexing_status=repo.indexing_status,
            progress=progress,
            last_indexed_at=repo.last_indexed_at,
            stats=RepoStatsOut.of(repo),
        )


class DashboardSummaryOut(BaseModel):
    github_account: str | None
    github_connected: bool
    repo_count: int
    commit_count: int
    review_comment_count: int
    last_indexed_at: datetime | None


class RepoListOut(BaseModel):
    summary: DashboardSummaryOut
    repos: list[RepoOut]


class ReindexOut(BaseModel):
    id: int
    indexing_status: str
