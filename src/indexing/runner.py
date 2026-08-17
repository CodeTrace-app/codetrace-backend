"""인덱싱 실행. 레포 하나를 collecting → parsing → done으로 진행시킨다.

API 요청 스레드에서 돌리면 응답이 몇 분씩 늦어지므로 백그라운드로 뺀다.
대시보드는 /repos를 폴링해 indexing_status와 progress를 읽는다 (api-spec §3).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from src.db.models import Repo
from src.db.session import SessionLocal
from src.indexing.history import collect_repo_history, generate_repo_summaries, link_symbol_evidence
from src.indexing.parsing import parse_repo

logger = logging.getLogger(__name__)

# 진행 중을 뜻하는 상태. 재인덱싱을 막는 기준이기도 하다.
IN_PROGRESS = ("collecting", "parsing")


def reset_stuck_indexing() -> int:
    """서버가 막 떴다면 진행 중인 인덱싱은 없다. 남아 있으면 이전 프로세스의 잔해다.

    인덱싱은 프로세스 안의 백그라운드 작업이라 배포·재시작이면 그대로 사라진다.
    그런데 상태는 collecting으로 남고, 재인덱싱은 "이미 진행 중"이라며 409로 막는다.
    되살릴 방법이 없어지므로 시작할 때 failed로 되돌려 재인덱싱 버튼이 살아나게 한다.
    """
    db = SessionLocal()
    try:
        stuck = list(db.scalars(select(Repo).where(Repo.indexing_status.in_(IN_PROGRESS))))
        for repo in stuck:
            repo.indexing_status = "failed"
            repo.progress_current = None
            repo.progress_total = None
            repo.progress_label = None
        if stuck:
            logger.warning("중단된 인덱싱 %d건을 failed로 되돌렸습니다", len(stuck))
            db.commit()
        return len(stuck)
    finally:
        db.close()


def run_indexing(repo_id: int, organization_id: int, installation_id: int) -> None:
    """레포 하나를 인덱싱한다. 백그라운드에서 호출된다.

    요청 세션은 응답과 함께 닫히므로 여기서 새 세션을 연다.
    """
    db = SessionLocal()
    try:
        try:
            repo = db.get(Repo, repo_id)
        except Exception:
            # 여기서 터지면(커넥션 풀 고갈 등) 상태가 collecting에 남고 재인덱싱은 409로 막힌다.
            logger.exception("인덱싱 시작 실패: repo_id=%s", repo_id)
            _mark_failed(db, repo_id)
            return
        if repo is None or repo.organization_id != organization_id:
            return

        try:
            # 1) 수집 — 클론, 커밋, PR·리뷰
            context = collect_repo_history(db, repo, installation_id)

            # 2) 파싱 — 파일·심볼·참조. 근거 연결은 심볼이 있어야 하므로 이 뒤에 온다.
            repo.indexing_status = "parsing"
            db.commit()
            parse_repo(db, repo, context.repo_dir)

            # 3) 근거 연결 — 심볼마다 git log -L로 커밋·PR을 붙인다.
            link_symbol_evidence(db, repo, context)

            # 4) 배경 요약 — 붙은 근거를 LLM으로 서술한다 (이슈 #28).
            #    근거가 있어야 하므로 3)보다 뒤여야 한다.
            generate_repo_summaries(db, repo)


            repo.indexing_status = "done"
            repo.last_indexed_at = datetime.now(timezone.utc)
            repo.progress_current = None
            repo.progress_total = None
            repo.progress_label = None
            db.commit()
        except Exception:
            # 실패해도 카드가 "수집 중"에 멈춰 있으면 안 된다.
            # failed면 프론트가 재인덱싱 버튼을 띄운다 (api-spec §9).
            logger.exception("인덱싱 실패: repo_id=%s", repo_id)
            _mark_failed(db, repo_id)
    finally:
        db.close()


def _mark_failed(db, repo_id: int) -> None:
    """실패를 기록한다. 여기서 또 실패해도 조용히 넘긴다.

    예외가 flush 중에 났다면 세션이 rollback을 기다리는 상태다. 그대로 commit하면
    PendingRollbackError가 나면서 failed 표시조차 못 남기고, 레포는 collecting에
    갇혀 재인덱싱(409)으로도 못 되살린다. 그래서 먼저 rollback하고 다시 읽는다.
    """
    try:
        db.rollback()
        repo = db.get(Repo, repo_id)
        if repo is None:
            return
        repo.indexing_status = "failed"
        repo.progress_current = None
        repo.progress_total = None
        repo.progress_label = None
        db.commit()
    except Exception:
        logger.exception("실패 상태 기록마저 실패: repo_id=%s", repo_id)
