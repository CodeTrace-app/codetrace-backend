"""GitHub App 인증(JWT 서명·installation token 캐싱)을 검사한다."""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.config import settings
from src.github import app_auth


@pytest.fixture
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


@pytest.fixture(autouse=True)
def _clear_token_cache():
    app_auth._token_cache.clear()
    yield
    app_auth._token_cache.clear()


def _fake_response(payload: dict, is_error: bool = False, status_code: int = 200, text: str = ""):
    class FakeResponse:
        def __init__(self):
            self.is_error = is_error
            self.status_code = status_code
            self.text = text

        def json(self):
            return payload

    return FakeResponse()


def test_앱_jwt는_RS256으로_서명되고_iss가_app_id다(keypair, monkeypatch):
    private_pem, public_pem = keypair
    monkeypatch.setattr(settings, "github_app_id", "999")
    monkeypatch.setattr(settings, "github_app_private_key", private_pem)

    token = app_auth._app_jwt()
    payload = jwt.decode(token, public_pem, algorithms=["RS256"])

    assert payload["iss"] == "999"
    assert payload["exp"] - payload["iat"] <= 600  # GitHub 상한 10분


def test_installation_token은_만료_전까지_캐시된다(monkeypatch):
    calls = []

    def fake_request(method, path, token, **kwargs):
        calls.append(path)
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        return _fake_response({"token": "cached-token", "expires_at": expires.isoformat().replace("+00:00", "Z")})

    monkeypatch.setattr(app_auth, "_app_jwt", lambda: "app-jwt")
    monkeypatch.setattr(app_auth, "_request", fake_request)

    assert app_auth.get_installation_token(1) == "cached-token"
    assert app_auth.get_installation_token(1) == "cached-token"
    assert calls == ["/app/installations/1/access_tokens"]  # 두 번째 호출은 캐시를 썼다


def test_installation_token은_만료_임박이면_재발급된다(monkeypatch):
    calls = []

    def fake_request(method, path, token, **kwargs):
        calls.append(path)
        expires = datetime.now(timezone.utc) + timedelta(seconds=10)  # 재발급 여유(60초) 안쪽
        return _fake_response({"token": f"token-{len(calls)}", "expires_at": expires.isoformat().replace("+00:00", "Z")})

    monkeypatch.setattr(app_auth, "_app_jwt", lambda: "app-jwt")
    monkeypatch.setattr(app_auth, "_request", fake_request)

    first = app_auth.get_installation_token(1)
    second = app_auth.get_installation_token(1)

    assert first != second
    assert len(calls) == 2


def test_계정_로그인명을_installation_정보에서_가져온다(monkeypatch):
    monkeypatch.setattr(app_auth, "_app_jwt", lambda: "app-jwt")
    monkeypatch.setattr(app_auth, "_request", lambda *a, **k: _fake_response({"account": {"login": "acme-payments"}}))

    assert app_auth.get_installation_account(42) == "acme-payments"


def test_GitHub_API_에러_응답은_GitHubAppError로_바뀐다(monkeypatch):
    monkeypatch.setattr(
        "httpx.request",
        lambda *a, **k: _fake_response({}, is_error=True, status_code=401, text="Bad credentials"),
    )

    with pytest.raises(app_auth.GitHubAppError):
        app_auth._request("GET", "/app/installations/1", "token")
