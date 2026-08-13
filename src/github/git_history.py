"""로컬 클론에서 커밋 이력을 읽는다.

`git blame`이 아니라 `git log -L`을 쓴다. blame은 각 줄의 **마지막** 커밋만 남기므로
전체 포맷팅이나 리팩터링 커밋 하나가 실제 의사결정 커밋을 전부 가린다.
log -L은 그 줄 범위를 건드린 커밋을 시간순으로 전부 돌려준다 (이슈 #27).

이 모듈은 git만 다룬다. DB 저장과 GitHub API 호출은 src/indexing/history.py가 한다.
"""

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 커밋 메시지 본문에는 줄바꿈이 들어가므로 줄 단위로 자를 수 없다.
# 메시지에 나올 수 없는 제어문자를 구분자로 쓴다.
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"

_CLONE_TIMEOUT = 300
_COMMAND_TIMEOUT = 60


class GitError(Exception):
    """git 명령 실패."""


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    title: str
    body: str
    author: str
    committed_at: datetime


def _redact(text: str, token: str | None) -> str:
    """에러 메시지에 섞여 나온 설치 토큰을 지운다.

    클론 URL에 토큰이 들어가므로 git이 실패 메시지에 URL을 그대로 담을 수 있다.
    그대로 두면 로그에 자격증명이 남는다.
    """
    return text.replace(token, "***") if token else text


def _run(args: list[str], cwd: Path | None = None, token: str | None = None, timeout: int = _COMMAND_TIMEOUT) -> str:
    # 토큰이 만료·무효면 git이 자격증명을 물으며 멈춘다. 백그라운드에는 답할 사람이 없어
    # 타임아웃까지 스레드를 잡고 있으므로, 묻지 말고 바로 실패하게 한다.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        raise GitError("git 실행 파일을 찾을 수 없습니다") from None
    except subprocess.TimeoutExpired:
        # `from None`으로 원본 예외를 끊는다. TimeoutExpired의 문자열에는 argv 전체가 들어 있고
        # 거기엔 토큰이 박힌 클론 URL이 있다. 체이닝된 채로 두면 logger.exception이
        # 트레이스백에 그대로 찍어 로그에 자격증명이 남는다.
        raise GitError(_redact(f"git {args[0]} 시간 초과", token)) from None

    if result.returncode != 0:
        raise GitError(_redact(f"git {args[0]} 실패: {result.stderr.strip()}", token))
    return result.stdout


def clone_url(full_name: str, token: str) -> str:
    """installation token을 붙인 클론 주소.

    GitHub App 토큰은 사용자명 자리에 x-access-token을 쓴다.
    """
    return f"https://x-access-token:{token}@github.com/{full_name}.git"


def ensure_clone(full_name: str, token: str, dest: Path) -> Path:
    """레포를 dest에 최신 상태로 준비한다.

    `git log -L`은 전체 이력을 훑으므로 얕은 클론(--depth)을 쓸 수 없다.

    재사용할 때 fetch만 하면 안 된다. fetch는 refs/remotes/origin/*만 갱신하고
    로컬 브랜치와 작업트리는 예전 커밋에 그대로 남는데, list_commits와 line_history는
    HEAD를 본다. 그러면 재인덱싱이 에러 없이 옛날 데이터를 다시 저장하고 끝난다.
    그래서 origin/HEAD로 되돌려 작업트리까지 맞춘다.
    """
    url = clone_url(full_name, token)
    if (dest / ".git").is_dir():
        try:
            # 토큰은 1시간마다 바뀐다. 예전 토큰이 박힌 remote를 그대로 두면 fetch가 실패한다.
            _run(["remote", "set-url", "origin", url], cwd=dest, token=token)
            _run(["fetch", "--prune", "origin"], cwd=dest, token=token, timeout=_CLONE_TIMEOUT)
            # 기본 브랜치가 바뀌었을 수도 있으므로 origin/HEAD를 다시 잡는다.
            _run(["remote", "set-head", "origin", "--auto"], cwd=dest, token=token)
            _run(["reset", "--hard", "origin/HEAD"], cwd=dest, token=token)
            _run(["clean", "-fdq"], cwd=dest, token=token)
            return dest
        except GitError as error:
            # 클론이 깨졌다(이전 클론이 타임아웃으로 죽었거나 디스크가 찼거나).
            # 그대로 두면 재인덱싱을 몇 번 해도 같은 자리에서 실패해 영영 못 고친다.
            logger.warning("클론이 손상되어 새로 받습니다 (%s): %s", dest, error)
            shutil.rmtree(dest, ignore_errors=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(["clone", url, str(dest)], token=token, timeout=_CLONE_TIMEOUT)
    except GitError:
        # 실패한 클론 찌꺼기를 남기면 다음 번에 재사용 경로를 타서 또 실패한다.
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def list_commits(repo_dir: Path) -> list[CommitInfo]:
    """기본 브랜치의 전체 커밋을 최신순으로 돌려준다.

    본문(%b)을 맨 뒤에 두고 앞에서부터 4번만 자른다. 커밋 메시지에 구분자로 쓴 제어문자가
    섞여 있어도 필드 개수가 어긋나지 않는다. 고객 레포의 메시지는 무엇이든 들어올 수 있고,
    한 커밋 때문에 그 레포 전체가 영영 인덱싱되지 않으면 안 된다.
    """
    fmt = _FIELD_SEP.join(["%H", "%s", "%an", "%aI", "%b"]) + _RECORD_SEP
    out = _run(["log", f"--format={fmt}"], cwd=repo_dir)

    commits = []
    for record in out.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(_FIELD_SEP, 4)
        if len(parts) != 5:
            logger.warning("커밋 레코드 형식이 어긋나 건너뜁니다: %s", record[:80])
            continue
        sha, title, author, iso_date, body = parts
        commits.append(
            CommitInfo(
                sha=sha,
                title=title,
                body=body.strip() or "",
                author=author,
                committed_at=datetime.fromisoformat(iso_date),
            )
        )
    return commits


def line_history(repo_dir: Path, path: str, start: int, end: int) -> list[str]:
    """path의 start~end 줄을 건드린 커밋 SHA를 최신순으로 돌려준다.

    파일이 이력에 없거나 줄 범위가 현재 파일을 벗어나면 git이 실패한다.
    심볼 위치는 파서가 준 값이라 인덱싱 시점과 어긋날 수 있으므로,
    수집 전체를 중단시키지 않고 빈 목록으로 넘긴다.
    """
    try:
        out = _run(["log", "-L", f"{start},{end}:{path}", "--format=%H", "--no-patch"], cwd=repo_dir)
    except GitError as error:
        # 조용히 넘기면 타임아웃·저장소 손상도 "이력 없음"과 구분되지 않는다.
        logger.warning("줄 범위 이력 조회 실패 (%s:%d-%d): %s", path, start, end, error)
        return []

    # -L은 같은 커밋을 여러 hunk로 중복 출력할 수 있다. 순서를 지키며 중복만 제거한다.
    seen: dict[str, None] = {}
    for line in out.splitlines():
        sha = line.strip()
        if sha:
            seen.setdefault(sha, None)
    return list(seen)
