"""git log -L 기반 이력 조회를 검사한다 (#27).

git을 모킹하지 않는다. 임시 저장소를 실제로 만들어 커밋을 쌓고 돌린다.
이 모듈의 값어치가 "git이 무엇을 돌려주는가"에 있어서, 모킹하면 검증이 사라진다.
"""

import subprocess

import pytest

from src.github import git_history


def _git(repo_dir, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _write(repo_dir, name, text):
    (repo_dir / name).write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """포맷팅 커밋이 실제 변경을 덮어쓴 저장소.

    git blame이면 마지막 포맷팅 커밋만 보이고, log -L이면 그 아래 이력까지 보인다.
    데모 레포의 verify_token이 겪은 상황과 같다.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "dev@acme.dev")
    _git(repo_dir, "config", "user.name", "kimdev")

    _write(repo_dir, "auth.py", "def verify(token):\n    return decode(token)\n")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "feat: 토큰 검증 추가 (#143)")

    _write(repo_dir, "auth.py", "def verify(token):\n    return decode(token, algorithms=['HS256'])\n")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "fix: 알고리즘 화이트리스트 강제 (#201)")

    # 같은 줄을 다시 건드리는 포맷팅 커밋. blame이었다면 위 두 개를 가린다.
    _write(repo_dir, "auth.py", 'def verify(token):\n    return decode(token, algorithms=["HS256"])\n')
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "chore: 전체 코드 포맷팅 적용")

    # 다른 파일의 커밋. 위 줄 범위 이력에 섞여 나오면 안 된다.
    _write(repo_dir, "other.py", "x = 1\n")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "chore: 무관한 파일 추가")

    return repo_dir


def test_커밋을_최신순으로_읽는다(repo):
    commits = git_history.list_commits(repo)

    assert [c.title for c in commits] == [
        "chore: 무관한 파일 추가",
        "chore: 전체 코드 포맷팅 적용",
        "fix: 알고리즘 화이트리스트 강제 (#201)",
        "feat: 토큰 검증 추가 (#143)",
    ]
    assert all(c.author == "kimdev" for c in commits)
    assert all(len(c.sha) == 40 for c in commits)
    assert commits[0].committed_at.tzinfo is not None


def test_줄_범위_이력이_포맷팅에_가려진_커밋까지_보여준다(repo):
    """#27의 핵심. blame이면 맨 위 포맷팅 커밋 하나만 남는다."""
    shas = git_history.line_history(repo, "auth.py", 1, 2)
    titles = {c.sha: c.title for c in git_history.list_commits(repo)}
    found = [titles[sha] for sha in shas]

    assert "fix: 알고리즘 화이트리스트 강제 (#201)" in found
    assert "feat: 토큰 검증 추가 (#143)" in found
    assert "chore: 무관한 파일 추가" not in found


def test_없는_파일은_수집을_멈추지_않고_빈_목록을_준다(repo):
    """심볼 위치는 파서가 준 값이라 인덱싱 시점과 어긋날 수 있다."""
    assert git_history.line_history(repo, "does_not_exist.py", 1, 5) == []


def test_커밋_본문의_줄바꿈이_레코드를_깨지_않는다(repo):
    _write(repo, "auth.py", "def verify(token):\n    return None\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fix: 제목\n\n본문 첫 줄\n본문 둘째 줄")

    latest = git_history.list_commits(repo)[0]

    assert latest.title == "fix: 제목"
    assert "본문 첫 줄" in latest.body
    assert "본문 둘째 줄" in latest.body


def test_토큰은_클론_주소에만_들어가고_에러에는_남지_않는다(tmp_path, monkeypatch):
    """실패 메시지에 자격증명이 새면 로그에 그대로 남는다.

    네트워크를 타지 않는다(이슈 #15: 테스트는 실제 네트워크 없이 통과해야 한다).
    존재하지 않는 로컬 경로로 클론을 실패시켜 같은 경로를 확인한다.
    """
    url = git_history.clone_url("acme/repo", "ghs_secret")
    assert url == "https://x-access-token:ghs_secret@github.com/acme/repo.git"

    missing = tmp_path / "nowhere"
    monkeypatch.setattr(git_history, "clone_url", lambda full_name, token: f"{missing}#{token}")

    with pytest.raises(git_history.GitError) as error:
        git_history.ensure_clone("acme/does-not-exist", "ghs_secret", tmp_path / "clone")

    assert "ghs_secret" not in str(error.value)


def test_재사용하는_클론이_원격_최신으로_맞춰진다(tmp_path, monkeypatch):
    """fetch만 하면 HEAD가 옛 커밋에 남아 재인덱싱이 옛 데이터를 다시 저장한다."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-b", "main")
    _git(upstream, "config", "user.email", "dev@acme.dev")
    _git(upstream, "config", "user.name", "kimdev")
    _write(upstream, "f.py", "x = 1\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "c1")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))

    _write(upstream, "f.py", "x = 2\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "c2")

    # 클론이 이미 있는 경로를 탄다. 원격은 로컬 upstream으로 둔다(네트워크 없이).
    monkeypatch.setattr(git_history, "clone_url", lambda full_name, token: str(upstream))
    git_history.ensure_clone("acme/repo", "tok", clone)

    assert [c.title for c in git_history.list_commits(clone)] == ["c2", "c1"]
