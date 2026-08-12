"""커밋·PR 이력 수집과 심볼 근거 연결 (이슈 #27).

제품의 차별점이 여기 걸려 있다. "코드가 왜 이렇게 작성되었는지"는 코드 파일에
없고 커밋 이력과 PR 리뷰에 흩어져 있으므로, 그것을 모아 심볼에 붙인다.

흐름:
    클론 → 커밋 수집 → PR·리뷰 수집 → 심볼별 git log -L로 근거 연결
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from src.config import settings
from src.db.models import Commit, PullRequest, Repo, Symbol, SymbolEvidence
from src.db.query import query
from src.github import git_history
from src.github.app_auth import get_installation_token
from src.github.client import get_repo, list_pr_comments, list_pr_commits, list_pull_requests

# "Merge pull request #12 from ..." — 머지 커밋이 어느 PR에서 왔는지 알려준다.
_MERGE_PR = re.compile(r"^Merge pull request #(\d+)\b")
# "fix: 토큰 검증에 알고리즘 화이트리스트 강제 (#201)" — squash 머지 관례.
_SQUASH_PR = re.compile(r"\(#(\d+)\)\s*$")

# 요약에 쓸 리뷰 코멘트는 가장 내용이 있는 것 하나만 남긴다.
# 봇이 남긴 한 줄("LGTM")까지 근거로 올리면 요약이 흐려진다.
_MIN_REVIEW_LENGTH = 20


def _clone_root() -> Path:
    return Path(settings.clone_root)


def _parse_github_time(value: str | None) -> datetime | None:
    """GitHub이 주는 "2026-01-02T00:00:00Z"를 datetime으로 바꾼다.

    문자열 그대로 DateTime 컬럼에 넣으면 저장 시점에 실패한다.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _pr_number_from_title(title: str) -> int | None:
    for pattern in (_MERGE_PR, _SQUASH_PR):
        found = pattern.search(title)
        if found:
            return int(found.group(1))
    return None


def _is_bot(comment: dict) -> bool:
    """CI 봇 코멘트인지 본다.

    커버리지 리포트 같은 봇 출력은 길이로 이기기 때문에, 길이만 보고 고르면
    맥락 패널(api-spec §4)에 사람의 논의 대신 봇 표가 실린다.
    """
    return (comment.get("user") or {}).get("type") == "Bot"


def _pick_review_excerpt(comments: list[dict]) -> str | None:
    """맥락 패널에 보여줄 리뷰 코멘트 하나를 고른다.

    사람이 쓴 것 중 가장 긴 코멘트를 쓴다. 짧은 코멘트는 "확인했습니다" 같은
    응답이라 코드가 왜 그렇게 됐는지를 설명하지 못한다.
    """
    bodies = [(c.get("body") or "").strip() for c in comments if not _is_bot(c)]
    usable = [b for b in bodies if len(b) >= _MIN_REVIEW_LENGTH]
    return max(usable, key=len) if usable else None


def _save_commits(db: Session, repo: Repo, commits: list[git_history.CommitInfo]) -> dict[str, Commit]:
    """커밋을 저장하고 sha → row 맵을 돌려준다. 재인덱싱이면 기존 것을 갱신한다."""
    existing = {c.sha: c for c in db.scalars(query(Commit, repo.organization_id).where(Commit.repo_id == repo.id))}

    repo.progress_current = 0
    repo.progress_total = len(commits)
    db.commit()

    saved: dict[str, Commit] = {}
    for index, info in enumerate(commits, start=1):
        row = existing.get(info.sha)
        if row is None:
            row = Commit(
                organization_id=repo.organization_id,
                repo_id=repo.id,
                sha=info.sha,
                title=info.title[:500],
                body=info.body or None,
                author=info.author[:100],
                committed_at=info.committed_at,
                url=f"https://github.com/{repo.github_full_name}/commit/{info.sha}",
            )
            db.add(row)
        saved[info.sha] = row

        # 대시보드가 5초마다 폴링하므로 중간 진행률이 보여야 한다.
        # 커밋마다 commit()하면 수백 번 왕복하므로 일정 간격으로만 반영한다.
        if index % 50 == 0:
            repo.progress_current = index
            db.commit()

    repo.progress_current = len(commits)
    repo.commits_count = len(saved)
    db.commit()
    return saved


def _save_pull_requests(
    db: Session, repo: Repo, installation_id: int
) -> tuple[dict[int, PullRequest], dict[str, PullRequest]]:
    """PR과 리뷰 코멘트를 저장한다.

    번호→PR 맵과 함께 sha→PR 맵도 돌려준다. 후자는 커밋이 어느 PR에 속하는지를
    PR의 커밋 목록으로 확정한 것이라, 커밋 제목 관례에 기대지 않는다.
    """
    existing = {
        p.number: p
        for p in db.scalars(query(PullRequest, repo.organization_id).where(PullRequest.repo_id == repo.id))
    }

    payloads = list_pull_requests(installation_id, repo.github_full_name)
    repo.progress_current = 0
    repo.progress_total = len(payloads)
    db.commit()

    saved: dict[int, PullRequest] = {}
    pr_by_sha: dict[str, PullRequest] = {}
    for index, payload in enumerate(payloads, start=1):
        number = payload["number"]
        comments = list_pr_comments(installation_id, repo.github_full_name, number)
        excerpt = _pick_review_excerpt(comments)

        row = existing.get(number)
        if row is None:
            row = PullRequest(
                organization_id=repo.organization_id,
                repo_id=repo.id,
                number=number,
                url=payload["html_url"],
            )
            db.add(row)
        # 제목·본문은 나중에 수정될 수 있으므로 재수집 때도 갱신한다.
        row.title = payload["title"][:500]
        row.body = payload.get("body") or None
        row.author = (payload.get("user") or {}).get("login", "unknown")[:100]
        row.merged_at = _parse_github_time(payload.get("merged_at"))
        row.review_excerpt = excerpt
        row.review_comment_count = len(comments)
        saved[number] = row

        for commit in list_pr_commits(installation_id, repo.github_full_name, number):
            pr_by_sha[commit["sha"]] = row

        # PR 단계도 커밋 단계만큼 오래 걸린다(PR당 API 3회). 화면이 멈춰 보이면 안 된다.
        repo.progress_current = index
        db.commit()

    repo.prs_count = len(saved)
    db.commit()
    return saved, pr_by_sha


def _find_pull_request(
    commit: Commit, pr_by_sha: dict[str, PullRequest], prs_by_number: dict[int, PullRequest]
) -> PullRequest | None:
    """커밋이 속한 PR을 찾는다.

    PR의 커밋 목록으로 확정하는 쪽을 먼저 본다. 커밋 제목의 "(#12)" 표기는
    그 번호의 PR이 이 레포에 실제로 있을 때만 쓴다 — 데모 레포처럼 과거 이력을
    흉내 낸 표기(#201 등)는 존재하지 않는 PR을 가리킨다.
    """
    found = pr_by_sha.get(commit.sha)
    if found is not None:
        return found
    number = _pr_number_from_title(commit.title)
    return prs_by_number.get(number) if number else None


def _link_symbol_evidence(
    db: Session,
    repo: Repo,
    repo_dir: Path,
    commits_by_sha: dict[str, Commit],
    prs_by_number: dict[int, PullRequest],
    pr_by_sha: dict[str, PullRequest],
) -> int:
    """심볼마다 git log -L로 근거 커밋을 찾아 연결한다.

    심볼은 분석팀 파서가 채운다(#20). 아직 없으면 연결할 대상이 없어 0을 돌려준다.
    """
    symbols = list(db.scalars(query(Symbol, repo.organization_id).where(Symbol.repo_id == repo.id)))
    if not symbols:
        return 0

    # 재인덱싱은 전체 재수집이므로 이전 근거를 지우고 다시 만든다.
    for stale in db.scalars(query(SymbolEvidence, repo.organization_id).where(SymbolEvidence.repo_id == repo.id)):
        db.delete(stale)
    db.flush()

    repo.progress_current = 0
    repo.progress_total = len(symbols)
    db.commit()

    linked = 0
    for index, symbol in enumerate(symbols, start=1):
        shas = git_history.line_history(repo_dir, symbol.path, symbol.start_line, symbol.end_line)
        seen_prs: set[int] = set()

        for sha in shas:
            commit = commits_by_sha.get(sha)
            if commit is None:
                continue
            db.add(
                SymbolEvidence(
                    organization_id=repo.organization_id,
                    repo_id=repo.id,
                    symbol_ident=symbol.ident,
                    kind="commit",
                    commit_id=commit.id,
                )
            )
            linked += 1

            # 커밋이 속한 PR도 근거가 된다. 리뷰 스레드에 결정 이유가 남기 때문이다.
            pr = _find_pull_request(commit, pr_by_sha, prs_by_number)
            if pr is not None and pr.number not in seen_prs:
                seen_prs.add(pr.number)
                db.add(
                    SymbolEvidence(
                        organization_id=repo.organization_id,
                        repo_id=repo.id,
                        symbol_ident=symbol.ident,
                        kind="pr",
                        pull_request_id=pr.id,
                    )
                )
                linked += 1

        # 심볼당 git log -L 한 번이라 심볼이 많으면 이 단계가 가장 오래 걸린다.
        if index % 20 == 0:
            repo.progress_current = index
            db.commit()

    repo.progress_current = len(symbols)
    db.commit()
    return linked


def _save_repo_metadata(db: Session, repo: Repo, installation_id: int) -> None:
    """대표 언어와 기본 브랜치를 채운다.

    둘 다 대시보드 카드에 그대로 표시된다(api-spec §3). 클론만으로는 알 수 없어
    API로 받는다. 등록 시점에는 사용자가 레포 이름만 주므로 여기가 채울 유일한 지점이다.
    """
    info = get_repo(installation_id, repo.github_full_name)
    repo.language = info.get("language")
    repo.default_branch = info.get("default_branch") or repo.default_branch
    db.commit()


@dataclass
class HistoryContext:
    """수집 결과. 파싱이 끝난 뒤 근거를 연결할 때 다시 쓴다.

    근거 연결은 심볼이 있어야 하므로 수집 단계에서 바로 할 수 없다.
    호출자(run_indexing)가 파싱을 끼워 넣고 이 값을 link_symbol_evidence로 넘긴다.
    """

    repo_dir: Path
    commits_by_sha: dict[str, Commit]
    prs_by_number: dict[int, PullRequest]
    pr_by_sha: dict[str, PullRequest]


def collect_repo_history(db: Session, repo: Repo, installation_id: int) -> HistoryContext:
    """레포 하나의 이력을 수집한다. 상태 전이는 호출자(run_indexing)가 맡는다."""
    token = get_installation_token(installation_id)
    repo_dir = _clone_root() / str(repo.organization_id) / repo.github_full_name.replace("/", "__")
    git_history.ensure_clone(repo.github_full_name, token, repo_dir)

    _save_repo_metadata(db, repo, installation_id)

    commits_by_sha = _save_commits(db, repo, git_history.list_commits(repo_dir))
    prs_by_number, pr_by_sha = _save_pull_requests(db, repo, installation_id)
    return HistoryContext(repo_dir, commits_by_sha, prs_by_number, pr_by_sha)


def link_symbol_evidence(db: Session, repo: Repo, context: HistoryContext) -> int:
    """파싱으로 심볼이 만들어진 뒤, 심볼마다 근거 커밋·PR을 연결한다."""
    return _link_symbol_evidence(
        db,
        repo,
        context.repo_dir,
        context.commits_by_sha,
        context.prs_by_number,
        context.pr_by_sha,
    )
