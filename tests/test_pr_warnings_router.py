"""PR 웹훅과 경고 이력 API를 검사한다. docs/api-spec.md §5·§8, 이슈 #29."""

import hashlib
import hmac
import json

from src.api import pr_warnings as pr_warnings_router
from src.auth import create_token
from src.config import settings
from src.db.models import Organization, PrWarning, Reference, Repo, User

_SECRET = "test-webhook-secret"

_BASE_SOURCE = "def verify_token(token):\n    return token\n"
_HEAD_SOURCE = "def verify_token(token, verify_exp):\n    return token\n"


def _make_org_and_repo(db_session, *, installation_id=152864387, full_name="acme-payments/api"):
    org = Organization(name="에이크미", slug="acme", github_installation_id=installation_id, github_account="acme-payments")
    db_session.add(org)
    db_session.flush()
    repo = Repo(organization_id=org.id, name=full_name.split("/")[-1], github_full_name=full_name)
    db_session.add(repo)
    admin = User(organization_id=org.id, email="admin@acme.dev", password_hash="x", name="관리자", role="admin")
    db_session.add(admin)
    db_session.commit()
    return org, repo, admin


def _auth(user):
    return {"Authorization": f"Bearer {create_token(user)}"}


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pr_payload(*, installation_id, full_name, number=12, action="opened", base_sha="base123", head_sha="head456"):
    return {
        "action": action,
        "installation": {"id": installation_id},
        "repository": {"full_name": full_name},
        "pull_request": {
            "number": number,
            "title": "토큰 검증에 만료 검사 옵션 추가",
            "html_url": f"https://github.com/{full_name}/pull/{number}",
            "user": {"login": "kimnewbie"},
            "base": {"sha": base_sha},
            "head": {"sha": head_sha},
        },
    }


def _send(client, payload, *, secret=_SECRET, event="pull_request", signature=None):
    body = json.dumps(payload).encode()
    sig = signature if signature is not None else _signature(secret, body)
    headers = {"Content-Type": "application/json"}
    if event is not None:
        headers["X-GitHub-Event"] = event
    if sig is not None:
        headers["X-Hub-Signature-256"] = sig
    return client.post("/api/v1/webhooks/github", content=body, headers=headers)


def _mock_pr_change(monkeypatch, *, files=None, sources=None):
    """list_pr_files·get_file_content·코멘트 함수를 가짜로 바꾼다.

    실제 GitHub API·네트워크 없이 웹훅 처리 경로 전체(서명 검증 이후)를 검사한다.
    돌려주는 created·updated 리스트에 코멘트 작성·수정 호출이 그대로 기록된다.
    """
    monkeypatch.setattr(settings, "github_webhook_secret", _SECRET)

    files = files if files is not None else [{"filename": "src/auth.py", "status": "modified"}]
    monkeypatch.setattr(pr_warnings_router, "list_pr_files", lambda *a, **k: files)

    sources = sources or {}
    monkeypatch.setattr(pr_warnings_router, "get_file_content", lambda inst, full, path, ref: sources.get(ref))

    created: list[dict] = []

    def fake_create(installation_id, full_name, number, body):
        created.append({"installation_id": installation_id, "full_name": full_name, "number": number, "body": body})
        return 999

    monkeypatch.setattr(pr_warnings_router, "create_issue_comment", fake_create)

    updated: list[dict] = []

    def fake_update(installation_id, full_name, comment_id, body):
        updated.append({"comment_id": comment_id, "body": body})
        return True

    monkeypatch.setattr(pr_warnings_router, "update_issue_comment", fake_update)

    return created, updated


# ---------------------------------------------------------------- 서명 검증


def test_서명이_없으면_401(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session)
    monkeypatch.setattr(settings, "github_webhook_secret", _SECRET)
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)

    res = _send(client, payload, signature="")

    assert res.status_code == 401


def test_서명이_틀리면_401(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session)
    monkeypatch.setattr(settings, "github_webhook_secret", _SECRET)
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)

    res = _send(client, payload, secret="다른-시크릿")

    assert res.status_code == 401


def test_웹훅_시크릿이_설정되지_않았으면_401(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session)
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)

    res = _send(client, payload, secret="아무거나")

    assert res.status_code == 401


# ---------------------------------------------------------------- 이벤트·액션 필터링


def test_pull_request_이외_이벤트는_무시한다(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session)
    created, _updated = _mock_pr_change(monkeypatch)
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)

    res = _send(client, payload, event="issues")

    assert res.status_code == 204
    assert created == []


def test_opened_synchronize_이외_액션은_무시한다(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session)
    created, _updated = _mock_pr_change(monkeypatch)
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name, action="closed")

    res = _send(client, payload)

    assert res.status_code == 204
    assert created == []


def test_연결되지_않은_installation은_무시한다(client, db_session, monkeypatch):
    _org, repo, _admin = _make_org_and_repo(db_session)
    created, _updated = _mock_pr_change(monkeypatch)
    payload = _pr_payload(installation_id=999999, full_name=repo.github_full_name)

    res = _send(client, payload)

    assert res.status_code == 204
    assert created == []


def test_등록되지_않은_레포는_무시한다(client, db_session, monkeypatch):
    org, _repo, _admin = _make_org_and_repo(db_session)
    created, _updated = _mock_pr_change(monkeypatch)
    payload = _pr_payload(installation_id=org.github_installation_id, full_name="acme-payments/다른레포")

    res = _send(client, payload)

    assert res.status_code == 204
    assert created == []


# ---------------------------------------------------------------- 판별·저장·코멘트


def test_시그니처_변경을_판별해_경고를_저장하고_코멘트를_단다(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session)
    db_session.add(
        Reference(
            organization_id=org.id,
            repo_id=repo.id,
            source_ident="src/middleware.py::require_login",
            target_ident="src/auth.py::verify_token",
            ref_type="call",
            path="src/middleware.py",
            line=17,
        )
    )
    db_session.commit()
    created, _updated = _mock_pr_change(
        monkeypatch, sources={"base123": _BASE_SOURCE, "head456": _HEAD_SOURCE}
    )
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)

    res = _send(client, payload)

    assert res.status_code == 204

    row = db_session.query(PrWarning).filter_by(repo_id=repo.id, pr_number=12).one()
    assert row.pr_title == "토큰 검증에 만료 검사 옵션 추가"
    assert row.head_sha == "head456"
    assert row.github_comment_id == 999
    assert len(row.items) == 1
    item = row.items[0]
    assert item.change_type == "signature_changed"
    assert item.symbol_ident == "src/auth.py::verify_token"
    assert "(token)에서 (token, verify_exp)로" in item.detail
    assert len(item.impacts) == 1
    assert item.impacts[0].symbol_ident == "src/middleware.py::require_login"

    assert len(created) == 1
    body = created[0]["body"]
    assert "signature_changed" not in body or "시그니처 변경" in body  # 사람이 읽을 라벨을 쓴다
    assert "src/auth.py::verify_token" in body
    assert f"repo={repo.id}" in body
    assert "tab=impact" in body


def test_경고가_없으면_아무것도_남기지_않는다(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session)
    created, updated = _mock_pr_change(
        monkeypatch, sources={"base123": _BASE_SOURCE, "head456": _BASE_SOURCE}
    )
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)

    res = _send(client, payload)

    assert res.status_code == 204
    assert created == []
    assert updated == []
    assert db_session.query(PrWarning).count() == 0


def test_재검사시_새_코멘트_대신_기존_코멘트를_고친다(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session)
    created, _updated = _mock_pr_change(
        monkeypatch, sources={"base123": _BASE_SOURCE, "head456": _HEAD_SOURCE}
    )
    opened = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name, action="opened")
    assert _send(client, opened).status_code == 204
    assert len(created) == 1

    created2, updated2 = _mock_pr_change(
        monkeypatch, sources={"base123": _BASE_SOURCE, "head456": _HEAD_SOURCE}
    )
    synchronize = _pr_payload(
        installation_id=org.github_installation_id, full_name=repo.github_full_name,
        action="synchronize", head_sha="head789",
    )

    res = _send(client, synchronize)

    assert res.status_code == 204
    assert created2 == []
    assert len(updated2) == 1
    assert updated2[0]["comment_id"] == 999

    row = db_session.query(PrWarning).filter_by(repo_id=repo.id, pr_number=12).one()
    assert row.head_sha == "head789"
    assert row.github_comment_id == 999
    assert len(row.items) == 1


def test_경고가_해소되면_코멘트를_고치고_이력에서_지운다(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session)
    _mock_pr_change(monkeypatch, sources={"base123": _BASE_SOURCE, "head456": _HEAD_SOURCE})
    opened = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)
    assert _send(client, opened).status_code == 204
    assert db_session.query(PrWarning).count() == 1

    _created2, updated2 = _mock_pr_change(
        monkeypatch, sources={"base123": _BASE_SOURCE, "head999": _BASE_SOURCE}
    )
    synchronize = _pr_payload(
        installation_id=org.github_installation_id, full_name=repo.github_full_name,
        action="synchronize", head_sha="head999",
    )

    res = _send(client, synchronize)

    assert res.status_code == 204
    assert len(updated2) == 1
    assert "더 이상 나타나지" in updated2[0]["body"]
    assert db_session.query(PrWarning).count() == 0


def test_GitHub_API_실패해도_204를_돌려준다(client, db_session, monkeypatch):
    from src.github.app_auth import GitHubAppError

    org, repo, _admin = _make_org_and_repo(db_session)
    monkeypatch.setattr(settings, "github_webhook_secret", _SECRET)

    def raise_error(*a, **k):
        raise GitHubAppError("네트워크 실패")

    monkeypatch.setattr(pr_warnings_router, "list_pr_files", raise_error)
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)

    res = _send(client, payload)

    assert res.status_code == 204
    assert db_session.query(PrWarning).count() == 0


def test_previous_filename이_이름_변경_판별에_전달된다(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session)
    old_source = "def process_payment(order_id):\n    return order_id\n"
    new_source = "def process_payment_v2(order_id):\n    return order_id\n"
    _mock_pr_change(
        monkeypatch,
        files=[{"filename": "src/payment.py", "previous_filename": "src/pay.py", "status": "renamed"}],
        sources={"base123": old_source, "head456": new_source},
    )
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)

    res = _send(client, payload)

    assert res.status_code == 204
    row = db_session.query(PrWarning).filter_by(repo_id=repo.id, pr_number=12).one()
    assert len(row.items) == 1
    assert row.items[0].change_type == "renamed"
    assert row.items[0].symbol_ident == "src/pay.py::process_payment"


# ---------------------------------------------------------------- GET /pr-warnings


def test_경고_이력이_없으면_빈_목록(client, db_session):
    _org, _repo, admin = _make_org_and_repo(db_session)

    res = client.get("/api/v1/pr-warnings", headers=_auth(admin))

    assert res.status_code == 200
    assert res.json() == {"items": [], "page": 1, "per_page": 20, "total": 0}


def test_경고_이력을_repo_기준으로_필터한다(client, db_session, monkeypatch):
    org, repo, admin = _make_org_and_repo(db_session)
    other_repo = Repo(organization_id=org.id, name="other", github_full_name="acme-payments/other")
    db_session.add(other_repo)
    db_session.commit()

    _mock_pr_change(monkeypatch, sources={"base123": _BASE_SOURCE, "head456": _HEAD_SOURCE})
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)
    assert _send(client, payload).status_code == 204

    res = client.get(f"/api/v1/pr-warnings?repo_id={other_repo.id}", headers=_auth(admin))

    assert res.status_code == 200
    assert res.json()["items"] == []

    res = client.get(f"/api/v1/pr-warnings?repo_id={repo.id}", headers=_auth(admin))
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["repo"] == repo.name
    assert body["items"][0]["warnings"][0]["change_type"] == "signature_changed"


# ---------------------------------------------------------------- 코멘트 실패 시 이력 보호


def test_코멘트_작성이_실패하면_DB에도_아무것도_남기지_않는다(client, db_session, monkeypatch):
    """코멘트를 못 보냈는데 이력에는 남으면, 나중에 조용히 사라져도 아무도 모른다.

    코멘트 API가 실패한 이번 시도는 아무것도 저장하지 않아야, 다음 synchronize가
    이 이벤트가 아예 없었던 것처럼 처음부터 다시 시도할 수 있다.
    """
    from src.github.app_auth import GitHubAppError

    org, repo, _admin = _make_org_and_repo(db_session)
    _mock_pr_change(monkeypatch, sources={"base123": _BASE_SOURCE, "head456": _HEAD_SOURCE})

    def raise_error(*a, **k):
        raise GitHubAppError("코멘트 작성 실패")

    monkeypatch.setattr(pr_warnings_router, "create_issue_comment", raise_error)
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)

    res = _send(client, payload)

    assert res.status_code == 204
    assert db_session.query(PrWarning).count() == 0


def test_코멘트_실패로_기록되지_않은_경고가_이후_해소돼도_조용히_넘어간다(client, db_session, monkeypatch):
    """코멘트 실패 뒤(이력 없음) 경고가 사라지면, 없는 행을 지우려다 터지면 안 된다."""
    from src.github.app_auth import GitHubAppError

    org, repo, _admin = _make_org_and_repo(db_session)
    _mock_pr_change(monkeypatch, sources={"base123": _BASE_SOURCE, "head456": _HEAD_SOURCE})
    monkeypatch.setattr(
        pr_warnings_router, "create_issue_comment",
        lambda *a, **k: (_ for _ in ()).throw(GitHubAppError("코멘트 작성 실패")),
    )
    opened = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)
    assert _send(client, opened).status_code == 204
    assert db_session.query(PrWarning).count() == 0

    created2, updated2 = _mock_pr_change(monkeypatch, sources={"base123": _BASE_SOURCE, "head789": _BASE_SOURCE})
    synchronize = _pr_payload(
        installation_id=org.github_installation_id, full_name=repo.github_full_name,
        action="synchronize", head_sha="head789",
    )

    res = _send(client, synchronize)

    assert res.status_code == 204
    assert created2 == []
    assert updated2 == []  # 알릴 코멘트도, 지울 이력도 없었으므로 아무 호출도 없다
    assert db_session.query(PrWarning).count() == 0


# ---------------------------------------------------------------- 이름 변경 + 미지원 확장자


def test_지원되지_않는_확장자로_이름이_바뀌어도_옛_함수는_삭제로_판별된다(client, db_session, monkeypatch):
    """.py를 .txt로 바꾸며 수정하면 새 이름은 파서가 없지만, 옛 함수가 사라진 사실은 잡아야 한다."""
    org, repo, _admin = _make_org_and_repo(db_session)
    created, _updated = _mock_pr_change(
        monkeypatch,
        files=[{"filename": "src/auth.txt", "previous_filename": "src/auth.py", "status": "renamed"}],
        sources={"base123": _BASE_SOURCE},
    )
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)

    res = _send(client, payload)

    assert res.status_code == 204
    row = db_session.query(PrWarning).filter_by(repo_id=repo.id, pr_number=12).one()
    assert len(row.items) == 1
    assert row.items[0].change_type == "deleted"
    assert row.items[0].symbol_ident == "src/auth.py::verify_token"
    assert len(created) == 1


def test_다른_조직의_경고는_보이지_않는다(client, db_session, monkeypatch):
    org, repo, _admin = _make_org_and_repo(db_session, installation_id=1)
    _mock_pr_change(monkeypatch, sources={"base123": _BASE_SOURCE, "head456": _HEAD_SOURCE})
    payload = _pr_payload(installation_id=org.github_installation_id, full_name=repo.github_full_name)
    assert _send(client, payload).status_code == 204

    other_org = Organization(name="다른회사", slug="other-co")
    db_session.add(other_org)
    db_session.flush()
    other_admin = User(organization_id=other_org.id, email="admin@other.dev", password_hash="x", name="관리자", role="admin")
    db_session.add(other_admin)
    db_session.commit()

    res = client.get("/api/v1/pr-warnings", headers=_auth(other_admin))

    assert res.status_code == 200
    assert res.json()["items"] == []
