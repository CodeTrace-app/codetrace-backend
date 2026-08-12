"""installation access token으로 호출하는 GitHub REST API 래퍼.

커밋 본문은 여기서 가져오지 않는다. `git log -L`로 줄 범위 이력을 봐야 해서
어차피 레포를 클론하므로, 커밋은 클론에서 읽는다 (src/github/git_history.py).
PR 본문과 리뷰 코멘트는 git에 없으므로 API로만 얻는다.
"""

import httpx

from src.github.app_auth import GITHUB_API, GitHubAppError, get_installation_token

_PER_PAGE = 100
_TIMEOUT = 10.0


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, token: str, what: str, **params) -> dict | list:
    """GET 한 번. 네트워크 실패도 GitHubAppError로 통일한다.

    httpx는 타임아웃·연결 실패 시 응답을 주지 않고 예외를 던진다. 그대로 두면
    라우터의 `except GitHubAppError`를 지나쳐 502가 아니라 500이 나간다.
    """
    try:
        response = httpx.get(f"{GITHUB_API}{path}", headers=_headers(token), params=params, timeout=_TIMEOUT)
    except httpx.HTTPError as error:
        raise GitHubAppError(f"{what} 조회 실패: {error}") from error
    if response.is_error:
        raise GitHubAppError(f"{what} 조회 실패: {response.status_code} {response.text}")
    return response.json()


def _get_paginated(path: str, token: str, what: str, key: str | None = None, **params) -> list[dict]:
    """페이지를 끝까지 따라가며 모은다.

    key가 있으면 응답 객체 안의 그 배열을, 없으면 응답 자체를 배열로 본다.
    """
    items: list[dict] = []
    page = 1
    while True:
        body = _get(path, token, what, per_page=_PER_PAGE, page=page, **params)
        page_items = body[key] if key else body
        items.extend(page_items)
        if len(page_items) < _PER_PAGE:
            return items
        page += 1


def list_installation_repos(installation_id: int) -> list[dict]:
    """설치된 GitHub App이 접근 가능한 레포 목록. 100개를 넘어도 전부 가져온다."""
    token = get_installation_token(installation_id)
    return _get_paginated("/installation/repositories", token, "레포 목록", key="repositories")


def get_repo(installation_id: int, full_name: str) -> dict:
    """레포 메타데이터. 대표 언어와 기본 브랜치는 git 클론에서 알 수 없어 API로 받는다."""
    token = get_installation_token(installation_id)
    return _get(f"/repos/{full_name}", token, "레포 정보")


def list_pull_requests(installation_id: int, full_name: str) -> list[dict]:
    """레포의 PR 전체. 닫힌 PR에도 맥락이 남아 있으므로 state=all로 가져온다."""
    token = get_installation_token(installation_id)
    return _get_paginated(f"/repos/{full_name}/pulls", token, "PR 목록", state="all")


def list_pr_commits(installation_id: int, full_name: str, number: int) -> list[dict]:
    """PR에 포함된 커밋 목록.

    커밋을 PR에 연결하는 유일하게 정확한 방법이다. 커밋 제목의 "(#12)"나
    "Merge pull request #12"에 기대면 팀 관례에 따라 어긋난다. 특히 머지 커밋은
    `git log -L` 결과에 나오지 않아 제목만으로는 PR 근거가 아예 안 붙는다.
    """
    token = get_installation_token(installation_id)
    return _get_paginated(f"/repos/{full_name}/pulls/{number}/commits", token, "PR 커밋")


def list_pr_comments(installation_id: int, full_name: str, number: int) -> list[dict]:
    """PR에 달린 코멘트. 두 종류를 합쳐서 돌려준다.

    GitHub은 PR 코멘트를 두 곳에 나눠 둔다.
    - /pulls/{n}/comments : 코드 줄에 달린 리뷰 코멘트
    - /issues/{n}/comments : 대화 탭의 일반 코멘트
    어느 쪽에 논의가 쌓이는지는 팀마다 다르므로 둘 다 본다.
    """
    token = get_installation_token(installation_id)
    review = _get_paginated(f"/repos/{full_name}/pulls/{number}/comments", token, "리뷰 코멘트")
    conversation = _get_paginated(f"/repos/{full_name}/issues/{number}/comments", token, "PR 코멘트")
    return [*review, *conversation]
