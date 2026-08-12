"""인증·조직 생성 엔드포인트."""

BASE = "/api/v1"


def signup(client, email="kim@acme.dev", password="hunter22!", name="김팀장"):
    return client.post(
        f"{BASE}/auth/signup", json={"email": email, "password": password, "name": name}
    )


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── 회원가입 ────────────────────────────────────────────────────────────────


def test_가입하면_토큰을_받고_조직은_비어있다(client):
    res = signup(client)
    assert res.status_code == 201

    body = res.json()
    assert body["access_token"]
    assert body["user"]["email"] == "kim@acme.dev"
    # 가입과 조직 생성은 두 단계다. 프론트는 이 값이 null이면 조직 생성 화면으로 보낸다.
    assert body["organization"] is None


def test_같은_이메일로_두_번_가입하면_409(client):
    signup(client)
    assert signup(client).status_code == 409


def test_짧은_비밀번호는_거부한다(client):
    assert signup(client, password="1234").status_code == 422


def test_이메일_형식이_아니면_거부한다(client):
    assert signup(client, email="not-an-email").status_code == 422


# ── 로그인 ──────────────────────────────────────────────────────────────────


def test_로그인하면_조직_정보까지_돌려준다(client):
    token = signup(client).json()["access_token"]
    client.post(f"{BASE}/organizations", json={"name": "에이크미"}, headers=auth(token))

    res = client.post(f"{BASE}/auth/login", json={"email": "kim@acme.dev", "password": "hunter22!"})
    assert res.status_code == 200
    assert res.json()["organization"]["name"] == "에이크미"


def test_비밀번호가_틀리면_401(client):
    signup(client)
    res = client.post(f"{BASE}/auth/login", json={"email": "kim@acme.dev", "password": "wrong123"})
    assert res.status_code == 401


def test_없는_계정으로_로그인하면_401(client):
    res = client.post(f"{BASE}/auth/login", json={"email": "nobody@acme.dev", "password": "hunter22!"})
    assert res.status_code == 401


# ── 조직 생성 ───────────────────────────────────────────────────────────────


def test_조직을_만들면_식별자가_발급된다(client):
    token = signup(client).json()["access_token"]
    res = client.post(f"{BASE}/organizations", json={"name": "에이크미"}, headers=auth(token))

    assert res.status_code == 201
    body = res.json()
    assert body["organization"]["slug"]
    assert body["organization"]["plan"] == "starter"


def test_조직_생성_응답의_토큰으로_조직이_붙는다(client):
    """가입 시점 토큰에는 조직이 없다. 새 토큰을 쓰지 않으면 조회가 비어 나온다."""
    old_token = signup(client).json()["access_token"]
    new_token = client.post(
        f"{BASE}/organizations", json={"name": "에이크미"}, headers=auth(old_token)
    ).json()["access_token"]

    assert new_token != old_token
    me = client.get(f"{BASE}/auth/me", headers=auth(new_token)).json()
    assert me["organization"]["name"] == "에이크미"


def test_조직은_사용자당_하나만(client):
    token = signup(client).json()["access_token"]
    client.post(f"{BASE}/organizations", json={"name": "에이크미"}, headers=auth(token))
    res = client.post(f"{BASE}/organizations", json={"name": "다른조직"}, headers=auth(token))
    assert res.status_code == 409


def test_한글_조직명도_식별자가_만들어진다(client):
    token = signup(client).json()["access_token"]
    slug = client.post(
        f"{BASE}/organizations", json={"name": "에이크미"}, headers=auth(token)
    ).json()["organization"]["slug"]
    assert slug and all(c.isalnum() or c == "-" for c in slug)


def test_같은_이름의_조직도_식별자가_겹치지_않는다(client):
    slugs = set()
    for i in range(3):
        token = signup(client, email=f"user{i}@acme.dev").json()["access_token"]
        body = client.post(
            f"{BASE}/organizations", json={"name": "에이크미"}, headers=auth(token)
        ).json()
        slugs.add(body["organization"]["slug"])
    assert len(slugs) == 3


def test_토큰_없이_조직을_만들_수_없다(client):
    assert client.post(f"{BASE}/organizations", json={"name": "에이크미"}).status_code == 401


# ── 데모 세션 ───────────────────────────────────────────────────────────────


def test_데모_세션은_로그인_없이_발급된다(client):
    res = client.post(f"{BASE}/auth/demo")
    assert res.status_code == 200

    body = res.json()
    assert body["read_only"] is True
    assert body["organization"]["slug"] == "demo"


def test_데모_세션은_쓰기가_막힌다(client):
    token = client.post(f"{BASE}/auth/demo").json()["access_token"]
    res = client.post(f"{BASE}/organizations", json={"name": "몰래만든조직"}, headers=auth(token))
    assert res.status_code == 403


def test_데모_세션을_여러_번_받아도_조직이_하나다(client):
    first = client.post(f"{BASE}/auth/demo").json()["organization"]["id"]
    second = client.post(f"{BASE}/auth/demo").json()["organization"]["id"]
    assert first == second


def test_데모_계정으로는_로그인할_수_없다(client):
    client.post(f"{BASE}/auth/demo")
    res = client.post(
        f"{BASE}/auth/login", json={"email": "demo@codetrace.app", "password": "demo"}
    )
    assert res.status_code == 401


# ── 세션 복원 ───────────────────────────────────────────────────────────────


def test_me는_토큰을_돌려주지_않는다(client):
    token = signup(client).json()["access_token"]
    body = client.get(f"{BASE}/auth/me", headers=auth(token)).json()
    assert "access_token" not in body
    assert body["user"]["email"] == "kim@acme.dev"


def test_토큰이_없으면_me도_401(client):
    assert client.get(f"{BASE}/auth/me").status_code == 401
