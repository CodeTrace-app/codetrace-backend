"""인덱싱 상태 전이를 검사한다.

특히 재시작으로 끊긴 인덱싱이 되살아나는지를 본다. 여기가 막히면
레포가 "수집 중"에 영원히 멈추고 재인덱싱 버튼도 409로 거부된다.
"""

import pytest

from src.db.models import Organization, Repo
from src.indexing import runner


@pytest.fixture
def use_test_db(monkeypatch, db_session):
    """runner는 자기 세션을 여는데, 테스트에서는 테스트 세션을 쓰게 한다."""

    class _NoCloseSession:
        """runner가 close()해도 테스트가 이어서 쓸 수 있어야 한다."""

        def __getattr__(self, name):
            return getattr(db_session, name)

        def close(self):
            pass

    monkeypatch.setattr(runner, "SessionLocal", lambda: _NoCloseSession())
    return db_session


def _make_repo(db_session, status):
    org = db_session.query(Organization).first()
    if org is None:
        org = Organization(name="에이크미", slug="acme")
        db_session.add(org)
        db_session.flush()
    repo = Repo(
        organization_id=org.id,
        name="api",
        github_full_name=f"acme/{status}",
        indexing_status=status,
        progress_current=42,
        progress_total=199,
    )
    db_session.add(repo)
    db_session.commit()
    return repo


@pytest.mark.parametrize("status", ["collecting", "parsing"])
def test_재시작하면_중단된_인덱싱이_failed로_풀린다(use_test_db, status):
    repo = _make_repo(use_test_db, status)

    assert runner.reset_stuck_indexing() == 1

    use_test_db.refresh(repo)
    assert repo.indexing_status == "failed"
    assert repo.progress_current is None
    assert repo.progress_total is None


@pytest.mark.parametrize("status", ["done", "failed"])
def test_끝난_레포는_건드리지_않는다(use_test_db, status):
    repo = _make_repo(use_test_db, status)

    assert runner.reset_stuck_indexing() == 0

    use_test_db.refresh(repo)
    assert repo.indexing_status == status


def test_풀린_뒤에는_재인덱싱이_다시_허용된다(client, db_session, use_test_db):
    """#26의 409 가드가 영구 교착으로 굳지 않는지 확인한다."""
    from src.auth import create_token
    from src.db.models import User

    org = Organization(name="에이크미", slug="acme2", github_installation_id=1, github_account="acme")
    db_session.add(org)
    db_session.flush()
    admin = User(organization_id=org.id, email="a@acme.dev", password_hash="x", name="관리자", role="admin")
    db_session.add(admin)
    repo = Repo(organization_id=org.id, name="api", github_full_name="acme/api", indexing_status="collecting")
    db_session.add(repo)
    db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(admin)}"}

    # 끊긴 상태 그대로면 재인덱싱이 막혀 되살릴 수 없다.
    assert client.post(f"/api/v1/repos/{repo.id}/reindex", headers=headers).status_code == 409

    runner.reset_stuck_indexing()

    assert client.post(f"/api/v1/repos/{repo.id}/reindex", headers=headers).status_code == 202
