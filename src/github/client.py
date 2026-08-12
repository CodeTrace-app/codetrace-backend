"""installation access token으로 호출하는 GitHub REST API 래퍼.

레포 목록 외에 커밋·PR·리뷰 수집(F-FOYOKP), 경고 코멘트 작성(F-LKOZFY)도
이 모듈에 함수를 추가해나간다.
"""

import httpx

from src.github.app_auth import GITHUB_API, GitHubAppError, get_installation_token


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def list_installation_repos(installation_id: int) -> list[dict]:
    """설치된 GitHub App이 접근 가능한 레포 목록. 100개를 넘어도 전부 가져온다."""
    token = get_installation_token(installation_id)
    repos: list[dict] = []
    page = 1
    while True:
        response = httpx.get(
            f"{GITHUB_API}/installation/repositories",
            headers=_headers(token),
            params={"per_page": 100, "page": page},
            timeout=10.0,
        )
        if response.is_error:
            raise GitHubAppError(f"레포 목록 조회 실패: {response.status_code} {response.text}")
        page_repos = response.json()["repositories"]
        repos.extend(page_repos)
        if len(page_repos) < 100:
            break
        page += 1
    return repos
