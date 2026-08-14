"""installation access token으로 호출하는 GitHub REST API 래퍼.

커밋 본문은 여기서 가져오지 않는다. `git log -L`로 줄 범위 이력을 봐야 해서
어차피 레포를 클론하므로, 커밋은 클론에서 읽는다 (src/github/git_history.py).
PR 본문과 리뷰 코멘트는 git에 없으므로 API로만 얻는다.
"""

import base64

import httpx

from src.github.app_auth import GITHUB_API, GitHubAppError, get_installation_token

_PER_PAGE = 100
_TIMEOUT = 10.0
# 페이지를 끝없이 도는 걸 막는 안전장치. 종료 조건이 "마지막 페이지가 덜 찼다" 하나뿐이라,
# 응답이 page 파라미터를 무시하면(프록시·API 변경) 백그라운드 스레드가 영원히 돈다.
_MAX_PAGES = 50


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, token: str, what: str, *, not_found_ok: bool = False, **params) -> dict | list | None:
    """GET 한 번. 네트워크 실패도 GitHubAppError로 통일한다.

    httpx는 타임아웃·연결 실패 시 응답을 주지 않고 예외를 던진다. 그대로 두면
    라우터의 `except GitHubAppError`를 지나쳐 502가 아니라 500이 나간다.

    not_found_ok면 404를 예외 대신 None으로 돌려준다. PR 검사에서 base·head
    어느 한쪽 리비전에 파일이 없는 것(신규·삭제 파일)은 정상 상태라 에러가 아니다.
    """
    try:
        response = httpx.get(f"{GITHUB_API}{path}", headers=_headers(token), params=params, timeout=_TIMEOUT)
    except httpx.HTTPError as error:
        raise GitHubAppError(f"{what} 조회 실패: {error}") from error
    if not_found_ok and response.status_code == 404:
        return None
    if response.is_error:
        raise GitHubAppError(f"{what} 조회 실패: {response.status_code} {response.text}")
    return response.json()


def _post(path: str, token: str, what: str, **kwargs) -> dict:
    try:
        response = httpx.post(f"{GITHUB_API}{path}", headers=_headers(token), timeout=_TIMEOUT, **kwargs)
    except httpx.HTTPError as error:
        raise GitHubAppError(f"{what} 실패: {error}") from error
    if response.is_error:
        raise GitHubAppError(f"{what} 실패: {response.status_code} {response.text}")
    return response.json()


def _patch(path: str, token: str, what: str, *, not_found_ok: bool = False, **kwargs) -> dict | None:
    try:
        response = httpx.patch(f"{GITHUB_API}{path}", headers=_headers(token), timeout=_TIMEOUT, **kwargs)
    except httpx.HTTPError as error:
        raise GitHubAppError(f"{what} 실패: {error}") from error
    if not_found_ok and response.status_code == 404:
        return None
    if response.is_error:
        raise GitHubAppError(f"{what} 실패: {response.status_code} {response.text}")
    return response.json()


def _get_paginated(path: str, token: str, what: str, key: str | None = None, **params) -> list[dict]:
    """페이지를 끝까지 따라가며 모은다.

    key가 있으면 응답 객체 안의 그 배열을, 없으면 응답 자체를 배열로 본다.
    """
    items: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        body = _get(path, token, what, per_page=_PER_PAGE, page=page, **params)
        page_items = body[key] if key else body
        items.extend(page_items)
        if len(page_items) < _PER_PAGE:
            return items
    raise GitHubAppError(f"{what} 조회가 {_MAX_PAGES}페이지를 넘겼습니다")


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


def list_pr_files(installation_id: int, full_name: str, number: int) -> list[dict]:
    """PR에서 바뀐 파일 목록. filename·status·(이름이 바뀐 경우) previous_filename을 담는다.

    이슈 #24 판별 함수의 ChangedFile.previous_path가 여기서 나온다.
    """
    token = get_installation_token(installation_id)
    return _get_paginated(f"/repos/{full_name}/pulls/{number}/files", token, "PR 변경 파일")


def get_file_content(installation_id: int, full_name: str, path: str, ref: str) -> str | None:
    """특정 리비전의 파일 내용. 그 리비전에 파일이 없으면(신규·삭제) None.

    Contents API는 1MB를 넘는 파일에는 content를 안 주고 download_url만 준다 —
    그런 파일도 None으로 처리한다(PR 검사가 대상으로 삼기엔 너무 크다).
    바이너리라 utf-8로 못 읽으면 파서 대상이 아니므로 마찬가지로 None.
    """
    token = get_installation_token(installation_id)
    body = _get(f"/repos/{full_name}/contents/{path}", token, "파일 내용", not_found_ok=True, ref=ref)
    if not isinstance(body, dict) or body.get("encoding") != "base64" or "content" not in body:
        return None
    try:
        return base64.b64decode(body["content"]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def create_issue_comment(installation_id: int, full_name: str, number: int, body: str) -> int:
    """PR에 코멘트를 새로 단다. 재검사 시 수정할 수 있도록 만든 코멘트의 id를 돌려준다."""
    token = get_installation_token(installation_id)
    result = _post(f"/repos/{full_name}/issues/{number}/comments", token, "PR 코멘트 작성", json={"body": body})
    return result["id"]


def update_issue_comment(installation_id: int, full_name: str, comment_id: int, body: str) -> bool:
    """기존 코멘트를 고쳐 쓴다. 코멘트가 이미 지워졌으면(수동 삭제 등) False를 돌려준다.

    PR당 코멘트 1개 유지 규칙(api-spec §8) 때문에 재검사 때 새로 달지 않고 이 함수로 고친다.
    """
    token = get_installation_token(installation_id)
    result = _patch(
        f"/repos/{full_name}/issues/comments/{comment_id}", token, "PR 코멘트 수정",
        not_found_ok=True, json={"body": body},
    )
    return result is not None
