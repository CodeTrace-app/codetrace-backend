"""GitHub App 인증. 앱 개인키로 JWT를 서명해 installation access token을 발급·캐싱한다.

흐름: 앱 JWT(RS256, 최대 10분) 서명 → POST /app/installations/{id}/access_tokens
→ installation access token(1시간 유효). 매 API 호출마다 새로 발급하면 앱 JWT
서명 비용과 GitHub 레이트리밋을 낭비하므로 만료 전까지 재사용한다.
"""

import threading
from datetime import datetime, timedelta, timezone

import httpx
import jwt

from src.config import settings

GITHUB_API = "https://api.github.com"
_JWT_TTL = timedelta(minutes=9)  # GitHub 허용 상한 10분에서 시계 오차만큼 여유를 둔다
_TOKEN_REFRESH_MARGIN = timedelta(seconds=60)

# installation_id -> (token, expires_at). 단일 프로세스 배포 전제 (해커톤 규모).
# 인덱싱은 백그라운드 스레드에서 도니 단일 프로세스여도 동시 접근은 일어난다.
_token_cache: dict[int, tuple[str, datetime]] = {}
_cache_lock = threading.Lock()


class GitHubAppError(Exception):
    """GitHub App API 호출 실패."""


def _app_jwt() -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iat": int((now - timedelta(seconds=60)).timestamp()),
        "exp": int((now + _JWT_TTL).timestamp()),
        "iss": settings.github_app_id,
    }
    return jwt.encode(payload, settings.github_private_key, algorithm="RS256")


def _request(method: str, path: str, token: str, **kwargs) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = httpx.request(method, f"{GITHUB_API}{path}", headers=headers, timeout=10.0, **kwargs)
    if response.is_error:
        raise GitHubAppError(f"GitHub API {method} {path} 실패: {response.status_code} {response.text}")
    return response


def get_installation_token(installation_id: int) -> str:
    """installation access token을 반환한다. 캐시가 만료 임박 전이면 재사용한다."""
    with _cache_lock:
        cached = _token_cache.get(installation_id)
        now = datetime.now(timezone.utc)
        if cached and cached[1] - now > _TOKEN_REFRESH_MARGIN:
            return cached[0]

        response = _request("POST", f"/app/installations/{installation_id}/access_tokens", _app_jwt())
        data = response.json()
        expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        _token_cache[installation_id] = (data["token"], expires_at)
        return data["token"]


def get_installation_account(installation_id: int) -> str:
    """설치된 GitHub 계정(조직 또는 사용자)의 로그인명을 반환한다."""
    response = _request("GET", f"/app/installations/{installation_id}", _app_jwt())
    return response.json()["account"]["login"]
