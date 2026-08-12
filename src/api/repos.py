"""레포 연동·인덱싱 API. docs/api-spec.md §3.

실제 커밋/PR 수집과 구문 분석은 이 라우터의 책임이 아니다 (#27, 분석팀 파서).
여기서는 인덱싱 대상 레포를 등록하고 `collecting` 상태로 시작하는 것과,
대시보드가 읽는 진행 상태·통계를 노출하는 것까지만 한다.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.auth import Ctx, current_user, get_db, writable_user
from src.db.models import PullRequest, Repo
from src.db.query import org_query, query
from src.indexing.runner import IN_PROGRESS, run_indexing
from src.schemas import DashboardSummaryOut, ReindexOut, RepoCreateRequest, RepoListOut, RepoOut

router = APIRouter(prefix="/repos", tags=["repos"])


def _get_org_or_404(ctx: Ctx, db: Session):
    org = db.scalar(org_query(ctx.organization_id)) if ctx.organization_id is not None else None
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "조직을 찾을 수 없습니다")
    return org


@router.get("")
def list_repos(ctx: Ctx = Depends(current_user), db: Session = Depends(get_db)) -> RepoListOut:
    org = _get_org_or_404(ctx, db)
    repos = list(db.scalars(query(Repo, ctx.organization_id)))
    # PR 개수가 아니라 코멘트 총 개수다 (api-spec §3의 "수집된 리뷰" 카드).
    review_comment_count = sum(
        pr.review_comment_count for pr in db.scalars(query(PullRequest, ctx.organization_id))
    )
    last_indexed_at = max((r.last_indexed_at for r in repos if r.last_indexed_at), default=None)

    summary = DashboardSummaryOut(
        github_account=org.github_account,
        github_connected=org.github_installation_id is not None,
        repo_count=len(repos),
        commit_count=sum(r.commits_count for r in repos),
        review_comment_count=review_comment_count,
        last_indexed_at=last_indexed_at,
    )
    return RepoListOut(summary=summary, repos=[RepoOut.of(r) for r in repos])


@router.post("", status_code=status.HTTP_201_CREATED)
def add_repo(
    payload: RepoCreateRequest,
    background: BackgroundTasks,
    ctx: Ctx = Depends(writable_user),
    db: Session = Depends(get_db),
) -> RepoOut:
    org = _get_org_or_404(ctx, db)
    if org.github_installation_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitHub App이 설치되어 있지 않습니다")

    already_count = len(db.scalars(query(Repo, ctx.organization_id)).all())
    if already_count >= org.repo_limit:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{org.plan.capitalize()} 플랜은 {org.repo_limit}개까지 레포를 추가할 수 있습니다",
        )

    duplicate = db.scalar(query(Repo, ctx.organization_id).where(Repo.github_full_name == payload.github_full_name))
    if duplicate is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 추가된 레포입니다")

    repo = Repo(
        organization_id=ctx.organization_id,
        name=payload.github_full_name.split("/")[-1],
        github_full_name=payload.github_full_name,
        indexing_status="collecting",
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)

    # 선택 즉시 인덱싱을 시작한다 (S-FWXUHO).
    background.add_task(run_indexing, repo.id, ctx.organization_id, org.github_installation_id)
    return RepoOut.of(repo)


@router.post("/{repo_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
def reindex_repo(
    repo_id: int,
    background: BackgroundTasks,
    ctx: Ctx = Depends(writable_user),
    db: Session = Depends(get_db),
) -> ReindexOut:
    org = _get_org_or_404(ctx, db)
    repo = db.scalar(query(Repo, ctx.organization_id).where(Repo.id == repo_id))
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "레포를 찾을 수 없습니다")
    if repo.indexing_status in IN_PROGRESS:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 인덱싱이 진행 중입니다")
    if org.github_installation_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitHub App이 설치되어 있지 않습니다")

    repo.indexing_status = "collecting"
    repo.progress_current = None
    repo.progress_total = None
    db.commit()

    background.add_task(run_indexing, repo.id, ctx.organization_id, org.github_installation_id)
    return ReindexOut(id=repo.id, indexing_status=repo.indexing_status)
