"""파싱 결과를 DB에 저장하는 단계 (이슈 #14)."""

import json

import pytest

from src.db.models import Organization, Reference, Repo, SourceFile, Symbol
from src.indexing.parsing import parse_repo

PAYMENT = '''from src.config import TIMEOUT


def process_payment(order_id, amount, retry=3):
    return _send(order_id)


def _send(oid):
    pass
'''

MIDDLEWARE = '''from src.payment import process_payment


def require_login(request):
    return process_payment(request.order_id, request.amount)


def audit(request):
    return process_payment(0, 0)
'''

# 같은 이름이 두 파일에 있으면 호출 대상을 특정할 수 없다.
DUP_A = "def handle():\n    pass\n"
DUP_B = "def handle():\n    pass\n"
CALLER = "def run():\n    return handle()\n"


@pytest.fixture
def repo_row(db_session):
    org = Organization(name="테스트", slug="test-org")
    db_session.add(org)
    db_session.flush()
    repo = Repo(organization_id=org.id, name="demo", github_full_name="acme/demo")
    db_session.add(repo)
    db_session.commit()
    return repo


def write(root, files: dict[str, str]):
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_지원_언어가_아닌_파일도_저장한다(db_session, repo_row, tmp_path):
    """파일 트리와 코드 뷰어는 README도 보여줘야 한다."""
    write(tmp_path, {"src/payment.py": PAYMENT, "README.md": "# 문서\n"})

    parse_repo(db_session, repo_row, tmp_path)

    files = {f.path: f.language for f in db_session.query(SourceFile).all()}
    assert files == {"src/payment.py": "python", "README.md": None}


def test_심볼과_참조를_저장한다(db_session, repo_row, tmp_path):
    write(tmp_path, {"src/payment.py": PAYMENT, "src/middleware.py": MIDDLEWARE})

    symbols, references = parse_repo(db_session, repo_row, tmp_path)

    assert symbols == 4  # process_payment, _send, require_login, audit
    idents = {s.ident for s in db_session.query(Symbol).all()}
    assert "src/payment.py::process_payment" in idents

    rows = db_session.query(Reference).all()
    assert references == len(rows)
    targets = {r.target_ident for r in rows}
    assert "src/payment.py::process_payment" in targets


def test_파라미터를_순서대로_보관한다(db_session, repo_row, tmp_path):
    """PR 시그니처 판별이 이 값을 비교한다."""
    write(tmp_path, {"src/payment.py": PAYMENT})

    parse_repo(db_session, repo_row, tmp_path)

    row = db_session.query(Symbol).filter_by(name="process_payment").one()
    assert json.loads(row.params) == ["order_id", "amount", "retry"]


def test_참조_횟수를_집계한다(db_session, repo_row, tmp_path):
    """그래프가 15개를 넘으면 이 값으로 정렬해 자른다."""
    write(tmp_path, {"src/payment.py": PAYMENT, "src/middleware.py": MIDDLEWARE})

    parse_repo(db_session, repo_row, tmp_path)

    counts = {s.name: s.reference_count for s in db_session.query(Symbol).all()}
    assert counts["process_payment"] == 2  # require_login, audit에서 호출
    assert counts["require_login"] == 0


def test_이름이_겹치면_저장하지_않는다(db_session, repo_row, tmp_path):
    """대상을 특정할 수 없는 관계를 지어내지 않는다."""
    write(tmp_path, {"a.py": DUP_A, "b.py": DUP_B, "caller.py": CALLER})

    _, references = parse_repo(db_session, repo_row, tmp_path)

    assert references == 0
    assert db_session.query(Reference).count() == 0


def test_외부_라이브러리_호출은_저장하지_않는다(db_session, repo_row, tmp_path):
    write(tmp_path, {"src/x.py": "import httpx\n\n\ndef fetch():\n    return httpx.get('/')\n"})

    _, references = parse_repo(db_session, repo_row, tmp_path)

    assert references == 0


def test_재인덱싱하면_이전_결과가_남지_않는다(db_session, repo_row, tmp_path):
    """사라진 함수가 그래프에 남으면 없는 관계를 보여주게 된다."""
    write(tmp_path, {"src/payment.py": PAYMENT, "src/middleware.py": MIDDLEWARE})
    parse_repo(db_session, repo_row, tmp_path)

    (tmp_path / "src" / "middleware.py").unlink()
    parse_repo(db_session, repo_row, tmp_path)

    paths = {f.path for f in db_session.query(SourceFile).all()}
    assert paths == {"src/payment.py"}
    assert not db_session.query(Symbol).filter_by(name="require_login").all()

    # 남는 참조는 payment.py 안의 호출뿐이다. 사라진 파일의 참조는 없어야 한다.
    assert {r.path for r in db_session.query(Reference).all()} == {"src/payment.py"}
    assert db_session.query(Symbol).filter_by(name="process_payment").one().reference_count == 0


def test_이름이_겹치는_정의가_있어도_인덱싱이_끝난다(db_session, repo_row, tmp_path):
    """Symbol에 UniqueConstraint(repo_id, ident)가 있어 겹치면 인덱싱 전체가 실패했다."""
    overload = (
        "from typing import overload\n\n\n"
        "@overload\n"
        "def send(x: int) -> int: ...\n\n\n"
        "@overload\n"
        "def send(x: str) -> str: ...\n\n\n"
        "def send(x):\n"
        "    return x\n"
    )
    write(tmp_path, {"src/payment.py": PAYMENT, "src/overload.py": overload})

    parse_repo(db_session, repo_row, tmp_path)

    rows = db_session.query(Symbol).filter_by(name="send").all()
    assert len(rows) == 1
    # 마지막 정의(구현부)가 남는다. @overload 스텁이 아니다.
    assert json.loads(rows[0].params) == ["x"]
    assert db_session.query(Symbol).filter_by(name="process_payment").all()


def test_파싱이_실패해도_이전_인덱스가_남는다(db_session, repo_row, tmp_path, monkeypatch):
    """지우고 커밋한 뒤 넣으면, 실패 시 이전 인덱스까지 날아가 재인덱싱으로도 복구되지 않았다."""
    write(tmp_path, {"src/payment.py": PAYMENT, "src/middleware.py": MIDDLEWARE})
    parse_repo(db_session, repo_row, tmp_path)
    before = {s.ident for s in db_session.query(Symbol).all()}

    def boom(*args, **kwargs):
        raise RuntimeError("파싱 도중 실패")

    monkeypatch.setattr("src.indexing.parsing._update_reference_counts", boom)

    with pytest.raises(RuntimeError):
        parse_repo(db_session, repo_row, tmp_path)
    db_session.rollback()  # 러너의 _mark_failed가 하는 일

    assert {s.ident for s in db_session.query(Symbol).all()} == before
    assert db_session.query(SourceFile).count() == 2


def test_의존성_디렉터리는_건너뛴다(db_session, repo_row, tmp_path):
    """남의 코드까지 넣으면 그래프가 의미를 잃는다."""
    write(tmp_path, {"src/payment.py": PAYMENT, "node_modules/pkg/index.js": "export const a = 1\n"})

    parse_repo(db_session, repo_row, tmp_path)

    paths = {f.path for f in db_session.query(SourceFile).all()}
    assert paths == {"src/payment.py"}


def test_문법_오류가_있어도_나머지를_인덱싱한다(db_session, repo_row, tmp_path):
    """작업 중인 파일 하나 때문에 인덱싱 전체가 실패하면 안 된다."""
    write(tmp_path, {"broken.py": "def f(:\n", "src/payment.py": PAYMENT})

    parse_repo(db_session, repo_row, tmp_path)

    assert db_session.query(Symbol).filter_by(name="process_payment").all()


def test_레포_통계를_채운다(db_session, repo_row, tmp_path):
    """대시보드 카드에 그대로 표시된다."""
    write(tmp_path, {"src/payment.py": PAYMENT, "README.md": "# 문서\n"})

    parse_repo(db_session, repo_row, tmp_path)

    assert repo_row.files_count == 2
    assert repo_row.functions_count == 2
