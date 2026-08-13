"""맥락 API를 검사한다. docs/api-spec.md §4."""

from datetime import datetime, timezone

import pytest

from src.auth import create_token
from src.db.models import (
    Commit,
    Organization,
    PullRequest,
    QueryLog,
    Repo,
    Symbol,
    SymbolEvidence,
    SymbolSummary,
    User,
)


@pytest.fixture
def setup(db_session):
    org = Organization(name="에이크미", slug="acme")
    db_session.add(org)
    db_session.flush()
    user = User(organization_id=org.id, email="a@acme.dev", password_hash="x", name="김신입", role="member")
    db_session.add(user)
    repo = Repo(organization_id=org.id, name="api", github_full_name="acme/api")
    db_session.add(repo)
    db_session.commit()
    return org, user, repo


def _auth(user, read_only=False):
    return {"Authorization": f"Bearer {create_token(user, read_only=read_only)}"}


def _add_symbol(db_session, repo, *, ident, name, path, start, end):
    row = Symbol(
        organization_id=repo.organization_id,
        repo_id=repo.id,
        ident=ident,
        name=name,
        path=path,
        kind="function",
        start_line=start,
        end_line=end,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _add_commit_evidence(db_session, repo, ident, *, title, sha="a" * 40, when=None):
    commit = Commit(
        organization_id=repo.organization_id,
        repo_id=repo.id,
        sha=sha,
        title=title,
        author="kimdev",
        committed_at=when or datetime(2023, 3, 1, tzinfo=timezone.utc),
        url=f"https://github.com/acme/api/commit/{sha}",
    )
    db_session.add(commit)
    db_session.flush()
    db_session.add(
        SymbolEvidence(
            organization_id=repo.organization_id,
            repo_id=repo.id,
            symbol_ident=ident,
            kind="commit",
            commit_id=commit.id,
        )
    )
    db_session.commit()
    return commit


def test_근거와_요약이_있으면_ok로_응답한다(client, db_session, setup):
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/auth.py::verify_token", name="verify_token", path="src/auth.py", start=30, end=32)
    _add_commit_evidence(db_session, repo, "src/auth.py::verify_token", title="fix: 알고리즘 화이트리스트 강제")
    db_session.add(
        SymbolSummary(
            organization_id=repo.organization_id,
            repo_id=repo.id,
            symbol_ident="src/auth.py::verify_token",
            status="ok",
            summary="보안 감사 지적에 따라 허용 알고리즘을 고정했다.",
        )
    )
    db_session.commit()

    res = client.get(f"/api/v1/repos/{repo.id}/context", params={"path": "src/auth.py", "line": 31}, headers=_auth(user))

    assert res.status_code == 200
    body = res.json()
    assert body["function"] == {"name": "verify_token", "path": "src/auth.py", "start_line": 30, "end_line": 32}
    assert body["status"] == "ok"
    assert "보안 감사" in body["summary"]
    assert body["evidence"][0]["kind"] == "commit"
    assert body["evidence"][0]["sha"] == "a" * 40
    assert body["evidence_truncated"] is False
    assert body["parent_module"] is None


def test_근거가_없으면_no_history와_이동경로를_준다(client, db_session, setup):
    """빈 화면 금지 (S-UBXNLW)."""
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/utils.py::format_krw", name="format_krw", path="src/utils.py", start=3, end=6)

    res = client.get(f"/api/v1/repos/{repo.id}/context", params={"path": "src/utils.py", "line": 4}, headers=_auth(user))

    body = res.json()
    assert body["status"] == "no_history"
    assert body["summary"] is None
    assert body["evidence"] == []
    assert body["parent_module"] == {"path": "src", "name": "src"}


def test_가장_안쪽_함수로_해석한다(client, db_session, setup):
    """중첩된 심볼이 같은 줄을 포함하면 범위가 좁은 쪽이 사용자가 클릭한 대상이다 (S-ZEZFED)."""
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/a.py::Outer", name="Outer", path="src/a.py", start=1, end=100)
    _add_symbol(db_session, repo, ident="src/a.py::Outer.inner", name="inner", path="src/a.py", start=40, end=50)

    res = client.get(f"/api/v1/repos/{repo.id}/context", params={"path": "src/a.py", "line": 45}, headers=_auth(user))

    assert res.json()["function"]["name"] == "inner"


def test_함수_밖이면_파일_단위_맥락으로_응답한다(client, db_session, setup):
    """404가 아니다 (api-spec §4)."""
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/a.py::f", name="f", path="src/a.py", start=40, end=50)
    _add_commit_evidence(db_session, repo, "src/a.py::f", title="feat: f 추가")

    res = client.get(f"/api/v1/repos/{repo.id}/context", params={"path": "src/a.py", "line": 5}, headers=_auth(user))

    assert res.status_code == 200
    body = res.json()
    assert body["function"]["name"] == "a.py"
    assert len(body["evidence"]) == 1


def test_요약이_아직_없어도_근거가_있으면_no_history로_왜곡하지_않는다(client, db_session, setup):
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/a.py::f", name="f", path="src/a.py", start=1, end=5)
    _add_commit_evidence(db_session, repo, "src/a.py::f", title="feat: f 추가")

    body = client.get(
        f"/api/v1/repos/{repo.id}/context", params={"path": "src/a.py", "line": 2}, headers=_auth(user)
    ).json()

    assert body["status"] == "ok"
    assert body["summary"] is None
    assert len(body["evidence"]) == 1


def test_근거가_12건을_넘으면_잘리고_플래그가_선다(client, db_session, setup):
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/a.py::f", name="f", path="src/a.py", start=1, end=5)
    for i in range(15):
        _add_commit_evidence(
            db_session, repo, "src/a.py::f", title=f"커밋 {i}", sha=f"{i:040d}",
            when=datetime(2023, 1, 1 + i, tzinfo=timezone.utc),
        )

    body = client.get(
        f"/api/v1/repos/{repo.id}/context", params={"path": "src/a.py", "line": 2}, headers=_auth(user)
    ).json()

    assert len(body["evidence"]) == 12
    assert body["evidence_truncated"] is True
    # 최신순으로 자른다
    assert body["evidence"][0]["title"] == "커밋 14"


def test_미병합_PR이_최신_커밋을_밀어내지_않는다(client, db_session, setup):
    """merged_at이 null인 PR을 '가장 최신'으로 오해하면 진짜 최근 커밋이 전부 잘려나간다."""
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/a.py::f", name="f", path="src/a.py", start=1, end=5)

    for i in range(12):
        pr = PullRequest(
            organization_id=repo.organization_id,
            repo_id=repo.id,
            number=100 + i,
            title=f"미병합 PR {i}",
            author="dev",
            merged_at=None,
            url=f"https://github.com/acme/api/pull/{100 + i}",
        )
        db_session.add(pr)
        db_session.flush()
        db_session.add(
            SymbolEvidence(
                organization_id=repo.organization_id,
                repo_id=repo.id,
                symbol_ident="src/a.py::f",
                kind="pr",
                pull_request_id=pr.id,
            )
        )
    for i in range(3):
        _add_commit_evidence(
            db_session, repo, "src/a.py::f", title=f"진짜 최신 커밋 {i}", sha=f"{i:040d}",
            when=datetime(2025, 6, 1 + i, tzinfo=timezone.utc),
        )
    db_session.commit()

    body = client.get(
        f"/api/v1/repos/{repo.id}/context", params={"path": "src/a.py", "line": 2}, headers=_auth(user)
    ).json()

    titles = [e["title"] for e in body["evidence"]]
    assert sum(1 for t in titles if t.startswith("진짜 최신 커밋")) == 3, titles


def test_요약이_인용한_근거는_반드시_목록에_있다(client, db_session, setup):
    """요약은 근거에 적힌 것만 썼다고 주장한다. 그 근거가 목록에 없으면 주장이 깨진다."""
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/a.py::f", name="f", path="src/a.py", start=1, end=5)

    # 잡음 커밋을 잔뜩 넣어 의미 있는 커밋을 밀어낼 수 있는 상황을 만든다.
    for i in range(12):
        _add_commit_evidence(
            db_session, repo, "src/a.py::f", title="chore: 주석 보완", sha=f"{i:040d}",
            when=datetime(2025, 1, 1 + i, tzinfo=timezone.utc),
        )
    _add_commit_evidence(
        db_session, repo, "src/a.py::f", title="fix: 실제 의미있는 변경", sha="f" * 40,
        when=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    db_session.commit()

    body = client.get(
        f"/api/v1/repos/{repo.id}/context", params={"path": "src/a.py", "line": 2}, headers=_auth(user)
    ).json()

    titles = [e["title"] for e in body["evidence"]]
    assert "fix: 실제 의미있는 변경" in titles, "요약에 쓰일 근거가 잡음에 밀려 사라졌다"


def test_PR_근거는_커밋과_필드가_다르다(client, db_session, setup):
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/a.py::f", name="f", path="src/a.py", start=1, end=5)
    pr = PullRequest(
        organization_id=repo.organization_id,
        repo_id=repo.id,
        number=7,
        title="feat: 리포트 CSV 내보내기",
        author="kimdev",
        merged_at=datetime(2023, 5, 1, tzinfo=timezone.utc),
        url="https://github.com/acme/api/pull/7",
        review_excerpt="엑셀에서 한글이 깨질 수 있습니다.",
    )
    db_session.add(pr)
    db_session.flush()
    db_session.add(
        SymbolEvidence(
            organization_id=repo.organization_id,
            repo_id=repo.id,
            symbol_ident="src/a.py::f",
            kind="pr",
            pull_request_id=pr.id,
        )
    )
    db_session.commit()

    item = client.get(
        f"/api/v1/repos/{repo.id}/context", params={"path": "src/a.py", "line": 2}, headers=_auth(user)
    ).json()["evidence"][0]

    assert item["kind"] == "pr"
    assert item["number"] == 7
    assert item["review_excerpt"] == "엑셀에서 한글이 깨질 수 있습니다."
    assert "author" not in item, "명세의 pr 항목에는 author가 없다"
    assert "sha" not in item


def test_데모_세션도_맥락을_읽을_수_있다(client, db_session, setup):
    """/context에는 데모 차단 표시가 없다. 막으면 시연이 깨진다."""
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/a.py::f", name="f", path="src/a.py", start=1, end=5)

    res = client.get(
        f"/api/v1/repos/{repo.id}/context",
        params={"path": "src/a.py", "line": 2},
        headers=_auth(user, read_only=True),
    )

    assert res.status_code == 200


def test_다른_조직의_레포는_404(client, db_session, setup):
    _org, user, _repo = setup
    other = Organization(name="다른회사", slug="other")
    db_session.add(other)
    db_session.flush()
    other_repo = Repo(organization_id=other.id, name="secret", github_full_name="other/secret")
    db_session.add(other_repo)
    db_session.commit()

    res = client.get(
        f"/api/v1/repos/{other_repo.id}/context", params={"path": "src/a.py", "line": 1}, headers=_auth(user)
    )

    assert res.status_code == 404


def test_조회하면_질의_이력이_남는다(client, db_session, setup):
    """api-spec §6의 관리자 화면이 이걸 읽는다."""
    _org, user, repo = setup
    _add_symbol(db_session, repo, ident="src/auth.py::verify_token", name="verify_token", path="src/auth.py", start=30, end=32)

    client.get(
        f"/api/v1/repos/{repo.id}/context", params={"path": "src/auth.py", "line": 31}, headers=_auth(user)
    )

    log = db_session.query(QueryLog).one()
    assert log.action == "context_view"
    assert log.target == "src/auth.py::verify_token"
    assert log.user_name == "김신입"
    assert log.repo_name == "api"


def test_인증이_없으면_401(client, db_session, setup):
    _org, _user, repo = setup

    res = client.get(f"/api/v1/repos/{repo.id}/context", params={"path": "src/a.py", "line": 1})

    assert res.status_code == 401
