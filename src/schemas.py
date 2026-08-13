"""API 요청·응답 스키마.

형태는 docs/api-spec.md를 따른다. 명세를 바꾸려면 문서를 먼저 고친다.
"""

from datetime import datetime
from typing import Literal

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


class InquiryCreateRequest(BaseModel):
    organization_name: str = Field(min_length=1, max_length=100)
    contact_name: str = Field(min_length=1, max_length=50)
    contact: str = Field(min_length=1, max_length=100)
    # 요금제 화면의 세 플랜. 다른 값이 오면 접수 단계에서 막는다.
    plan: Literal["starter", "team", "business"]


class InquiryCreatedOut(BaseModel):
    id: int
    message: str


class PlanOut(BaseModel):
    """관리자 설정 화면의 현재 요금제."""

    plan: str
    price_krw: int
    repo_limit: int
    repos_used: int


class RepoCreateRequest(BaseModel):
    """레포 등록 요청.

    "소유자/레포" 형식만 받는다. 이 값이 클론 경로와 GitHub API 경로에 그대로 들어가서,
    ".."이 섞이면 의도하지 않은 엔드포인트를 가리킨다 (httpx가 dot segment를 정규화한다).
    각 조각은 영숫자로 시작하게 해서 ".."과 "."을 원천 차단한다.
    100자 제한은 Repo.name(String(100))에 맞춘 것이다.
    """

    github_full_name: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
    )


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
        # 진행 중일 때만 progress를 준다 (api-spec §3). done·failed인데 값이 남아 있으면
        # 실패한 카드에 100% 진행바가 뜬다. DB에 남은 값이 어떻든 여기서 계약을 지킨다.
        in_progress = repo.indexing_status in ("collecting", "parsing")
        progress = None
        if in_progress and repo.progress_total is not None:
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


# ---------------------------------------------------------------- 맥락 (api-spec §4)


class FunctionOut(BaseModel):
    name: str
    path: str
    start_line: int
    end_line: int


class CommitEvidenceOut(BaseModel):
    """커밋 근거. PR 근거와 필드 구성이 다르므로 모델을 나눈다.

    하나로 합쳐 null을 섞어 내보내면 프론트 목데이터와 어긋난다 (api-spec §4).
    """

    kind: Literal["commit"] = "commit"
    sha: str
    title: str
    author: str
    date: datetime
    url: str


class PrEvidenceOut(BaseModel):
    kind: Literal["pr"] = "pr"
    number: int
    title: str
    date: datetime | None
    url: str
    review_excerpt: str | None


class ParentModuleOut(BaseModel):
    path: str
    name: str


class ContextOut(BaseModel):
    """맥락 패널 응답. 필드 6개 고정 — 추가하면 계약 위반이다."""

    function: FunctionOut
    status: Literal["ok", "no_history", "conflicting"]
    summary: str | None
    evidence: list[CommitEvidenceOut | PrEvidenceOut]
    evidence_truncated: bool
    parent_module: ParentModuleOut | None
