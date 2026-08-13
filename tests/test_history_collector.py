"""커밋·PR 수집과 심볼 근거 연결을 검사한다 (#27).

git은 실제로 돌리고(임시 저장소), GitHub API만 모킹한다.
근거 연결이 맞는지가 git log -L 결과에 달려 있어서 git을 가짜로 두면 검증이 안 된다.
"""

import json
import subprocess

import pytest

from src.db.models import Commit, Organization, PullRequest, Repo, Symbol, SymbolEvidence
from src.indexing import history


def _index(db_session, repo_row, installation_id: int = 1):
    """수집 → 근거 연결 → 요약을 이어서 실행한다.

    실제 인덱싱(run_indexing)은 수집과 근거 연결 사이에 파싱을 끼워 심볼을 만든다.
    심볼을 직접 넣어 검사하는 테스트에서는 그 단계만 건너뛴다.
    """
    context = history.collect_repo_history(db_session, repo_row, installation_id=installation_id)
    history.link_symbol_evidence(db_session, repo_row, context)
    history.generate_repo_summaries(db_session, repo_row)
    return context


def _git(repo_dir, *args):
    subprocess.run(
        ["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


@pytest.fixture
def source_repo(tmp_path):
    """인덱싱 대상이 될 가짜 원본 저장소."""
    repo_dir = tmp_path / "source"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "dev@acme.dev")
    _git(repo_dir, "config", "user.name", "kimdev")

    (repo_dir / "auth.py").write_text("def verify(token):\n    return decode(token)\n", encoding="utf-8")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "feat: JWT 검증 분리 (#143)")

    (repo_dir / "auth.py").write_text(
        "def verify(token):\n    return decode(token, algorithms=['HS256'])\n", encoding="utf-8"
    )
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "fix: 알고리즘 화이트리스트 강제 (#201)")

    (repo_dir / "unrelated.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "chore: 무관한 변경")
    return repo_dir


@pytest.fixture
def repo_row(db_session):
    org = Organization(name="에이크미", slug="acme", github_installation_id=1, github_account="acme")
    db_session.add(org)
    db_session.flush()
    repo = Repo(organization_id=org.id, name="api", github_full_name="acme/api", indexing_status="collecting")
    db_session.add(repo)
    db_session.commit()
    return repo


@pytest.fixture
def fake_github(monkeypatch, source_repo, tmp_path):
    """클론은 로컬 복사로, API는 고정 응답으로 바꾼다."""
    monkeypatch.setattr(history, "get_installation_token", lambda installation_id: "tok")
    monkeypatch.setattr(history, "_clone_root", lambda: tmp_path / "clones")

    def fake_clone(full_name, token, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not (dest / ".git").is_dir():
            _git(source_repo.parent, "clone", "-q", str(source_repo), str(dest))
        return dest

    monkeypatch.setattr(history.git_history, "ensure_clone", fake_clone)

    # 요약 단계가 진짜 OpenAI를 부르면 테스트가 네트워크와 비용에 묶인다.
    # 이 파일은 근거 연결을 검증하므로 요약은 형식만 맞는 응답으로 대신한다.
    monkeypatch.setattr(
        "src.llm.summarizer.complete",
        lambda system, user: json.dumps(
            {
                "status": "ok",
                "sentences": [{"text": "근거에 따라 변경됐다.", "evidence": ["e1"]}],
                "used": ["e1"],
                "dropped": [],
                "conflict_sides": None,
            },
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(
        history,
        "get_repo",
        lambda installation_id, full_name: {"language": "Python", "default_branch": "develop"},
    )
    monkeypatch.setattr(
        history,
        "list_pull_requests",
        lambda installation_id, full_name: [
            {
                "number": 201,
                "title": "토큰 검증 강화",
                "body": "알고리즘을 고정한다",
                "user": {"login": "kimdev"},
                "html_url": "https://github.com/acme/api/pull/201",
                "merged_at": "2026-01-02T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        history,
        "list_pr_comments",
        lambda installation_id, full_name, number: [
            {"body": "확인"},
            {"body": "알고리즘을 고정하지 않으면 none 서명으로 우회할 수 있습니다"},
        ],
    )
    # PR에 어떤 커밋이 들었는지는 GitHub만 안다. 기본은 빈 목록(제목 매칭으로 넘어감).
    monkeypatch.setattr(history, "list_pr_commits", lambda installation_id, full_name, number: [])


def test_커밋을_모두_저장한다(db_session, repo_row, fake_github):
    _index(db_session, repo_row)

    commits = db_session.query(Commit).all()
    assert len(commits) == 3
    assert repo_row.commits_count == 3
    assert all(c.organization_id == repo_row.organization_id for c in commits)
    assert all(c.url.startswith("https://github.com/acme/api/commit/") for c in commits)


def test_PR과_리뷰_코멘트를_저장한다(db_session, repo_row, fake_github):
    _index(db_session, repo_row)

    pr = db_session.query(PullRequest).one()
    assert pr.number == 201
    assert pr.author == "kimdev"
    # 짧은 응답("확인")이 아니라 이유가 담긴 코멘트를 골라야 한다.
    assert "none 서명" in pr.review_excerpt
    # 발췌는 하나지만 개수는 전부 세야 대시보드 합산이 맞는다.
    assert pr.review_comment_count == 2
    assert repo_row.prs_count == 1


def test_대표_언어와_기본_브랜치를_GitHub에서_채운다(db_session, repo_row, fake_github):
    """등록 시점엔 레포 이름만 받으므로 수집 단계에서 채워야 한다."""
    assert repo_row.language is None
    assert repo_row.default_branch == "main"

    _index(db_session, repo_row)

    assert repo_row.language == "Python"
    assert repo_row.default_branch == "develop"


def test_심볼_근거에_포맷팅에_가려질_커밋들이_모두_연결된다(db_session, repo_row, fake_github):
    """#27 완료 조건: verify_token 근거에 #201과 #143이 모두 포함된다."""
    db_session.add(
        Symbol(
            organization_id=repo_row.organization_id,
            repo_id=repo_row.id,
            ident="auth.py::verify",
            name="verify",
            path="auth.py",
            kind="function",
            start_line=1,
            end_line=2,
        )
    )
    db_session.commit()

    _index(db_session, repo_row)

    evidences = db_session.query(SymbolEvidence).filter_by(symbol_ident="auth.py::verify").all()
    commit_titles = {
        db_session.get(Commit, e.commit_id).title for e in evidences if e.kind == "commit"
    }

    assert "fix: 알고리즘 화이트리스트 강제 (#201)" in commit_titles
    assert "feat: JWT 검증 분리 (#143)" in commit_titles
    assert "chore: 무관한 변경" not in commit_titles


def test_커밋_제목의_PR_번호로_PR_근거도_연결된다(db_session, repo_row, fake_github):
    db_session.add(
        Symbol(
            organization_id=repo_row.organization_id,
            repo_id=repo_row.id,
            ident="auth.py::verify",
            name="verify",
            path="auth.py",
            kind="function",
            start_line=1,
            end_line=2,
        )
    )
    db_session.commit()

    _index(db_session, repo_row)

    pr_evidences = db_session.query(SymbolEvidence).filter_by(kind="pr").all()
    assert len(pr_evidences) == 1
    assert db_session.get(PullRequest, pr_evidences[0].pull_request_id).number == 201


def test_PR_커밋_목록으로도_PR_근거가_붙는다(db_session, repo_row, source_repo, fake_github, monkeypatch):
    """머지 커밋은 git log -L 결과에 안 나오므로 제목 매칭만으로는 PR 근거가 안 붙는다.

    PR의 커밋 목록으로 확정하면 커밋 제목 관례와 무관하게 연결된다.
    """
    db_session.add(
        Symbol(
            organization_id=repo_row.organization_id,
            repo_id=repo_row.id,
            ident="auth.py::verify",
            name="verify",
            path="auth.py",
            kind="function",
            start_line=1,
            end_line=2,
        )
    )
    db_session.commit()

    # 어떤 커밋 제목도 가리키지 않는 번호라, 제목 매칭으로는 절대 연결되지 않는다.
    monkeypatch.setattr(
        history,
        "list_pull_requests",
        lambda installation_id, full_name: [
            {
                "number": 999,
                "title": "토큰 검증 강화",
                "body": None,
                "user": {"login": "kimdev"},
                "html_url": "https://github.com/acme/api/pull/999",
                "merged_at": None,
            }
        ],
    )

    oldest = subprocess.run(
        ["git", "log", "--format=%H"],
        cwd=source_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[-1]
    monkeypatch.setattr(history, "list_pr_commits", lambda i, f, n: [{"sha": oldest}])

    _index(db_session, repo_row)

    pr_evidences = db_session.query(SymbolEvidence).filter_by(kind="pr").all()
    assert len(pr_evidences) == 1
    assert db_session.get(PullRequest, pr_evidences[0].pull_request_id).number == 999


def test_봇_코멘트는_리뷰_발췌로_뽑지_않는다():
    """CI 봇 리포트는 길이로 사람 코멘트를 이긴다."""
    comments = [
        {"body": "커버리지 리포트 " + "x" * 500, "user": {"type": "Bot"}},
        {"body": "이 분기는 결제 실패 시에도 타는지 확인이 필요합니다", "user": {"type": "User"}},
    ]

    assert history._pick_review_excerpt(comments) == "이 분기는 결제 실패 시에도 타는지 확인이 필요합니다"


def test_심볼이_없으면_근거를_만들지_않는다(db_session, repo_row, fake_github):
    """분석팀 파서(#20)가 아직 안 붙은 상태에서도 수집은 끝나야 한다."""
    _index(db_session, repo_row)

    assert db_session.query(SymbolEvidence).count() == 0
    assert db_session.query(Commit).count() == 3


def test_재수집해도_커밋이_중복되지_않는다(db_session, repo_row, fake_github):
    _index(db_session, repo_row)
    _index(db_session, repo_row)

    assert db_session.query(Commit).count() == 3
    assert db_session.query(PullRequest).count() == 1


def test_커밋_단계_진행률이_커밋_수로_채워진다(db_session, repo_row, source_repo):
    from src.github import git_history as gh

    history._save_commits(db_session, repo_row, gh.list_commits(source_repo))

    assert repo_row.progress_total == 3
    assert repo_row.progress_current == 3


def test_PR_단계도_진행률을_갱신한다(db_session, repo_row, fake_github):
    """PR 단계는 PR당 API 3회라 오래 걸린다. 여기서 멈춰 보이면 안 된다."""
    history._save_pull_requests(db_session, repo_row, installation_id=1)

    # 커밋 수(3)가 아니라 PR 수(1)를 기준으로 다시 잡혀야 한다.
    assert repo_row.progress_total == 1
    assert repo_row.progress_current == 1


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Merge pull request #12 from acme/feat/x", 12),
        ("fix: 토큰 검증에 알고리즘 화이트리스트 강제 (#201)", 201),
        ("chore: PR 번호가 없는 커밋", None),
        ("fix: 본문 중간의 (#99) 표기는 제목 끝이 아니다 아님", None),
    ],
)
def test_커밋_제목에서_PR_번호를_뽑는다(title, expected):
    assert history._pr_number_from_title(title) == expected


def test_짧은_코멘트만_있으면_리뷰_발췌를_남기지_않는다():
    assert history._pick_review_excerpt([{"body": "LGTM"}, {"body": "확인"}]) is None
