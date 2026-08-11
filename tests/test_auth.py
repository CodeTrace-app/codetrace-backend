"""인증 의존성이 데모 세션·권한 경계를 지키는지 검사한다."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.auth import Ctx, admin_user, create_token, current_user, hash_password, verify_password, writable_user
from src.db.models import User


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/read")
    def read(ctx: Ctx = Depends(current_user)):
        return {"org": ctx.organization_id}

    @app.post("/write")
    def write(ctx: Ctx = Depends(writable_user)):
        return {"ok": True}

    @app.get("/admin")
    def admin(ctx: Ctx = Depends(admin_user)):
        return {"ok": True}

    return TestClient(app)


def token(user_id: int, org: int, role: str = "member", read_only: bool = False) -> str:
    return create_token(User(id=user_id, organization_id=org, role=role), read_only=read_only)


def auth(t: str | None) -> dict:
    return {"Authorization": f"Bearer {t}"} if t else {}


def test_토큰이_없으면_401(client):
    assert client.get("/read").status_code == 401


def test_잘못된_토큰은_401(client):
    assert client.get("/read", headers=auth("garbage")).status_code == 401


def test_데모_세션도_읽기는_된다(client):
    t = token(3, 99, read_only=True)
    assert client.get("/read", headers=auth(t)).status_code == 200


def test_데모_세션은_쓰기가_막힌다(client):
    t = token(3, 99, read_only=True)
    assert client.post("/write", headers=auth(t)).status_code == 403


def test_일반_사용자는_쓸_수_있다(client):
    assert client.post("/write", headers=auth(token(2, 7))).status_code == 200


def test_일반_사용자는_관리자_API를_못_쓴다(client):
    assert client.get("/admin", headers=auth(token(2, 7))).status_code == 403


def test_관리자는_관리자_API를_쓸_수_있다(client):
    t = token(1, 7, role="admin")
    assert client.get("/admin", headers=auth(t)).status_code == 200


def test_토큰의_조직이_그대로_전달된다(client):
    res = client.get("/read", headers=auth(token(2, 7)))
    assert res.json()["org"] == 7


def test_비밀번호_해시와_검증():
    hashed = hash_password("hunter22")
    assert hashed != "hunter22"
    assert verify_password("hunter22", hashed)
    assert not verify_password("wrong", hashed)
