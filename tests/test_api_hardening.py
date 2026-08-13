"""코드 리뷰에서 나온 경계 조건들.

동시 요청, 응답 시간 노출, 길이 제한처럼 정상 흐름 테스트로는 드러나지 않는 것들이다.
"""

import time

import pytest

from src.api import repos as repos_router
from src.auth import hash_password, verify_password_constant_time
from src.db.models import Organization, Repo, User

BASE = "/api/v1"


@pytest.fixture(autouse=True)
def scheduled_indexing(monkeypatch):
    """백그라운드 인덱싱을 실제로 돌리지 않는다.

    여기서 보려는 것은 등록 단계의 경계 조건이고, 수집 파이프라인은 별도 테스트가 검증한다.
    """
    monkeypatch.setattr(repos_router, "run_indexing", lambda *args: None)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signup(client, email="kim@acme.dev", password="hunter22!"):
    return client.post(
        f"{BASE}/auth/signup", json={"email": email, "password": password, "name": "김팀장"}
    )


# ── 로그인 응답 시간으로 가입 여부가 새지 않는다 ──────────────────────────


def test_없는_계정도_해시_검증을_거친다():
    """계정 유무로 응답 시간이 갈리면 이메일 목록만으로 가입자를 가려낼 수 있다."""
    started = time.perf_counter()
    assert verify_password_constant_time("hunter22!", None) is False
    absent = time.perf_counter() - started

    hashed = hash_password("hunter22!")
    started = time.perf_counter()
    verify_password_constant_time("wrong-password", hashed)
    present = time.perf_counter() - started

    # 같은 자릿수면 충분하다. 정확히 같을 수는 없다.
    assert absent > present / 10


def test_가입_여부와_무관하게_같은_메시지를_준다(client):
    signup(client)

    known = client.post(f"{BASE}/auth/login", json={"email": "kim@acme.dev", "password": "wrong123!"})
    unknown = client.post(f"{BASE}/auth/login", json={"email": "nobody@acme.dev", "password": "wrong123!"})

    assert known.status_code == unknown.status_code == 401
    assert known.json()["detail"] == unknown.json()["detail"]


# ── 중복 가입 ──────────────────────────────────────────────────────────────


def test_중복_가입은_409로_돌려준다(client):
    """미리 조회해서 막아도 동시 요청은 통과한다. unique 제약 위반을 409로 옮긴다."""
    signup(client)
    assert signup(client).status_code == 409


def test_중복_가입_실패_뒤에도_세션이_살아있다(client, db_session):
    """rollback을 빼먹으면 이후 요청이 PendingRollbackError로 전부 깨진다."""
    signup(client)
    signup(client)

    assert signup(client, email="other@acme.dev").status_code == 201
    assert db_session.query(User).count() == 2


# ── 조직 생성 ──────────────────────────────────────────────────────────────


def test_조직은_한_번만_만들어진다(client, db_session):
    token = signup(client).json()["access_token"]

    first = client.post(f"{BASE}/organizations", json={"name": "에이크미"}, headers=auth(token))
    second = client.post(f"{BASE}/organizations", json={"name": "또다른조직"}, headers=auth(token))

    assert first.status_code == 201
    assert second.status_code == 409
    # 고아 조직이 남으면 사용자와 연결되지 않은 데이터가 생긴다
    assert db_session.query(Organization).count() == 1


# ── 레포 등록 ──────────────────────────────────────────────────────────────


def _org_with_installation(client, db_session):
    token = signup(client).json()["access_token"]
    token = client.post(
        f"{BASE}/organizations", json={"name": "에이크미"}, headers=auth(token)
    ).json()["access_token"]

    org = db_session.query(Organization).filter_by(name="에이크미").one()
    org.github_installation_id = 1234
    db_session.commit()
    return token, org


def test_같은_레포를_두_번_등록하면_409(client, db_session):
    """500이 나면 화면에 에러가 그대로 노출된다."""
    token, _ = _org_with_installation(client, db_session)
    body = {"github_full_name": "acme-payments/acme-payment-service"}

    first = client.post(f"{BASE}/repos", json=body, headers=auth(token))
    second = client.post(f"{BASE}/repos", json=body, headers=auth(token))

    assert first.status_code == 201
    assert second.status_code == 409
    assert db_session.query(Repo).count() == 1


def test_플랜_한도를_넘기면_403(client, db_session):
    token, org = _org_with_installation(client, db_session)

    for i in range(org.repo_limit):
        res = client.post(
            f"{BASE}/repos", json={"github_full_name": f"acme-payments/repo-{i}"}, headers=auth(token)
        )
        assert res.status_code == 201

    over = client.post(
        f"{BASE}/repos", json={"github_full_name": "acme-payments/one-more"}, headers=auth(token)
    )
    assert over.status_code == 403
    assert db_session.query(Repo).count() == org.repo_limit


# ── 입력 길이 ──────────────────────────────────────────────────────────────


def test_레포_이름이_컬럼_길이를_넘으면_거부한다(client, db_session):
    """정규식만 보면 201자가 통과해 저장 시점에 500이 난다."""
    token, _ = _org_with_installation(client, db_session)
    too_long = f"{'a' * 100}/{'b' * 100}"  # 201자

    res = client.post(f"{BASE}/repos", json={"github_full_name": too_long}, headers=auth(token))

    assert res.status_code == 422


def test_한글_비밀번호도_바이트로_길이를_잰다(client):
    """bcrypt는 72바이트를 넘는 부분을 버린다. 글자 수만 재면 뒷부분이 검증에 반영되지 않는다."""
    korean = "비밀번호" * 7  # 28자 = 84바이트

    res = signup(client, email="long@acme.dev", password=korean)

    assert res.status_code == 422


def test_영문_비밀번호는_72바이트까지_받는다(client):
    res = signup(client, email="ok@acme.dev", password="a" * 72)
    assert res.status_code == 201
