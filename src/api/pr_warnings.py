"""PR 웹훅과 경고 이력 (이슈 #29, api-spec §5·§8).

웹훅이 받는 것: pull_request의 opened·synchronize. 변경된 파일만 base/head
리비전에서 즉시 다시 읽어 이슈 #24의 판별 함수(src.analysis.detect_changes)에
넘긴다. 저장된 인덱스로는 변경 여부를 판단하지 않는다 — 판별 함수의 책임이자
전제라 여기서도 지킨다. 인덱스는 판별 함수 내부에서 "영향받는 위치"를 찾는 데만 쓰인다.
"""

import hashlib
import hmac
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from src.analysis import ChangedFile, Impacted, Warning, detect_changes
from src.auth import Ctx, current_user, get_db
from src.config import settings
from src.db.models import Organization, PrWarning, PrWarningImpact, PrWarningItem, Repo
from src.db.query import query
from src.github.app_auth import GitHubAppError
from src.github.client import (
    create_issue_comment,
    get_file_content,
    list_pr_files,
    update_issue_comment,
)
from src.indexing.parsing import MODULE_IDENT
from src.parser import get_adapter
from src.schemas import ImpactedOut, PrWarningListOut, PrWarningOut, WarningOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pr-warnings"])

_HANDLED_ACTIONS = ("opened", "synchronize")

_CHANGE_LABELS = {
    "signature_changed": "시그니처 변경",
    "deleted": "삭제",
    "renamed": "이름 변경",
    "constant_changed": "상수 값 변경",
}


def _verify_signature(header: str | None, body: bytes) -> None:
    """X-Hub-Signature-256 서명을 검증한다. 불일치·미설정은 401 (api-spec §8)."""
    if not settings.github_webhook_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "웹훅이 설정되지 않았습니다")
    if not header:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "서명이 없습니다")
    expected = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, header):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "서명이 유효하지 않습니다")


def _collect_changed_files(installation_id: int, full_name: str, base_sha: str, head_sha: str, number: int) -> list[ChangedFile]:
    """PR의 변경 파일을 모아 base/head 소스를 채운다.

    파서가 없는 쪽은 API 호출을 아끼려고 건너뛴다. base·head 어느 한쪽만 파서가
    있어도(예: .py를 .txt로 이름 바꾸며 수정) 그쪽 소스는 채워야 한다 — 새 이름이
    지원 확장자가 아니라고 옛 파일 쪽 함수들의 "삭제" 판정까지 놓치면 안 된다.
    """
    changed_files: list[ChangedFile] = []
    for file in list_pr_files(installation_id, full_name, number):
        path = file["filename"]
        previous_path = file.get("previous_filename")
        base_path = previous_path or path
        base_supported = get_adapter(base_path) is not None
        head_supported = get_adapter(path) is not None
        if not base_supported and not head_supported:
            continue
        status_ = file["status"]

        base_source = None
        if status_ != "added" and base_supported:
            base_source = get_file_content(installation_id, full_name, base_path, base_sha)
        head_source = None
        if status_ != "removed" and head_supported:
            head_source = get_file_content(installation_id, full_name, path, head_sha)

        if base_source is None and head_source is None:
            continue
        changed_files.append(
            ChangedFile(path=path, previous_path=previous_path, base_source=base_source, head_source=head_source)
        )
    return changed_files


def _find_warning_row(db: Session, org: Organization, repo: Repo, pr_number: int) -> PrWarning | None:
    return db.scalar(
        query(PrWarning, org.id).where(PrWarning.repo_id == repo.id).where(PrWarning.pr_number == pr_number)
    )


def _upsert_warning(db: Session, org: Organization, repo: Repo, pr: dict, head_sha: str, warnings: list[Warning], row: PrWarning | None) -> PrWarning:
    """이 PR의 경고 행을 최신 판별 결과로 통째로 교체한다.

    synchronize로 다시 검사하면 이전 결과는 더 이상 유효하지 않다 —
    수정됐던 문제가 그대로일 수도, 다시 생겼을 수도 있어 항목을 지우고 다시 쓴다.
    """
    if row is None:
        row = PrWarning(
            organization_id=org.id,
            repo_id=repo.id,
            pr_number=pr["number"],
            pr_title=pr["title"],
            pr_url=pr["html_url"],
            author=pr["user"]["login"],
            head_sha=head_sha,
        )
        db.add(row)
        db.flush()
    else:
        row.pr_title = pr["title"]
        row.head_sha = head_sha
        for item in list(row.items):
            db.delete(item)
        db.flush()

    for warning in warnings:
        item = PrWarningItem(
            organization_id=org.id,
            pr_warning_id=row.id,
            change_type=warning.change_type,
            symbol_ident=warning.symbol,
            detail=warning.detail,
        )
        db.add(item)
        db.flush()
        for impacted in warning.impacted:
            db.add(
                PrWarningImpact(
                    organization_id=org.id,
                    pr_warning_item_id=item.id,
                    symbol_ident=impacted.symbol,
                    path=impacted.path,
                    line=impacted.line,
                    ref_type=impacted.type,
                )
            )
    db.commit()
    db.refresh(row)
    return row


def _explorer_link(repo_id: int, symbol: str) -> str:
    return f"{settings.frontend_url}/explorer?repo={repo_id}&fn={quote(symbol, safe='')}&tab=impact"


def _impacted_line(impacted: Impacted) -> str:
    """영향받는 위치 한 줄. import는 함수가 아니라 파일 전체를 가리킨다 (이슈 #61).

    출발 심볼이 없는 import는 내부적으로 "파일경로::<module>"로 저장하는데,
    이 내부 표시를 그대로 보여주면 신입 개발자가 함수 이름으로 오해한다.
    저장 형식은 그대로 두고 표시할 때만 파일 단위 문장으로 바꾼다. 탐색기는
    "<module>"을 열 수 없는 대상이라 함수 링크도 걸지 않는다.
    """
    if impacted.type == "import" and impacted.symbol.endswith(MODULE_IDENT):
        return f"- {impacted.path} 파일이 import ({impacted.line}행)"
    return f"- `{impacted.symbol}` ({impacted.path}:{impacted.line}, {impacted.type})"


def _build_comment_body(repo_id: int, warnings: list[Warning], head_sha: str) -> str:
    short_sha = head_sha[:7]
    if not warnings:
        return f"CodeTrace: 이전에 감지된 변경 경고가 최신 커밋(`{short_sha}`)에서 더 이상 나타나지 않습니다."

    lines = [f"## CodeTrace 변경 감지 — {len(warnings)}건", ""]
    for index, warning in enumerate(warnings, start=1):
        label = _CHANGE_LABELS[warning.change_type]
        link = _explorer_link(repo_id, warning.symbol)
        lines.append(f"### {index}. `{warning.symbol}` — {label}")
        lines.append(warning.detail)
        lines.append("")
        lines.append(f"[탐색기에서 영향 범위 보기]({link})")
        if warning.impacted:
            lines.append("")
            lines.append(f"영향받는 위치 {len(warning.impacted)}곳:")
            for impacted in warning.impacted:
                lines.append(_impacted_line(impacted))
        lines.append("")
        lines.append("---")
        lines.append("")
    lines.append(f"최신 커밋: `{short_sha}`")
    return "\n".join(lines)


def _apply_warnings(
    db: Session, org: Organization, repo: Repo, pr: dict, head_sha: str, warnings: list[Warning], installation_id: int, full_name: str, number: int,
) -> None:
    """판별 결과를 이력에 반영하고 코멘트를 맞춘다.

    처음부터 경고가 없던 PR은 이력에 아무것도 남기지 않는다 — "PR 경고 이력" 화면에
    경고 0건짜리 항목이 쌓이면 정작 봐야 할 경고를 찾기 어려워진다 (소음 방지 원칙,
    이슈 #24 판별 함수와 같은 기준). 경고가 있다가 사라진 PR은 코멘트로 알리고 이력에서 뺀다.

    코멘트를 먼저 보내고, 성공해야만 DB에 반영한다. 순서를 반대로 하면(DB 먼저) 코멘트
    API가 실패했을 때 "감지했지만 아무에게도 알리지 못한" 행이 남는데, 그 상태에서
    문제가 저절로 없어지면 경고 0건 갈래가 코멘트 ID 없는 행을 그냥 지워버려 —
    감지했다는 사실도, 해소됐다는 사실도 아무도 모르게 조용히 사라진다.
    GitHubAppError가 여기서 나면 DB에 아무것도 쓰지 않은 채 그대로 올라가 라우터의
    except가 로그만 남기므로, 다음 synchronize 때 이 웹훅이 안 왔던 것처럼 다시 시도된다.
    """
    row = _find_warning_row(db, org, repo, pr["number"])

    if not warnings and row is None:
        return

    if not warnings:
        if row.github_comment_id is not None:
            update_issue_comment(installation_id, full_name, row.github_comment_id, _build_comment_body(repo.id, [], head_sha))
        db.delete(row)
        db.commit()
        return

    body = _build_comment_body(repo.id, warnings, head_sha)
    comment_id = row.github_comment_id if row is not None else None
    if comment_id is not None and not update_issue_comment(installation_id, full_name, comment_id, body):
        comment_id = None  # 코멘트가 수동으로 지워졌다. 새로 단다.
    if comment_id is None:
        comment_id = create_issue_comment(installation_id, full_name, number, body)

    row = _upsert_warning(db, org, repo, pr, head_sha, warnings, row)
    row.github_comment_id = comment_id
    db.commit()


def _handle_pull_request_event(db: Session, payload: dict) -> None:
    """서명 검증 이후의 나머지 처리. GitHub API·DB 호출이 섞여 있어 통째로 블로킹이다.

    이 라우터가 이벤트 루프 위에서 직접 돌면(다른 라우터는 전부 동기 def라
    FastAPI가 알아서 스레드풀로 돌리지만, 여기는 원문 바이트를 읽으려고
    async def를 쓴다) PR 하나 검사하는 동안 다른 모든 요청이 멈춘다.
    호출부에서 run_in_threadpool로 감싸 이벤트 루프를 막지 않는다.
    """
    if payload.get("action") not in _HANDLED_ACTIONS:
        return

    installation_id = (payload.get("installation") or {}).get("id")
    full_name = (payload.get("repository") or {}).get("full_name")
    pr = payload.get("pull_request") or {}
    number = pr.get("number")
    if installation_id is None or full_name is None or number is None:
        return

    # organizations는 조직 자신의 테이블이라 query() 래퍼 대상이 아니다 — installation_id로
    # 조직을 찾는 것 자체가 조직 경계를 넘나드는 유일하게 정당한 조회다.
    org = db.scalar(select(Organization).where(Organization.github_installation_id == installation_id))
    if org is None:
        # 우리 서비스에 연결되지 않은 조직의 설치다. 조용히 무시한다.
        return

    repo = db.scalar(query(Repo, org.id).where(Repo.github_full_name == full_name))
    if repo is None:
        # 등록하지 않은 레포의 PR이다. 조용히 무시한다.
        return

    base_sha = (pr.get("base") or {}).get("sha")
    head_sha = (pr.get("head") or {}).get("sha")
    if not base_sha or not head_sha:
        return

    try:
        changed_files = _collect_changed_files(installation_id, full_name, base_sha, head_sha, number)
        warnings = detect_changes(db, repo, changed_files)
        _apply_warnings(db, org, repo, pr, head_sha, warnings, installation_id, full_name, number)
    except GitHubAppError:
        # 웹훅은 GitHub이 재시도하지 않으므로 여기서 500을 내도 얻는 게 없다.
        # 다음 synchronize 때 다시 시도되도록 조용히 넘긴다.
        logger.exception("PR 웹훅 처리 실패: repo=%s pr=%s", full_name, number)


@router.post("/webhooks/github", status_code=status.HTTP_204_NO_CONTENT)
async def github_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    body = await request.body()
    _verify_signature(request.headers.get("x-hub-signature-256"), body)

    if request.headers.get("x-github-event") != "pull_request":
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    payload = await request.json()
    await run_in_threadpool(_handle_pull_request_event, db, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/pr-warnings")
def list_pr_warnings(
    repo_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    ctx: Ctx = Depends(current_user),
    db: Session = Depends(get_db),
) -> PrWarningListOut:
    if ctx.organization_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "조직을 찾을 수 없습니다")

    stmt = query(PrWarning, ctx.organization_id).options(
        selectinload(PrWarning.items).selectinload(PrWarningItem.impacts)
    )
    if repo_id is not None:
        stmt = stmt.where(PrWarning.repo_id == repo_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(
            stmt.order_by(PrWarning.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
        )
    )

    repo_names = {r.id: r.name for r in db.scalars(query(Repo, ctx.organization_id))}

    items = [
        PrWarningOut(
            id=row.id,
            repo=repo_names.get(row.repo_id, ""),
            pr_number=row.pr_number,
            pr_title=row.pr_title,
            pr_url=row.pr_url,
            author=row.author,
            created_at=row.created_at,
            warnings=[
                WarningOut(
                    change_type=item.change_type,
                    symbol=item.symbol_ident,
                    detail=item.detail,
                    impacted=[
                        ImpactedOut(symbol=i.symbol_ident, path=i.path, line=i.line, type=i.ref_type)
                        for i in item.impacts
                    ],
                )
                for item in row.items
            ],
        )
        for row in rows
    ]
    return PrWarningListOut(items=items, page=page, per_page=per_page, total=total)
