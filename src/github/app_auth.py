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
    """앱 JWT. 개인키가 없거나 형식이 깨져 있으면 여기서 실패한다.

    라우터는 GitHubAppError만 502로 바꾼다. jwt 라이브러리의 예외를 그대로 올리면
    설정 실수(배포에 키를 안 넣은 경우)가 500으로 나가 원인을 못 찾는다.
    """
    if not settings.github_private_key:
        raise GitHubAppError("GitHub App 개인키가 설정되지 않았습니다")

    now = datetime.now(timezone.utc)
    payload = {
        "iat": int((now - timedelta(seconds=60)).timestamp()),
        "exp": int((now + _JWT_TTL).timestamp()),
        "iss": settings.github_app_id,
    }
    try:
        return jwt.encode(payload, settings.github_private_key, algorithm="RS256")
    except Exception as error:
        raise GitHubAppError(f"앱 JWT 서명 실패: {error}") from error


def _request(method: str, path: str, token: str, **kwargs) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = httpx.request(method, f"{GITHUB_API}{path}", headers=headers, timeout=10.0, **kwargs)
    except httpx.HTTPError as error:
        # 타임아웃·연결 실패는 응답이 아니라 예외로 온다. 그대로 두면 500이 나간다.
        raise GitHubAppError(f"GitHub API {method} {path} 실패: {error}") from error
    if response.is_error:
        raise GitHubAppError(f"GitHub API {method} {path} 실패: {response.status_code} {response.text}")
    return response


def get_installation_token(installation_id: int) -> str:
    """installation access token을 반환한다. 캐시가 만료 임박 전이면 재사용한다.

    락은 캐시 접근에만 건다. 네트워크 호출까지 감싸면 서로 무관한 조직의 토큰 발급이
    한 줄로 서서, GitHub이 느릴 때 다른 조직의 API 요청까지 그만큼 밀린다.
    """
    now = datetime.now(timezone.utc)
    with _cache_lock:
        cached = _token_cache.get(installation_id)
    if cached and cached[1] - now > _TOKEN_REFRESH_MARGIN:
        return cached[0]

    response = _request("POST", f"/app/installations/{installation_id}/access_tokens", _app_jwt())
    try:
        data = response.json()
        token = data["token"]
        expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError) as error:
        raise GitHubAppError(f"토큰 응답 형식이 예상과 다릅니다: {error}") from error

    with _cache_lock:
        _token_cache[installation_id] = (token, expires_at)
    return token


def get_installation_account(installation_id: int) -> str:
    """설치된 GitHub 계정(조직 또는 사용자)의 로그인명을 반환한다."""
    response = _request("GET", f"/app/installations/{installation_id}", _app_jwt())
    try:
        return response.json()["account"]["login"]
    except (KeyError, TypeError, ValueError) as error:
        raise GitHubAppError(f"설치 정보 형식이 예상과 다릅니다: {error}") from error
