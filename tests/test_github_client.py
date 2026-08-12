"""installation token 기반 GitHub REST 호출(레포 목록)을 검사한다."""

import pytest

from src.github import client


def _fake_response(payload: dict, is_error: bool = False, status_code: int = 200, text: str = ""):
    class FakeResponse:
        def __init__(self):
            self.is_error = is_error
            self.status_code = status_code
            self.text = text

        def json(self):
            return payload

    return FakeResponse()


def test_레포_목록을_가져온다(monkeypatch):
    monkeypatch.setattr(client, "get_installation_token", lambda installation_id: "tok")
    monkeypatch.setattr(
        "httpx.get",
        lambda *a, **k: _fake_response({"repositories": [{"full_name": "acme/api", "private": True}]}),
    )

    assert client.list_installation_repos(1) == [{"full_name": "acme/api", "private": True}]


def test_레포가_100개를_넘으면_다음_페이지까지_가져온다(monkeypatch):
    monkeypatch.setattr(client, "get_installation_token", lambda installation_id: "tok")

    pages = {
        1: [{"full_name": f"acme/repo-{i}", "private": True} for i in range(100)],
        2: [{"full_name": "acme/repo-100", "private": True}],
    }
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append(params["page"])
        return _fake_response({"repositories": pages[params["page"]]})

    monkeypatch.setattr("httpx.get", fake_get)

    repos = client.list_installation_repos(1)

    assert len(repos) == 101
    assert calls == [1, 2]


def test_실패_응답은_GitHubAppError로_바뀐다(monkeypatch):
    monkeypatch.setattr(client, "get_installation_token", lambda installation_id: "tok")
    monkeypatch.setattr(
        "httpx.get",
        lambda *a, **k: _fake_response({}, is_error=True, status_code=404, text="Not Found"),
    )

    with pytest.raises(client.GitHubAppError):
        client.list_installation_repos(1)
