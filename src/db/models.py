"""DB 모델 전체.

규칙 (예외 없음):
- organizations를 제외한 모든 테이블에 organization_id가 있다.
- 조회는 src/db/query.py의 query() 래퍼만 사용한다. raw 쿼리·select() 직접 사용 금지.
- 심볼 식별자는 "파일경로::함수명" 형식으로 통일한다 (그래프 노드 id, PR 경고, 질의 이력 공통).
"""

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.session import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OrgScopedMixin:
    """조직 격리 대상 테이블이 공통으로 갖는 컬럼.

    query() 래퍼가 이 컬럼을 강제로 필터링한다. 이 믹스인을 빼먹은 모델은
    query()에서 AttributeError로 즉시 실패한다.
    """

    @property
    def _org_fk(self) -> str:
        return "organizations.id"

    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )


# ---------------------------------------------------------------- 조직·사용자


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(20), default="starter")  # starter|team|business
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)

    # GitHub App 설치 정보 (조직당 하나)
    github_installation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    github_account: Mapped[str | None] = mapped_column(String(100), nullable=True)

    users: Mapped[list["User"]] = relationship(back_populates="organization")

    @property
    def repo_limit(self) -> int:
        return {"starter": 3, "team": 10}.get(self.plan, 999)


class User(OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    role: Mapped[str] = mapped_column(String(10), default="member")  # admin|member

    organization: Mapped["Organization"] = relationship(back_populates="users")


# ---------------------------------------------------------------- 레포·인덱싱


class Repo(OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "repos"
    __table_args__ = (
        UniqueConstraint("organization_id", "github_full_name", name="uq_repo_org_fullname"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    github_full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(100), default="main")

    # collecting -> parsing -> done | failed
    indexing_status: Mapped[str] = mapped_column(String(20), default="collecting")
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    files_count: Mapped[int] = mapped_column(Integer, default=0)
    functions_count: Mapped[int] = mapped_column(Integer, default=0)
    commits_count: Mapped[int] = mapped_column(Integer, default=0)
    prs_count: Mapped[int] = mapped_column(Integer, default=0)


class SourceFile(OrgScopedMixin, Base):
    """인덱싱된 파일. 파일 트리와 코드 뷰어의 원본."""

    __tablename__ = "source_files"
    __table_args__ = (UniqueConstraint("repo_id", "path", name="uq_file_repo_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)  # python|typescript|javascript
    content: Mapped[str] = mapped_column(Text, nullable=False)


class Symbol(OrgScopedMixin, Base):
    """파서가 추출한 심볼 (함수·상수·클래스).

    ident는 "파일경로::함수명" 형식. 그래프 노드 id와 동일하다.
    """

    __tablename__ = "symbols"
    __table_args__ = (
        UniqueConstraint("repo_id", "ident", name="uq_symbol_repo_ident"),
        Index("ix_symbol_repo_path", "repo_id", "path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    ident: Mapped[str] = mapped_column(String(600), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # function|constant|class
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    # 파라미터 이름 목록. PR 시그니처 변경 판별의 비교 대상.
    params: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 레포 전체에서 이 심볼이 참조되는 횟수. 그래프 15개 초과 시 정렬 기준.
    reference_count: Mapped[int] = mapped_column(Integer, default=0)


class Reference(OrgScopedMixin, Base):
    """심볼 간 연결. 추적 유형 4가지만 저장한다.

    테이블명이 symbol_references인 이유: references는 SQL 예약어라
    psql에서 따옴표 없이 조회하면 문법 오류가 난다.
    """

    __tablename__ = "symbol_references"
    __table_args__ = (
        Index("ix_reference_target", "repo_id", "target_ident"),
        Index("ix_reference_source", "repo_id", "source_ident"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    source_ident: Mapped[str] = mapped_column(String(600), nullable=False)
    target_ident: Mapped[str] = mapped_column(String(600), nullable=False)
    # call|import|constant|inheritance
    ref_type: Mapped[str] = mapped_column(String(20), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    line: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------------------------------------------------------------- 이력 근거


class Commit(OrgScopedMixin, Base):
    __tablename__ = "commits"
    __table_args__ = (UniqueConstraint("repo_id", "sha", name="uq_commit_repo_sha"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    sha: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str] = mapped_column(String(100), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)


class PullRequest(OrgScopedMixin, Base):
    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repo_id", "number", name="uq_pr_repo_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str] = mapped_column(String(100), nullable=False)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    # 리뷰 스레드에서 뽑은 핵심 코멘트. 맥락 패널의 review_excerpt 원본.
    review_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)


class SymbolEvidence(OrgScopedMixin, Base):
    """심볼 ↔ 근거(커밋·PR) 연결.

    git log -L로 심볼의 라인 범위 전체 이력을 수집해 만든다.
    git blame은 마지막 커밋만 남기므로 사용하지 않는다.
    """

    __tablename__ = "symbol_evidences"
    __table_args__ = (Index("ix_evidence_symbol", "repo_id", "symbol_ident"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    symbol_ident: Mapped[str] = mapped_column(String(600), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # commit|pr
    commit_id: Mapped[int | None] = mapped_column(
        ForeignKey("commits.id", ondelete="CASCADE"), nullable=True
    )
    pull_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE"), nullable=True
    )


class SymbolSummary(OrgScopedMixin, Base):
    """LLM이 생성한 배경 요약. 근거 상태에 따라 status가 달라진다."""

    __tablename__ = "symbol_summaries"
    __table_args__ = (UniqueConstraint("repo_id", "symbol_ident", name="uq_summary_repo_symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    symbol_ident: Mapped[str] = mapped_column(String(600), nullable=False)
    # ok|no_history|conflicting
    status: Mapped[str] = mapped_column(String(20), default="ok")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ---------------------------------------------------------------- PR 사전 검사


class PrWarning(OrgScopedMixin, TimestampMixin, Base):
    """웹훅이 판별한 PR 경고. 화면 9(PR 경고 이력)의 원본.

    comment_id는 PR당 코멘트 1개 유지를 위해 저장한다.
    재검사 시 새 코멘트를 달지 말고 이 ID의 코멘트를 수정한다.
    """

    __tablename__ = "pr_warnings"
    __table_args__ = (
        UniqueConstraint("repo_id", "pr_number", name="uq_warning_repo_pr"),
        Index("ix_warning_org_created", "organization_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), index=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_title: Mapped[str] = mapped_column(String(500), nullable=False)
    pr_url: Mapped[str] = mapped_column(String(500), nullable=False)
    author: Mapped[str] = mapped_column(String(100), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    items: Mapped[list["PrWarningItem"]] = relationship(
        back_populates="warning", cascade="all, delete-orphan"
    )


class PrWarningItem(OrgScopedMixin, Base):
    """경고 1건. 변경된 심볼 하나에 대응한다."""

    __tablename__ = "pr_warning_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    pr_warning_id: Mapped[int] = mapped_column(
        ForeignKey("pr_warnings.id", ondelete="CASCADE"), index=True
    )
    # signature_changed|deleted|renamed|constant_changed (이 4가지만 경고한다)
    change_type: Mapped[str] = mapped_column(String(30), nullable=False)
    symbol_ident: Mapped[str] = mapped_column(String(600), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)

    warning: Mapped["PrWarning"] = relationship(back_populates="items")
    impacts: Mapped[list["PrWarningImpact"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )


class PrWarningImpact(OrgScopedMixin, Base):
    """경고 1건이 영향을 주는 위치."""

    __tablename__ = "pr_warning_impacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    pr_warning_item_id: Mapped[int] = mapped_column(
        ForeignKey("pr_warning_items.id", ondelete="CASCADE"), index=True
    )
    symbol_ident: Mapped[str] = mapped_column(String(600), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    line: Mapped[int] = mapped_column(Integer, nullable=False)
    ref_type: Mapped[str] = mapped_column(String(20), nullable=False)

    item: Mapped["PrWarningItem"] = relationship(back_populates="impacts")


# ---------------------------------------------------------------- 통제·전환


class QueryLog(OrgScopedMixin, TimestampMixin, Base):
    """질의 이력. 조직 관리자만 조회하고 90일 후 삭제한다."""

    __tablename__ = "query_logs"
    __table_args__ = (Index("ix_querylog_org_created", "organization_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    user_name: Mapped[str] = mapped_column(String(50), nullable=False)
    repo_id: Mapped[int | None] = mapped_column(
        ForeignKey("repos.id", ondelete="SET NULL"), nullable=True
    )
    repo_name: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)  # context_view|graph_view
    target: Mapped[str] = mapped_column(String(600), nullable=False)


class Inquiry(TimestampMixin, Base):
    """구독 문의. 가입 전 방문자도 남기므로 조직에 속하지 않는다.

    조직 데이터가 아니므로 organization_id를 두지 않는 유일한 예외이며,
    query() 래퍼 대상이 아니다. 조회는 서비스 운영자만 한다.
    """

    __tablename__ = "inquiries"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_name: Mapped[str] = mapped_column(String(100), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(50), nullable=False)
    contact: Mapped[str] = mapped_column(String(100), nullable=False)
    plan: Mapped[str] = mapped_column(String(20), nullable=False)
