"""구독 문의·요금제 조회 (이슈 #42)."""

from src.db.models import Inquiry

BASE = "/api/v1"

INQUIRY = {
    "organization_name": "에이크미",
    "contact_name": "김팀장",
    "contact": "010-1234-5678",
    "plan": "team",
}


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signup(client, email="kim@acme.dev"):
    """가입하고 조직까지 만든 뒤 토큰을 돌려준다."""
    token = client.post(
        f"{BASE}/auth/signup",
        json={"email": email, "password": "hunter22!", "name": "김팀장"},
    ).json()["access_token"]
    return client.post(
        f"{BASE}/organizations", json={"name": "에이크미"}, headers=auth(token)
    ).json()["access_token"]


# ── 구독 문의 ──────────────────────────────────────────────────────────────


def test_로그인하지_않아도_문의가_접수된다(client, db_session):
    """가입 전 방문자가 요금제 화면에서 남기는 문의다."""
    res = client.post(f"{BASE}/inquiries", json=INQUIRY)

    assert res.status_code == 201
    assert res.json()["message"]
    assert db_session.query(Inquiry).count() == 1


def test_문의_내용이_그대로_저장된다(client, db_session):
    client.post(f"{BASE}/inquiries", json=INQUIRY)

    row = db_session.query(Inquiry).one()
    assert row.organization_name == "에이크미"
    assert row.contact == "010-1234-5678"
    assert row.plan == "team"


def test_없는_플랜은_거부한다(client):
    res = client.post(f"{BASE}/inquiries", json={**INQUIRY, "plan": "enterprise"})
    assert res.status_code == 422


def test_연락처가_비면_거부한다(client):
    res = client.post(f"{BASE}/inquiries", json={**INQUIRY, "contact": ""})
    assert res.status_code == 422


# ── 요금제 조회 ────────────────────────────────────────────────────────────


def test_관리자는_현재_요금제를_본다(client):
    token = signup(client)

    res = client.get(f"{BASE}/admin/plan", headers=auth(token))

    assert res.status_code == 200
    body = res.json()
    # 조직을 만들면 Starter로 시작한다
    assert body["plan"] == "starter"
    assert body["repo_limit"] == 3
    assert body["repos_used"] == 0
    assert body["price_krw"] == 50_000


def test_토큰_없이는_조회할_수_없다(client):
    assert client.get(f"{BASE}/admin/plan").status_code == 401


def test_데모_세션은_조회할_수_없다(client):
    token = client.post(f"{BASE}/auth/demo").json()["access_token"]
    assert client.get(f"{BASE}/admin/plan", headers=auth(token)).status_code == 403


def test_조직이_없는_사용자는_조회할_수_없다(client):
    """가입만 하고 조직을 만들지 않은 상태."""
    token = client.post(
        f"{BASE}/auth/signup",
        json={"email": "solo@acme.dev", "password": "hunter22!", "name": "혼자"},
    ).json()["access_token"]

    assert client.get(f"{BASE}/admin/plan", headers=auth(token)).status_code == 404
