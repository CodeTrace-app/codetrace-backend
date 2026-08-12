"""레포 연동·인덱싱 API를 검사한다. docs/api-spec.md §3."""

import pytest

from src.api import repos as repos_router
from src.auth import create_token
from src.db.models import Organization, PullRequest, Repo, User


@pytest.fixture(autouse=True)
def scheduled_indexing(monkeypatch):
    """백그라운드 인덱싱을 실제로 돌리지 않고 호출만 기록한다.

    수집 파이프라인 자체는 test_history_collector.py가 검증한다.
    여기서 진짜로 돌리면 라우터 테스트가 git·GitHub API에 묶인다.
    """
    calls = []
    monkeypatch.setattr(repos_router, "run_indexing", lambda *args: calls.append(args))
    return calls


def _make_org_and_admin(db_session, *, plan="starter", installation_id=152864387, account="acme-payments"):
    org = Organization(
        name="에이크미", slug="acme", plan=plan, github_installation_id=installation_id, github_account=account
    )
    db_session.add(org)
    db_session.flush()
    admin = User(organization_id=org.id, email="admin@acme.dev", password_hash="x", name="관리자", role="admin")
    db_session.add(admin)
    db_session.commit()
    return org, admin


def _auth(user, read_only=False):
    return {"Authorization": f"Bearer {create_token(user, read_only=read_only)}"}


def test_레포가_없으면_빈_대시보드를_보여준다(client, db_session):
    _org, admin = _make_org_and_admin(db_session, installation_id=123, account="acme-payments")

    res = client.get("/api/v1/repos", headers=_auth(admin))

    assert res.status_code == 200
    body = res.json()
    assert body["summary"] == {
        "github_account": "acme-payments",
        "github_connected": True,
        "repo_count": 0,
        "commit_count": 0,
        "review_comment_count": 0,
        "last_indexed_at": None,
    }
    assert body["repos"] == []


def test_레포를_추가하면_collecting_상태로_시작한다(client, db_session):
    _org, admin = _make_org_and_admin(db_session)

    res = client.post("/api/v1/repos", json={"github_full_name": "acme-payments/api"}, headers=_auth(admin))

    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "api"
    assert body["github_full_name"] == "acme-payments/api"
    assert body["indexing_status"] == "collecting"
    assert body["progress"] is None
    assert body["stats"] == {"files": 0, "functions": 0, "commits": 0, "prs": 0}


def test_레포를_추가하면_인덱싱이_예약된다(client, db_session, scheduled_indexing):
    org, admin = _make_org_and_admin(db_session)

    res = client.post("/api/v1/repos", json={"github_full_name": "acme-payments/api"}, headers=_auth(admin))

    assert res.status_code == 201
    assert scheduled_indexing == [(res.json()["id"], org.id, org.github_installation_id)]


def test_GitHub_App_미설치면_레포를_추가할_수_없다(client, db_session, scheduled_indexing):
    _org, admin = _make_org_and_admin(db_session, installation_id=None, account=None)

    res = client.post("/api/v1/repos", json={"github_full_name": "acme-payments/api"}, headers=_auth(admin))

    assert res.status_code == 409
    assert scheduled_indexing == []


def test_이미_추가된_레포는_409(client, db_session):
    org, admin = _make_org_and_admin(db_session)
    db_session.add(Repo(organization_id=org.id, name="api", github_full_name="acme-payments/api"))
    db_session.commit()

    res = client.post("/api/v1/repos", json={"github_full_name": "acme-payments/api"}, headers=_auth(admin))

    assert res.status_code == 409


def test_플랜_레포_한도를_넘으면_403(client, db_session):
    org, admin = _make_org_and_admin(db_session, plan="starter")  # 한도 3개
    for i in range(3):
        db_session.add(Repo(organization_id=org.id, name=f"repo{i}", github_full_name=f"acme/repo{i}"))
    db_session.commit()

    res = client.post("/api/v1/repos", json={"github_full_name": "acme/repo-new"}, headers=_auth(admin))

    assert res.status_code == 403
    assert "Starter" in res.json()["detail"]
    assert "3" in res.json()["detail"]


def test_데모_세션은_레포를_추가할_수_없다(client, db_session):
    _org, admin = _make_org_and_admin(db_session)
    token = create_token(admin, read_only=True)

    res = client.post(
        "/api/v1/repos", json={"github_full_name": "acme/api"}, headers={"Authorization": f"Bearer {token}"}
    )

    assert res.status_code == 403


def test_재인덱싱하면_상태가_collecting으로_초기화된다(client, db_session, scheduled_indexing):
    org, admin = _make_org_and_admin(db_session)
    repo = Repo(
        organization_id=org.id,
        name="api",
        github_full_name="acme/api",
        indexing_status="done",
        progress_current=None,
        progress_total=None,
    )
    db_session.add(repo)
    db_session.commit()

    res = client.post(f"/api/v1/repos/{repo.id}/reindex", headers=_auth(admin))

    assert res.status_code == 202
    assert res.json() == {"id": repo.id, "indexing_status": "collecting"}
    assert scheduled_indexing == [(repo.id, org.id, org.github_installation_id)]


def test_이미_인덱싱_진행중이면_재인덱싱은_409(client, db_session):
    org, admin = _make_org_and_admin(db_session)
    repo = Repo(organization_id=org.id, name="api", github_full_name="acme/api", indexing_status="parsing")
    db_session.add(repo)
    db_session.commit()

    res = client.post(f"/api/v1/repos/{repo.id}/reindex", headers=_auth(admin))

    assert res.status_code == 409


def test_존재하지_않는_레포_재인덱싱은_404(client, db_session):
    _org, admin = _make_org_and_admin(db_session)

    res = client.post("/api/v1/repos/999/reindex", headers=_auth(admin))

    assert res.status_code == 404


def test_다른_조직의_레포는_보이지_않는다(client, db_session):
    org1, admin1 = _make_org_and_admin(db_session)
    org2 = Organization(name="다른회사", slug="other")
    db_session.add(org2)
    db_session.flush()
    other_repo = Repo(organization_id=org2.id, name="secret", github_full_name="other/secret")
    db_session.add(other_repo)
    db_session.commit()

    res = client.post(f"/api/v1/repos/{other_repo.id}/reindex", headers=_auth(admin1))

    assert res.status_code == 404


def test_대시보드_요약이_레포_통계를_합산한다(client, db_session):
    org, admin = _make_org_and_admin(db_session, installation_id=1, account="acme")
    repo_a = Repo(organization_id=org.id, name="a", github_full_name="acme/a", commits_count=10)
    db_session.add(repo_a)
    db_session.add(Repo(organization_id=org.id, name="b", github_full_name="acme/b", commits_count=5))
    db_session.flush()
    for number, comment_count in ((1, 3), (2, 4)):
        db_session.add(
            PullRequest(
                organization_id=org.id,
                repo_id=repo_a.id,
                number=number,
                title="pr",
                author="dev",
                url=f"https://github.com/acme/a/pull/{number}",
                review_excerpt="리뷰 코멘트",
                review_comment_count=comment_count,
            )
        )
    db_session.commit()

    res = client.get("/api/v1/repos", headers=_auth(admin))

    body = res.json()
    assert body["summary"]["repo_count"] == 2
    assert body["summary"]["commit_count"] == 15
    # PR 개수(2)가 아니라 코멘트 총합(3+4)이어야 한다.
    assert body["summary"]["review_comment_count"] == 7
