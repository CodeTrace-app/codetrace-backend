"""맥락 API (api-spec §4). 선택한 코드 위치의 배경 요약과 근거를 돌려준다.

요약은 인덱싱 중에 미리 만들어 저장해두고 여기서는 읽기만 한다.
사용자가 함수를 클릭할 때마다 LLM 왕복을 기다리게 하지 않기 위함이다.
"""

import logging
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.auth import Ctx, current_user, get_db
from src.db.models import Commit, QueryLog, Repo, Symbol, SymbolSummary, User
from src.db.query import query
from src.llm.summarizer import load_evidence, select_for_display
from src.schemas import CommitEvidenceOut, ContextOut, FunctionOut, ParentModuleOut, PrEvidenceOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repos", tags=["context"])


def _innermost_symbol(symbols: list[Symbol], line: int) -> Symbol | None:
    """line을 감싸는 가장 안쪽 심볼.

    중첩 함수나 클래스 안의 메서드에서는 여러 심볼이 같은 줄을 포함한다.
    범위가 가장 좁은 것이 사용자가 클릭한 대상이다 (S-ZEZFED).
    """
    covering = [s for s in symbols if s.start_line <= line <= s.end_line]
    if not covering:
        return None
    return min(covering, key=lambda s: s.end_line - s.start_line)


def _parent_module(path: str) -> ParentModuleOut | None:
    """근거가 없을 때 사용자가 옮겨갈 상위 디렉터리 (S-UBXNLW: 빈 화면 금지)."""
    parent = PurePosixPath(path).parent
    if str(parent) in (".", "", "/"):
        return None
    return ParentModuleOut(path=str(parent), name=parent.name)


def _load_display_evidence(db: Session, repo: Repo, symbol_idents: list[str]):
    """화면에 실을 근거를 최신순으로 고른다.

    선별을 요약 쪽과 같은 함수로 한다. 두 곳이 각자 고르면 요약이 인용한 근거가
    목록에 없는 상태가 생기고, "추측이 아니라 근거"라는 이 제품의 주장이 화면에서 깨진다.
    """
    items = load_evidence(db, repo, symbol_idents)
    display = select_for_display(items)
    return display, len(items) > len(display)


def _to_evidence_out(items) -> list[CommitEvidenceOut | PrEvidenceOut]:
    out: list[CommitEvidenceOut | PrEvidenceOut] = []
    for item in items:
        row = item.source
        if isinstance(row, Commit):
            out.append(
                CommitEvidenceOut(
                    sha=row.sha, title=row.title, author=row.author, date=row.committed_at, url=row.url
                )
            )
        else:
            out.append(
                PrEvidenceOut(
                    number=row.number,
                    title=row.title,
                    date=row.merged_at,
                    url=row.url,
                    review_excerpt=row.review_excerpt,
                )
            )
    return out


def _record_query_log(db: Session, ctx: Ctx, repo: Repo, target: str) -> None:
    """질의 이력 (api-spec §6). 조직 관리자가 나중에 조회한다.

    §4에는 안 적혀 있지만 §6의 action="context_view"가 이 API를 가리킨다.
    기록에 실패해도 사용자 응답을 막지 않는다.
    """
    try:
        user = db.scalar(query(User, ctx.organization_id).where(User.id == ctx.user_id))
        db.add(
            QueryLog(
                organization_id=ctx.organization_id,
                user_id=ctx.user_id,
                user_name=(user.name if user else "unknown")[:50],
                repo_id=repo.id,
                repo_name=repo.name,
                action="context_view",
                target=target,
            )
        )
        db.commit()
    except Exception:
        logger.exception("질의 이력 기록 실패: repo=%s target=%s", repo.id, target)
        db.rollback()


@router.get("/{repo_id}/context")
def get_context(
    repo_id: int,
    path: str = Query(..., min_length=1, max_length=500),
    line: int = Query(..., ge=1),
    ctx: Ctx = Depends(current_user),
    db: Session = Depends(get_db),
) -> ContextOut:
    """선택한 위치의 배경 맥락. 데모 세션도 읽을 수 있다 (🚫데모 표시 없음)."""
    repo = db.scalar(query(Repo, ctx.organization_id).where(Repo.id == repo_id))
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "레포를 찾을 수 없습니다")

    symbols = list(
        db.scalars(
            query(Symbol, ctx.organization_id)
            .where(Symbol.repo_id == repo.id)
            .where(Symbol.path == path)
        )
    )
    symbol = _innermost_symbol(symbols, line)

    if symbol is not None:
        function = FunctionOut(
            name=symbol.name, path=symbol.path, start_line=symbol.start_line, end_line=symbol.end_line
        )
        idents = [symbol.ident]
        target = symbol.ident
    else:
        # 함수 밖(모듈 레벨)이면 파일 단위 맥락으로 답한다 (api-spec §4).
        # 404가 아니다 — 클릭한 위치가 함수 밖이라는 이유로 화면을 비우면 안 된다.
        end_line = max((s.end_line for s in symbols), default=line)
        function = FunctionOut(
            name=PurePosixPath(path).name, path=path, start_line=1, end_line=end_line
        )
        idents = [s.ident for s in symbols]
        target = path

    evidence_items, truncated = _load_display_evidence(db, repo, idents)

    summary_row = None
    if symbol is not None:
        summary_row = db.scalar(
            query(SymbolSummary, ctx.organization_id)
            .where(SymbolSummary.repo_id == repo.id)
            .where(SymbolSummary.symbol_ident == symbol.ident)
        )

    if summary_row is not None and evidence_items:
        result_status = summary_row.status
        summary = summary_row.summary
    elif summary_row is not None:
        # 근거는 사라졌는데 요약만 남은 상태(재인덱싱 중 함수 이동 등).
        # 확인할 근거가 없는 요약을 그대로 보여주면 근거 기반이라는 주장이 깨진다.
        logger.warning("근거 없는 요약을 숨김: repo=%s symbol=%s", repo.id, target)
        result_status = "no_history"
        summary = None
    elif evidence_items:
        # 근거는 있는데 요약이 아직 없다(생성 실패 또는 미생성). 근거를 no_history로 왜곡하지 않는다.
        result_status = "ok"
        summary = None
    else:
        result_status = "no_history"
        summary = None

    # 응답을 먼저 만든다. 질의 이력의 commit()이 세션을 만료시켜서, 뒤에 직렬화하면
    # 방금 읽은 근거 객체를 전부 다시 조회하게 된다 (운영 세션은 expire_on_commit=True).
    response = ContextOut(
        function=function,
        status=result_status,
        summary=summary,
        evidence=_to_evidence_out(evidence_items),
        evidence_truncated=truncated,
        # 근거가 없을 때 이동 경로를 준다. 근거가 있으면 이동할 이유가 없다.
        parent_module=_parent_module(path) if result_status == "no_history" else None,
    )
    _record_query_log(db, ctx, repo, target)
    return response
