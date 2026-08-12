"""인덱싱 실행. 레포 하나를 collecting → parsing → done으로 진행시킨다.

API 요청 스레드에서 돌리면 응답이 몇 분씩 늦어지므로 백그라운드로 뺀다.
대시보드는 /repos를 폴링해 indexing_status와 progress를 읽는다 (api-spec §3).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from src.db.models import Repo
from src.db.session import SessionLocal
from src.indexing.history import collect_repo_history

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
        repo = db.get(Repo, repo_id)
        if repo is None or repo.organization_id != organization_id:
            return

        try:
            collect_repo_history(db, repo, installation_id)

            # 파싱 단계(분석팀 #20·#21)가 들어오면 여기에 붙는다.
            # 아직 없으므로 수집이 끝나면 바로 완료로 넘긴다.
            repo.indexing_status = "done"
            repo.last_indexed_at = datetime.now(timezone.utc)
            repo.progress_current = None
            repo.progress_total = None
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
        db.commit()
    except Exception:
        logger.exception("실패 상태 기록마저 실패: repo_id=%s", repo_id)
