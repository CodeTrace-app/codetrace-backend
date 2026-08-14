"""API 요청·응답 스키마.

형태는 docs/api-spec.md를 따른다. 명세를 바꾸려면 문서를 먼저 고친다.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, EmailStr, Field

from src.db.models import Organization, Repo, User


def _fits_bcrypt(value: str) -> str:
    """bcrypt가 처리할 수 있는 길이인지 본다.

    bcrypt는 72바이트를 넘는 부분을 조용히 버린다. 글자 수만 제한하면
    한글이나 이모지가 섞인 비밀번호는 뒷부분이 검증에 반영되지 않는다
    (한글 한 글자가 UTF-8로 3바이트라 24자만 넘어도 한도에 닿는다).
    """
    if len(value.encode("utf-8")) > 72:
        raise ValueError("비밀번호가 너무 깁니다 (한글은 한 글자가 3바이트로 계산됩니다)")
    return value


BcryptPassword = Annotated[str, AfterValidator(_fits_bcrypt)]


class SignupRequest(BaseModel):
    email: EmailStr
    password: BcryptPassword = Field(min_length=8)
    name: str = Field(min_length=1, max_length=50)


class LoginRequest(BaseModel):
    email: EmailStr
    password: BcryptPassword


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
    조각당 100자는 Repo.name(String(100))에, 전체 200자는
    Repo.github_full_name(String(200))에 맞춘 것이다. 조각 길이만 막으면
    201자가 통과해 저장 시점에 500이 난다.
    """

    github_full_name: str = Field(
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$",
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


# ---------------------------------------------------------------- 탐색기 (api-spec §4)


class TreeFileOut(BaseModel):
    """파일 노드. 디렉터리와 필드 구성이 달라 모델을 나눈다.

    하나로 합쳐 children: null을 파일에 붙이면 프론트 목데이터와 어긋난다.
    language는 파일에서만 null이 될 수 있다(미지원 언어). 그건 값이 있는 것이다.
    """

    path: str
    name: str
    type: Literal["file"] = "file"
    language: str | None


class TreeDirOut(BaseModel):
    path: str
    name: str
    type: Literal["dir"] = "dir"
    children: list["TreeDirOut | TreeFileOut"]


class TreeOut(BaseModel):
    root: list[TreeDirOut | TreeFileOut]


class FileFunctionOut(BaseModel):
    """뷰어에서 클릭할 수 있는 함수 범위."""

    name: str
    start_line: int
    end_line: int


class FileOut(BaseModel):
    path: str
    language: str | None
    content: str
    truncated: bool
    functions: list[FileFunctionOut]


class GraphRootOut(BaseModel):
    """그래프의 기준 심볼. depth·direction이 없다 — 자기 자신이라서다."""

    id: str
    name: str
    path: str
    kind: str


class GraphNodeOut(GraphRootOut):
    depth: int
    direction: Literal["caller", "callee"]
    reference_count: int


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    # 프론트가 색과 텍스트 두 가지로 함께 표기한다 (색맹·흑백 대응, api-spec §4).
    type: Literal["call", "import", "constant", "inheritance"]


class GraphOut(BaseModel):
    root: GraphRootOut
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    total_nodes: int
    truncated: bool


# ---------------------------------------------------------------- PR 경고 (api-spec §5)


class ImpactedOut(BaseModel):
    symbol: str
    path: str
    line: int
    type: Literal["call", "import", "constant", "inheritance"]


class WarningOut(BaseModel):
    change_type: Literal["signature_changed", "deleted", "renamed", "constant_changed"]
    symbol: str
    detail: str
    impacted: list[ImpactedOut]


class PrWarningOut(BaseModel):
    """PR 경고 이력 한 건. §5 응답의 items[] 원소."""

    id: int
    repo: str
    pr_number: int
    pr_title: str
    pr_url: str
    author: str
    created_at: datetime
    warnings: list[WarningOut]


class PrWarningListOut(BaseModel):
    items: list[PrWarningOut]
    page: int
    per_page: int
    total: int
