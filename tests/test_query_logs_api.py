"""질의 이력 조회 API를 검사한다. docs/api-spec.md §6, 이슈 #30."""

from datetime import datetime, timedelta, timezone

from src.api.admin import cleanup_old_query_logs
from src.auth import create_token
from src.db.models import Organization, QueryLog, Repo, User

BASE = "/api/v1"


def _auth(user, read_only=False):
    return {"Authorization": f"Bearer {create_token(user, read_only=read_only)}"}


def _make_org(db_session, *, name="에이크미", slug="acme"):
    org = Organization(name=name, slug=slug)
    db_session.add(org)
    db_session.flush()
    return org


def _make_repo(db_session, org, *, name="api", full_name="acme/api"):
    repo = Repo(organization_id=org.id, name=name, github_full_name=full_name)
    db_session.add(repo)
    db_session.flush()
    return repo


def _make_user(db_session, org, *, email="user@acme.dev", name="사용자", role="admin"):
    user = User(organization_id=org.id, email=email, password_hash="x", name=name, role=role)
    db_session.add(user)
    db_session.flush()
    return user


def _add_log(db_session, org, repo, user, *, action="context_view", target="src/a.py::f", created_at=None):
    row = QueryLog(
        organization_id=org.id,
        user_id=user.id,
        user_name=user.name,
        repo_id=repo.id,
        repo_name=repo.name,
        action=action,
        target=target,
    )
    db_session.add(row)
    db_session.flush()
    if created_at is not None:
        row.created_at = created_at
    return row


def test_이력이_조회된다(client, db_session):
    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    admin = _make_user(db_session, org, role="admin")
    _add_log(db_session, org, repo, admin, target="src/payment.py::process_payment")
    db_session.commit()

    res = client.get(f"{BASE}/admin/query-logs", headers=_auth(admin))

    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["per_page"] == 20
    item = body["items"][0]
    assert item["user_name"] == admin.name
    assert item["action"] == "context_view"
    assert item["repo"] == repo.name
    assert item["target"] == "src/payment.py::process_payment"


def test_member는_조회할_수_없다(client, db_session):
    org = _make_org(db_session)
    member = _make_user(db_session, org, role="member")
    db_session.commit()

    res = client.get(f"{BASE}/admin/query-logs", headers=_auth(member))

    assert res.status_code == 403


def test_읽기_전용_관리자도_조회할_수_있다(client, db_session):
    """조회에는 쓰기 권한을 요구하지 않는다. 역할과 조직 격리만 본다."""
    org = _make_org(db_session)
    admin = _make_user(db_session, org, role="admin")
    db_session.commit()

    res = client.get(f"{BASE}/admin/query-logs", headers=_auth(admin, read_only=True))

    assert res.status_code == 200


def test_토큰_없이는_조회할_수_없다(client):
    assert client.get(f"{BASE}/admin/query-logs").status_code == 401


def test_다른_조직의_이력은_보이지_않는다(client, db_session):
    org_a = _make_org(db_session, name="에이크미", slug="acme")
    repo_a = _make_repo(db_session, org_a)
    admin_a = _make_user(db_session, org_a, email="a@acme.dev", role="admin")
    _add_log(db_session, org_a, repo_a, admin_a)

    org_b = _make_org(db_session, name="다른회사", slug="other")
    admin_b = _make_user(db_session, org_b, email="b@other.dev", role="admin")
    db_session.commit()

    res = client.get(f"{BASE}/admin/query-logs", headers=_auth(admin_b))

    assert res.status_code == 200
    assert res.json()["items"] == []


def test_페이지네이션(client, db_session):
    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    admin = _make_user(db_session, org, role="admin")
    for i in range(25):
        _add_log(db_session, org, repo, admin, target=f"src/a.py::f{i}")
    db_session.commit()

    page1 = client.get(f"{BASE}/admin/query-logs?page=1", headers=_auth(admin)).json()
    page2 = client.get(f"{BASE}/admin/query-logs?page=2", headers=_auth(admin)).json()

    assert page1["total"] == 25
    assert len(page1["items"]) == 20
    assert len(page2["items"]) == 5
    assert {item["target"] for item in page1["items"]} & {item["target"] for item in page2["items"]} == set()


def test_context_조회하면_이력이_남는다(client, db_session):
    """맥락 API를 실제로 호출해 record_query_log가 남긴 행이 조회에 나오는지 끝까지 확인한다."""
    from src.db.models import Commit, Symbol, SymbolEvidence

    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    admin = _make_user(db_session, org, role="admin")
    symbol = Symbol(
        organization_id=org.id, repo_id=repo.id, ident="src/a.py::f", name="f", path="src/a.py",
        kind="function", start_line=1, end_line=3,
    )
    db_session.add(symbol)
    commit = Commit(
        organization_id=org.id, repo_id=repo.id, sha="a" * 40, title="init", author="kim",
        committed_at=datetime.now(timezone.utc), url="https://github.com/acme/api/commit/" + "a" * 40,
    )
    db_session.add(commit)
    db_session.flush()
    db_session.add(
        SymbolEvidence(
            organization_id=org.id, repo_id=repo.id, symbol_ident=symbol.ident, kind="commit", commit_id=commit.id
        )
    )
    db_session.commit()

    ctx_res = client.get(f"{BASE}/repos/{repo.id}/context?path=src/a.py&line=2", headers=_auth(admin))
    assert ctx_res.status_code == 200

    res = client.get(f"{BASE}/admin/query-logs", headers=_auth(admin))
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "context_view"
    assert body["items"][0]["target"] == "src/a.py::f"


# ---------------------------------------------------------------- 90일 보존


def test_90일_지난_이력은_삭제된다(db_session, monkeypatch):
    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    admin = _make_user(db_session, org, role="admin")
    old = _add_log(db_session, org, repo, admin, target="old")
    recent = _add_log(db_session, org, repo, admin, target="recent")
    db_session.commit()

    old.created_at = datetime.now(timezone.utc) - timedelta(days=91)
    recent.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.commit()

    monkeypatch.setattr("src.api.admin.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)  # conftest가 테스트 끝에 또 닫으므로 여기선 막는다

    deleted = cleanup_old_query_logs()

    assert deleted == 1
    remaining = db_session.query(QueryLog).all()
    assert len(remaining) == 1
    assert remaining[0].target == "recent"


def test_90일이_안된_이력은_남는다(db_session, monkeypatch):
    org = _make_org(db_session)
    repo = _make_repo(db_session, org)
    admin = _make_user(db_session, org, role="admin")
    _add_log(db_session, org, repo, admin, target="fresh")
    db_session.commit()

    monkeypatch.setattr("src.api.admin.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    deleted = cleanup_old_query_logs()

    assert deleted == 0
    assert db_session.query(QueryLog).count() == 1
