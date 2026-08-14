"""코드 탐색기 API를 검사한다. docs/api-spec.md §4."""

import pytest

from src.auth import create_token
from src.db.models import Organization, QueryLog, Reference, Repo, SourceFile, Symbol, User


@pytest.fixture
def setup(db_session):
    org = Organization(name="에이크미", slug="acme")
    db_session.add(org)
    db_session.flush()
    user = User(
        organization_id=org.id, email="a@acme.dev", password_hash="x", name="김신입", role="member"
    )
    db_session.add(user)
    repo = Repo(
        organization_id=org.id, name="api", github_full_name="acme/api", indexing_status="done"
    )
    db_session.add(repo)
    db_session.commit()
    return org, user, repo


def _auth(user, read_only=False):
    return {"Authorization": f"Bearer {create_token(user, read_only=read_only)}"}


def _add_file(db_session, repo, path, *, language="python", content="x = 1\n"):
    db_session.add(
        SourceFile(
            organization_id=repo.organization_id,
            repo_id=repo.id,
            path=path,
            language=language,
            content=content,
        )
    )
    db_session.commit()


def _add_symbol(db_session, repo, ident, *, kind="function", start=1, end=10, count=0):
    path, name = ident.split("::")
    db_session.add(
        Symbol(
            organization_id=repo.organization_id,
            repo_id=repo.id,
            ident=ident,
            name=name,
            path=path,
            kind=kind,
            start_line=start,
            end_line=end,
            reference_count=count,
        )
    )
    db_session.commit()


def _add_ref(db_session, repo, source, target, ref_type="call"):
    db_session.add(
        Reference(
            organization_id=repo.organization_id,
            repo_id=repo.id,
            source_ident=source,
            target_ident=target,
            ref_type=ref_type,
            path=source.split("::")[0],
            line=1,
        )
    )
    db_session.commit()


# ── 파일 트리 ──────────────────────────────────────────────────────────────


def test_트리를_디렉터리_구조로_돌려준다(client, db_session, setup):
    _, user, repo = setup
    _add_file(db_session, repo, "src/payment.py")
    _add_file(db_session, repo, "src/pg/client.py")
    _add_file(db_session, repo, "README.md", language=None)

    res = client.get(f"/api/v1/repos/{repo.id}/tree", headers=_auth(user))

    assert res.status_code == 200
    root = res.json()["root"]
    # 디렉터리가 먼저, 그다음 이름순 (api-spec §4의 예시 순서)
    assert [n["name"] for n in root] == ["src", "README.md"]
    assert root[1] == {"path": "README.md", "name": "README.md", "type": "file", "language": None}

    src = root[0]
    assert src["type"] == "dir"
    assert [n["name"] for n in src["children"]] == ["pg", "payment.py"]
    assert src["children"][0]["children"][0]["path"] == "src/pg/client.py"


def test_파일_노드에는_children이_없다(client, db_session, setup):
    """프론트 목데이터와 형태가 어긋나면 안 된다."""
    _, user, repo = setup
    _add_file(db_session, repo, "main.py")

    node = client.get(f"/api/v1/repos/{repo.id}/tree", headers=_auth(user)).json()["root"][0]

    assert "children" not in node
    assert node["language"] == "python"


def test_인덱싱_중이면_트리를_주지_않는다(client, db_session, setup):
    _, user, repo = setup
    repo.indexing_status = "parsing"
    db_session.commit()

    res = client.get(f"/api/v1/repos/{repo.id}/tree", headers=_auth(user))

    assert res.status_code == 409


def test_다른_조직의_트리는_보이지_않는다(client, db_session, setup):
    _, user, repo = setup
    other = Organization(name="남의회사", slug="other")
    db_session.add(other)
    db_session.flush()
    other_repo = Repo(
        organization_id=other.id, name="x", github_full_name="other/x", indexing_status="done"
    )
    db_session.add(other_repo)
    db_session.commit()

    res = client.get(f"/api/v1/repos/{other_repo.id}/tree", headers=_auth(user))

    assert res.status_code == 404


# ── 파일 내용 ──────────────────────────────────────────────────────────────


def test_파일_내용과_함수_범위를_돌려준다(client, db_session, setup):
    _, user, repo = setup
    _add_file(db_session, repo, "src/payment.py", content="def refund():\n    pass\n")
    _add_symbol(db_session, repo, "src/payment.py::refund", start=1, end=2)
    _add_symbol(db_session, repo, "src/payment.py::process", start=5, end=9)

    body = client.get(
        f"/api/v1/repos/{repo.id}/file", params={"path": "src/payment.py"}, headers=_auth(user)
    ).json()

    assert body["content"] == "def refund():\n    pass\n"
    assert body["language"] == "python"
    assert body["truncated"] is False
    # 뷰어가 위에서부터 읽으므로 줄 번호순이다
    assert [f["name"] for f in body["functions"]] == ["refund", "process"]


def test_클래스도_클릭할_수_있게_담는다(client, db_session, setup):
    """함수가 없는 모델 파일에서 목록이 비면 갈 곳이 없다 (이슈 #25)."""
    _, user, repo = setup
    _add_file(db_session, repo, "src/models/refund.py", content="@dataclass\nclass Refund:\n    id: int\n")
    _add_symbol(db_session, repo, "src/models/refund.py::Refund", kind="class", start=1, end=3)

    body = client.get(
        f"/api/v1/repos/{repo.id}/file",
        params={"path": "src/models/refund.py"},
        headers=_auth(user),
    ).json()

    assert body["functions"] == [
        {"name": "Refund", "start_line": 1, "end_line": 3, "kind": "class"}
    ]


def test_상수는_담지_않는다(client, db_session, setup):
    """클릭해서 볼 본문이 없다."""
    _, user, repo = setup
    _add_file(db_session, repo, "src/config.py", content="TIMEOUT = 10\n")
    _add_symbol(db_session, repo, "src/config.py::TIMEOUT", kind="constant", start=1, end=1)

    body = client.get(
        f"/api/v1/repos/{repo.id}/file", params={"path": "src/config.py"}, headers=_auth(user)
    ).json()

    assert body["functions"] == []


def test_함수_항목은_kind를_function으로_준다():
    """기존 항목의 모양이 바뀌면 프론트가 깨진다. 필드만 늘어야 한다."""
    from src.schemas import FileFunctionOut

    item = FileFunctionOut(name="refund", start_line=1, end_line=2)
    assert item.kind == "function"


def test_없는_파일은_404(client, db_session, setup):
    _, user, repo = setup

    res = client.get(
        f"/api/v1/repos/{repo.id}/file", params={"path": "src/none.py"}, headers=_auth(user)
    )

    assert res.status_code == 404


def test_잘린_파일은_truncated로_알린다(client, db_session, setup):
    """뷰어 하단에 "일부만 표시됨"을 띄우는 근거다."""
    from src.indexing.parsing import MAX_FILE_BYTES

    _, user, repo = setup
    _add_file(db_session, repo, "big.py", content="a" * MAX_FILE_BYTES)

    body = client.get(
        f"/api/v1/repos/{repo.id}/file", params={"path": "big.py"}, headers=_auth(user)
    ).json()

    assert body["truncated"] is True


# ── 영향 범위 그래프 ───────────────────────────────────────────────────────


@pytest.fixture
def graph_repo(db_session, setup):
    """checkout → process_payment → PgClient.request, renew → checkout (api-spec §4 예시)."""
    _, user, repo = setup
    for ident, count in [
        ("src/payment.py::process_payment", 12),
        ("src/api/checkout.py::checkout", 12),
        ("src/pg/client.py::PgClient.request", 3),
        ("src/api/subscribe.py::renew", 1),
        ("src/unrelated.py::far_away", 0),
    ]:
        _add_symbol(db_session, repo, ident, count=count)

    _add_ref(db_session, repo, "src/api/checkout.py::checkout", "src/payment.py::process_payment")
    _add_ref(db_session, repo, "src/payment.py::process_payment", "src/pg/client.py::PgClient.request")
    _add_ref(db_session, repo, "src/api/subscribe.py::renew", "src/api/checkout.py::checkout")
    # 깊이 3이라 나오면 안 되는 연결
    _add_ref(db_session, repo, "src/unrelated.py::far_away", "src/api/subscribe.py::renew")
    return user, repo


def _graph(client, user, repo, path="src/payment.py", function="process_payment"):
    return client.get(
        f"/api/v1/repos/{repo.id}/graph",
        params={"path": path, "function": function},
        headers=_auth(user),
    )


def test_깊이_2까지_양방향으로_훑는다(client, graph_repo):
    user, repo = graph_repo

    body = _graph(client, user, repo).json()

    assert body["root"]["id"] == "src/payment.py::process_payment"
    nodes = {n["id"]: n for n in body["nodes"]}
    assert set(nodes) == {
        "src/api/checkout.py::checkout",
        "src/pg/client.py::PgClient.request",
        "src/api/subscribe.py::renew",
    }
    assert nodes["src/api/checkout.py::checkout"]["depth"] == 1
    assert nodes["src/api/checkout.py::checkout"]["direction"] == "caller"
    assert nodes["src/pg/client.py::PgClient.request"]["direction"] == "callee"
    # 2단계는 1단계에서 온 방향을 그대로 물려받는다
    assert nodes["src/api/subscribe.py::renew"]["depth"] == 2
    assert nodes["src/api/subscribe.py::renew"]["direction"] == "caller"


def test_깊이_3은_넘어가지_않는다(client, graph_repo):
    user, repo = graph_repo

    body = _graph(client, user, repo).json()

    assert "src/unrelated.py::far_away" not in {n["id"] for n in body["nodes"]}
    assert body["total_nodes"] == 3
    assert body["truncated"] is False


def test_노드에_참조_횟수를_담는다(client, graph_repo):
    """15개 초과 접기의 정렬 기준이다 (S-TQFUEH)."""
    user, repo = graph_repo

    nodes = {n["id"]: n for n in _graph(client, user, repo).json()["nodes"]}

    assert nodes["src/api/checkout.py::checkout"]["reference_count"] == 12
    assert nodes["src/api/subscribe.py::renew"]["reference_count"] == 1


def test_간선은_저장된_방향_그대로_준다(client, graph_repo):
    user, repo = graph_repo

    edges = {(e["source"], e["target"], e["type"]) for e in _graph(client, user, repo).json()["edges"]}

    assert edges == {
        ("src/api/checkout.py::checkout", "src/payment.py::process_payment", "call"),
        ("src/payment.py::process_payment", "src/pg/client.py::PgClient.request", "call"),
        ("src/api/subscribe.py::renew", "src/api/checkout.py::checkout", "call"),
    }


def test_순환_참조가_있어도_끝난다(client, db_session, setup):
    _, user, repo = setup
    _add_symbol(db_session, repo, "a.py::one")
    _add_symbol(db_session, repo, "b.py::two")
    _add_ref(db_session, repo, "a.py::one", "b.py::two")
    _add_ref(db_session, repo, "b.py::two", "a.py::one")

    body = _graph(client, user, repo, path="a.py", function="one").json()

    assert [n["id"] for n in body["nodes"]] == ["b.py::two"]


def test_import_근거는_그래프에_올리지_않는다(client, db_session, setup):
    """출발점이 심볼이 아니라 파일이다. 노드 kind에 모듈이 없다 (이슈 #20 결정)."""
    _, user, repo = setup
    _add_symbol(db_session, repo, "src/payment.py::handle")
    _add_ref(db_session, repo, "src/api.py::<module>", "src/payment.py::handle", ref_type="import")

    body = _graph(client, user, repo, path="src/payment.py", function="handle").json()

    assert body["nodes"] == []
    assert body["edges"] == []


def test_상한을_넘으면_잘라내고_알린다(client, db_session, setup):
    from src.api.explorer import MAX_GRAPH_NODES

    _, user, repo = setup
    _add_symbol(db_session, repo, "src/payment.py::process")
    for i in range(MAX_GRAPH_NODES + 10):
        _add_symbol(db_session, repo, f"src/caller{i}.py::call{i}", count=i)
        _add_ref(db_session, repo, f"src/caller{i}.py::call{i}", "src/payment.py::process")

    body = _graph(client, user, repo, path="src/payment.py", function="process").json()

    assert body["total_nodes"] == MAX_GRAPH_NODES
    assert body["truncated"] is True
    # 많이 참조되는 것부터 남는다
    assert body["nodes"][0]["reference_count"] == MAX_GRAPH_NODES + 9


def test_없는_함수는_404(client, db_session, setup):
    _, user, repo = setup

    assert _graph(client, user, repo).status_code == 404


def test_그래프_조회를_질의_이력에_남긴다(client, db_session, graph_repo):
    """api-spec §6의 action=graph_view."""
    user, repo = graph_repo

    _graph(client, user, repo)

    log = db_session.query(QueryLog).one()
    assert log.action == "graph_view"
    assert log.target == "src/payment.py::process_payment"


def test_데모_세션도_그래프를_읽는다(client, graph_repo):
    """읽기 전용이지 조회 금지가 아니다 (🚫데모 표시 없음)."""
    user, repo = graph_repo

    res = client.get(
        f"/api/v1/repos/{repo.id}/graph",
        params={"path": "src/payment.py", "function": "process_payment"},
        headers=_auth(user, read_only=True),
    )

    assert res.status_code == 200
