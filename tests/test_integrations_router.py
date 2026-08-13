"""연동 설정(GitHub App) API를 검사한다. docs/api-spec.md §2."""

from datetime import datetime, timedelta, timezone

import jwt

from src.auth import create_token
from src.config import settings
from src.db.models import Organization, Repo, User
from src.api import integrations as integrations_router


def _make_org_and_admin(db_session, *, installation_id=None, account=None):
    org = Organization(name="에이크미", slug="acme", github_installation_id=installation_id, github_account=account)
    db_session.add(org)
    db_session.flush()
    admin = User(organization_id=org.id, email="admin@acme.dev", password_hash="x", name="관리자", role="admin")
    db_session.add(admin)
    db_session.commit()
    return org, admin


def _make_orgless_user(db_session):
    """가입만 하고 아직 조직을 안 만든 사용자. organization_id가 nullable로 바뀐 뒤 추가된 경로."""
    user = User(organization_id=None, email="new@acme.dev", password_hash="x", name="신규", role="admin")
    db_session.add(user)
    db_session.commit()
    return user


def _auth(user, read_only=False):
    return {"Authorization": f"Bearer {create_token(user, read_only=read_only)}"}


def _install_state(organization_id, purpose="github_install"):
    payload = {
        "org": organization_id,
        "purpose": purpose,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def test_미연동_상태를_보여준다(client, db_session):
    _org, admin = _make_org_and_admin(db_session)

    res = client.get("/api/v1/integrations", headers=_auth(admin))

    assert res.status_code == 200
    body = res.json()
    assert body["github"] == {"status": "not_connected"}
    assert body["gitlab"] == {"status": "coming_soon"}


def test_연동된_상태를_보여준다(client, db_session):
    _org, admin = _make_org_and_admin(db_session, installation_id=123, account="acme-payments")

    res = client.get("/api/v1/integrations", headers=_auth(admin))

    assert res.json()["github"] == {"status": "connected", "installation_id": 123, "account": "acme-payments"}


def test_조직_없는_사용자는_연동_상태_조회시_404(client, db_session):
    user = _make_orgless_user(db_session)

    res = client.get("/api/v1/integrations", headers=_auth(user))

    assert res.status_code == 404


def test_조직_없는_사용자는_설치_URL도_404(client, db_session):
    user = _make_orgless_user(db_session)

    res = client.get("/api/v1/integrations/github/install-url", headers=_auth(user))

    assert res.status_code == 404


def test_설치_URL은_조직을_담은_state를_포함한다(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "github_app_slug", "codetrace-app")
    org, admin = _make_org_and_admin(db_session)

    res = client.get("/api/v1/integrations/github/install-url", headers=_auth(admin))

    assert res.status_code == 200
    url = res.json()["url"]
    assert url.startswith("https://github.com/apps/codetrace-app/installations/new?state=")

    state = url.split("state=", 1)[1]
    payload = jwt.decode(state, settings.secret_key, algorithms=["HS256"])
    assert payload["org"] == org.id
    assert payload["purpose"] == "github_install"


def test_데모_세션은_설치_URL을_받을_수_없다(client, db_session):
    _org, admin = _make_org_and_admin(db_session)

    res = client.get("/api/v1/integrations/github/install-url", headers=_auth(admin, read_only=True))

    assert res.status_code == 403


def test_콜백은_installation_id를_저장하고_프론트로_리다이렉트한다(client, db_session, monkeypatch):
    org, _admin = _make_org_and_admin(db_session)
    monkeypatch.setattr(integrations_router, "get_installation_account", lambda installation_id: "acme-payments")

    res = client.get(
        "/api/v1/integrations/github/callback",
        params={"installation_id": 999, "setup_action": "install", "state": _install_state(org.id)},
        follow_redirects=False,
    )

    assert res.status_code in (302, 307)
    assert res.headers["location"] == f"{settings.frontend_url}/settings/integrations"

    db_session.refresh(org)
    assert org.github_installation_id == 999
    assert org.github_account == "acme-payments"


def test_다른_조직의_설치를_가로챌_수_없다(client, db_session, monkeypatch):
    """installation_id는 브라우저 주소창에서 오는 값이라 위조할 수 있다.

    공격자가 자기 조직용 state를 정상 발급받은 뒤 남의 installation_id를 붙이면
    그 조직의 private 레포를 읽게 된다. state 검증만으로는 못 막는다.
    """
    victim = Organization(
        name="피해자회사", slug="victim", github_installation_id=77777, github_account="victim-corp"
    )
    db_session.add(victim)
    attacker, _admin = _make_org_and_admin(db_session)
    db_session.commit()

    monkeypatch.setattr(integrations_router, "get_installation_account", lambda i: "victim-corp")

    res = client.get(
        "/api/v1/integrations/github/callback",
        params={"installation_id": 77777, "setup_action": "install", "state": _install_state(attacker.id)},
        follow_redirects=False,
    )

    assert res.status_code == 409
    db_session.refresh(attacker)
    assert attacker.github_installation_id is None
    assert attacker.github_account is None


def test_같은_조직이_재설치하는_것은_막지_않는다(client, db_session, monkeypatch):
    org, _admin = _make_org_and_admin(db_session, installation_id=555, account="acme")
    monkeypatch.setattr(integrations_router, "get_installation_account", lambda i: "acme")

    res = client.get(
        "/api/v1/integrations/github/callback",
        params={"installation_id": 555, "setup_action": "update", "state": _install_state(org.id)},
        follow_redirects=False,
    )

    assert res.status_code in (302, 307)


def test_installation_id_없는_콜백도_리다이렉트된다(client, db_session):
    """조직 승인 대기(setup_action=request)면 GitHub이 installation_id를 안 보낸다.

    필수 파라미터로 두면 사용자가 프론트 대신 생 JSON 422를 본다.
    """
    org, _admin = _make_org_and_admin(db_session)

    res = client.get(
        "/api/v1/integrations/github/callback",
        params={"setup_action": "request", "state": _install_state(org.id)},
        follow_redirects=False,
    )

    assert res.status_code in (302, 307)
    db_session.refresh(org)
    assert org.github_installation_id is None


def test_만료_시각이_없는_state는_거부한다(client, db_session):
    """exp를 빼면 만료 검사가 통째로 건너뛰어진다."""
    org, _admin = _make_org_and_admin(db_session)
    forged = jwt.encode(
        {"org": org.id, "purpose": "github_install"}, settings.secret_key, algorithm="HS256"
    )

    res = client.get(
        "/api/v1/integrations/github/callback",
        params={"installation_id": 1, "setup_action": "install", "state": forged},
    )

    assert res.status_code == 400


def test_승인_대기_setup_action은_저장하지_않는다(client, db_session):
    org, _admin = _make_org_and_admin(db_session)

    res = client.get(
        "/api/v1/integrations/github/callback",
        params={"installation_id": 999, "setup_action": "request", "state": _install_state(org.id)},
        follow_redirects=False,
    )

    assert res.status_code in (302, 307)
    db_session.refresh(org)
    assert org.github_installation_id is None


def test_위조된_state는_400(client, db_session):
    res = client.get(
        "/api/v1/integrations/github/callback",
        params={"installation_id": 999, "setup_action": "install", "state": "garbage"},
    )
    assert res.status_code == 400


def test_다른_목적의_state는_거부된다(client, db_session):
    org, _admin = _make_org_and_admin(db_session)
    state = _install_state(org.id, purpose="something-else")

    res = client.get(
        "/api/v1/integrations/github/callback",
        params={"installation_id": 999, "setup_action": "install", "state": state},
    )
    assert res.status_code == 400


def test_존재하지_않는_조직의_state는_404(client, db_session):
    res = client.get(
        "/api/v1/integrations/github/callback",
        params={"installation_id": 999, "setup_action": "install", "state": _install_state(99999)},
    )
    assert res.status_code == 404


def test_미설치_상태에서_레포_목록은_409(client, db_session):
    _org, admin = _make_org_and_admin(db_session)

    res = client.get("/api/v1/integrations/github/repos", headers=_auth(admin))

    assert res.status_code == 409


def test_레포_목록에_이미_추가된_레포가_표시된다(client, db_session, monkeypatch):
    org, admin = _make_org_and_admin(db_session, installation_id=123, account="acme-payments")
    db_session.add(Repo(organization_id=org.id, name="api", github_full_name="acme-payments/api"))
    db_session.commit()

    monkeypatch.setattr(
        integrations_router,
        "list_installation_repos",
        lambda installation_id: [
            {"full_name": "acme-payments/api", "private": True},
            {"full_name": "acme-payments/web", "private": True},
        ],
    )

    res = client.get("/api/v1/integrations/github/repos", headers=_auth(admin))

    assert res.status_code == 200
    repos = {r["github_full_name"]: r for r in res.json()["repos"]}
    assert repos["acme-payments/api"]["already_added"] is True
    assert repos["acme-payments/web"]["already_added"] is False
