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


def calls(db_session):
    """호출 참조만. import 근거는 따로 검사한다."""
    return db_session.query(Reference).filter_by(ref_type="call").all()


def imports(db_session):
    return db_session.query(Reference).filter_by(ref_type="import").all()


def one(rows):
    assert len(rows) == 1, [f"{r.source_ident} → {r.target_ident}" for r in rows]
    return rows[0]


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


def test_import를_알면_이름이_겹쳐도_대상을_확정한다(db_session, repo_row, tmp_path):
    """#14에서 동명이인이라 버려지던 호출이 살아난다 (이슈 #20)."""
    write(
        tmp_path,
        {
            "src/payment.py": "def handle():\n    pass\n",
            "src/legacy.py": "def handle():\n    pass\n",
            "src/api.py": "from src.payment import handle\n\n\ndef run():\n    return handle()\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    row = one(calls(db_session))
    assert row.source_ident == "src/api.py::run"
    assert row.target_ident == "src/payment.py::handle"


def test_상대_import도_같은_방식으로_확정한다(db_session, repo_row, tmp_path):
    write(
        tmp_path,
        {
            "src/payment.py": "def handle():\n    pass\n",
            "src/legacy.py": "def handle():\n    pass\n",
            "src/api.py": "from .payment import handle\n\n\ndef run():\n    return handle()\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    assert one(calls(db_session)).target_ident == "src/payment.py::handle"


def test_모듈을_거친_호출도_확정한다(db_session, repo_row, tmp_path):
    """import src.payment 뒤의 payment.handle() 형태."""
    write(
        tmp_path,
        {
            "src/payment.py": "def handle():\n    pass\n",
            "src/legacy.py": "def handle():\n    pass\n",
            "src/api.py": "from src import payment\n\n\ndef run():\n    return payment.handle()\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    assert one(calls(db_session)).target_ident == "src/payment.py::handle"


def test_같은_파일에_정의가_있으면_그것을_고른다(db_session, repo_row, tmp_path):
    """import하지 않은 이상 파이썬은 자기 파일 것을 부른다."""
    write(
        tmp_path,
        {
            "a.py": "def helper():\n    pass\n\n\ndef run():\n    return helper()\n",
            "b.py": "def helper():\n    pass\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    assert one(calls(db_session)).target_ident == "a.py::helper"


def test_객체를_거친_호출은_같은_파일_규칙을_쓰지_않는다(db_session, repo_row, tmp_path):
    """obj.handle()의 handle은 이 파일의 handle이 아니라 obj의 것이다."""
    write(
        tmp_path,
        {
            "a.py": "def handle():\n    pass\n\n\ndef run(client):\n    return client.handle()\n",
            "b.py": "def handle():\n    pass\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    assert calls(db_session) == []


def test_외부_라이브러리_import는_확정_근거가_되지_못한다(db_session, repo_row, tmp_path):
    """레포에 없는 모듈이라 어느 파일 것인지 알려주지 못한다."""
    write(
        tmp_path,
        {
            "src/payment.py": "def get():\n    pass\n",
            "src/legacy.py": "def get():\n    pass\n",
            "src/api.py": "import httpx\n\n\ndef run():\n    return httpx.get('/')\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    assert calls(db_session) == []


def test_상수_참조를_저장한다(db_session, repo_row, tmp_path):
    """데모 시안의 payment_service → TIMEOUT_SECONDS 연결이 이것이다 (이슈 #22)."""
    write(
        tmp_path,
        {
            "src/config.py": "TIMEOUT_SECONDS = 10\n",
            "src/payment.py": (
                "from src.config import TIMEOUT_SECONDS\n\n\n"
                "def process():\n"
                "    return TIMEOUT_SECONDS\n"
            ),
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    constant = db_session.query(Symbol).filter_by(kind="constant").one()
    assert constant.ident == "src/config.py::TIMEOUT_SECONDS"

    row = one(db_session.query(Reference).filter_by(ref_type="constant").all())
    assert row.source_ident == "src/payment.py::process"
    assert row.target_ident == "src/config.py::TIMEOUT_SECONDS"
    # 그래프의 접기 기준에 상수도 함께 잡힌다
    assert constant.reference_count == 1


def test_상수_참조도_이름이_겹치면_import로_좁힌다(db_session, repo_row, tmp_path):
    write(
        tmp_path,
        {
            "src/config.py": "TIMEOUT = 10\n",
            "src/legacy.py": "TIMEOUT = 99\n",
            "src/payment.py": (
                "from src.config import TIMEOUT\n\n\ndef process():\n    return TIMEOUT\n"
            ),
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    row = one(db_session.query(Reference).filter_by(ref_type="constant").all())
    assert row.target_ident == "src/config.py::TIMEOUT"


# ── TypeScript (이슈 #21) ──────────────────────────────────────────────────


def test_TS의_상대_import로_호출_대상을_확정한다(db_session, repo_row, tmp_path):
    """데모 레포의 RefundListPage → api/refund → api/client 구조다."""
    write(
        tmp_path,
        {
            "admin-web/src/api/client.ts": "export function request(path) { return path }\n",
            "admin-web/src/api/refund.ts": (
                "import { request } from './client'\n\n"
                "export function listRefunds() { return request('/refunds') }\n"
            ),
            "admin-web/src/pages/RefundListPage.tsx": (
                "import { listRefunds } from '../api/refund'\n\n"
                "export default function RefundListPage() { return listRefunds() }\n"
            ),
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    edges = {(r.source_ident, r.target_ident) for r in calls(db_session)}
    assert edges == {
        (
            "admin-web/src/pages/RefundListPage.tsx::RefundListPage",
            "admin-web/src/api/refund.ts::listRefunds",
        ),
        ("admin-web/src/api/refund.ts::listRefunds", "admin-web/src/api/client.ts::request"),
    }


def test_index_파일을_가리키는_import도_찾는다(db_session, repo_row, tmp_path):
    """'./api'는 api/index.ts를 가리킨다. TS/JS에서 흔한 형태다."""
    write(
        tmp_path,
        {
            "src/api/index.ts": "export function request() { return 1 }\n",
            "src/app.ts": "import { request } from './api'\n\nexport function run() { return request() }\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    assert one(calls(db_session)).target_ident == "src/api/index.ts::request"


def test_외부_라이브러리_import는_TS에서도_걸러진다(db_session, repo_row, tmp_path):
    write(
        tmp_path,
        {
            "src/a.ts": "export function useState() { return 1 }\n",
            "src/page.tsx": (
                "import { useState } from 'react'\n\n"
                "export default function P() { return useState() }\n"
            ),
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    # react의 useState다. 레포 안의 동명 함수로 이어지면 없는 관계를 그리는 것이다
    assert calls(db_session) == []
    assert {r.target_ident for r in imports(db_session)} == {"react.useState"}


def test_파이썬과_TS가_한_레포에_섞여도_각자_인덱싱된다(db_session, repo_row, tmp_path):
    write(
        tmp_path,
        {
            "src/payment.py": "def process():\n    pass\n",
            "admin-web/src/api/refund.ts": "export function listRefunds() { return 1 }\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    assert {s.ident for s in db_session.query(Symbol).all()} == {
        "src/payment.py::process",
        "admin-web/src/api/refund.ts::listRefunds",
    }


# ── import 근거 저장 (PRD의 추적 관계 4가지 중 하나) ────────────────────────


def test_레포_안의_import는_대상_심볼까지_이어_저장한다(db_session, repo_row, tmp_path):
    write(
        tmp_path,
        {
            "src/payment.py": "def handle():\n    pass\n",
            "src/api.py": "from src.payment import handle\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    row = one(imports(db_session))
    assert row.source_ident == "src/api.py::<module>"
    assert row.target_ident == "src/payment.py::handle"
    assert row.line == 1


def test_외부_라이브러리_import도_근거로_남긴다(db_session, repo_row, tmp_path):
    """영향 판단의 근거라 버리지 않는다. 다만 그래프 노드가 되지는 않는다."""
    write(tmp_path, {"src/api.py": "import httpx\nfrom fastapi import Depends\n"})

    parse_repo(db_session, repo_row, tmp_path)

    assert {r.target_ident for r in imports(db_session)} == {"httpx", "fastapi.Depends"}


def test_모듈을_들여오면_그_파일을_가리킨다(db_session, repo_row, tmp_path):
    write(
        tmp_path,
        {
            "src/payment.py": "def handle():\n    pass\n",
            "src/api.py": "from src import payment\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    assert one(imports(db_session)).target_ident == "src/payment.py::<module>"


def test_import는_참조_횟수에_섞이지_않는다(db_session, repo_row, tmp_path):
    """그래프에 그려지는 연결만 센다. 섞이면 화면 숫자와 어긋난다."""
    write(
        tmp_path,
        {
            "src/payment.py": "def handle():\n    pass\n",
            "src/api.py": "from src.payment import handle\n\n\ndef run():\n    return handle()\n",
        },
    )

    parse_repo(db_session, repo_row, tmp_path)

    # 호출 1건뿐이다. import 1건은 세지 않는다.
    assert db_session.query(Symbol).filter_by(name="handle").one().reference_count == 1


def test_외부_라이브러리_호출은_저장하지_않는다(db_session, repo_row, tmp_path):
    write(tmp_path, {"src/x.py": "import httpx\n\n\ndef fetch():\n    return httpx.get('/')\n"})

    parse_repo(db_session, repo_row, tmp_path)

    assert calls(db_session) == []


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


def test_큰_파일은_상한까지만_저장한다(db_session, repo_row, tmp_path):
    """통째로 버리면 파일 트리에서 사라져 "일부만 표시됨" 안내조차 못 띄운다."""
    from src.indexing.parsing import MAX_FILE_BYTES

    write(tmp_path, {"big.py": "# " + "a" * MAX_FILE_BYTES})

    parse_repo(db_session, repo_row, tmp_path)

    row = db_session.query(SourceFile).filter_by(path="big.py").one()
    assert len(row.content.encode("utf-8")) == MAX_FILE_BYTES


def test_바이너리_파일은_저장하지_않는다(db_session, repo_row, tmp_path):
    (tmp_path / "logo.json").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe")

    parse_repo(db_session, repo_row, tmp_path)

    assert db_session.query(SourceFile).count() == 0


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


# ── 클래스와 상속 (이슈 #25) ───────────────────────────────────────────────

MODEL_BASE = "class Base:\n    pass\n"

MODEL_REFUND = '''from dataclasses import dataclass

from src.models.base import Base


@dataclass
class Refund(Base):
    id: int
    amount: int
'''

REFUND_SERVICE = '''from src.models.refund import Refund


def make_refund(amount):
    return Refund(id=0, amount=amount)
'''


def test_함수가_없는_모델_파일도_심볼을_갖는다(db_session, repo_row, tmp_path):
    """데모 레포의 src/models/*.py가 정확히 이 모양이다. 비면 막다른 화면이 된다."""
    write(tmp_path, {"src/models/refund.py": MODEL_REFUND})

    parse_repo(db_session, repo_row, tmp_path)

    symbols = db_session.query(Symbol).filter_by(path="src/models/refund.py").all()
    assert [(s.ident, s.kind) for s in symbols] == [("src/models/refund.py::Refund", "class")]


def test_상속을_참조로_저장한다(db_session, repo_row, tmp_path):
    write(tmp_path, {"src/models/base.py": MODEL_BASE, "src/models/refund.py": MODEL_REFUND})

    parse_repo(db_session, repo_row, tmp_path)

    ref = one(db_session.query(Reference).filter_by(ref_type="inheritance").all())
    assert ref.source_ident == "src/models/refund.py::Refund"
    assert ref.target_ident == "src/models/base.py::Base"


def test_상속_대상이_레포_밖이면_저장하지_않는다(db_session, repo_row, tmp_path):
    """BaseModel은 pydantic 것이다. 없는 관계를 그리지 않는다."""
    write(tmp_path, {"src/models/refund.py": "from pydantic import BaseModel\n\n\nclass Refund(BaseModel):\n    pass\n"})

    parse_repo(db_session, repo_row, tmp_path)

    assert db_session.query(Reference).filter_by(ref_type="inheritance").all() == []


def test_클래스를_부르는_호출도_이어진다(db_session, repo_row, tmp_path):
    """Refund(...) 생성자 호출이 클래스 노드로 이어져 모델 파일이 그래프에 붙는다."""
    write(tmp_path, {"src/models/refund.py": MODEL_REFUND, "src/service.py": REFUND_SERVICE})

    parse_repo(db_session, repo_row, tmp_path)

    ref = one(calls(db_session))
    assert ref.source_ident == "src/service.py::make_refund"
    assert ref.target_ident == "src/models/refund.py::Refund"


def test_피참조_횟수에_상속이_들어간다(db_session, repo_row, tmp_path):
    """그래프가 상한에 걸려 자를 때 쓰는 값이다."""
    write(tmp_path, {"src/models/base.py": MODEL_BASE, "src/models/refund.py": MODEL_REFUND})

    parse_repo(db_session, repo_row, tmp_path)

    base = db_session.query(Symbol).filter_by(ident="src/models/base.py::Base").one()
    assert base.reference_count == 1
