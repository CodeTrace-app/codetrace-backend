"""Python 파서. DB 없이 소스 문자열만으로 검사한다."""

from src.parser import get_adapter, language_of
from src.parser.python import PythonAdapter

SOURCE = '''"""결제 처리."""

TIMEOUT = 10


def process_payment(order_id, amount, retry=3):
    """PG사에 결제를 요청한다."""
    _send(order_id)
    return _finish(order_id)


def _send(oid):
    pass


def _finish(oid):
    pass


class PgClient:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def request(self, url: str, timeout: int = 5) -> dict:
        return process_payment(url, 0)

    def retry_request(self, url):
        # 재귀 호출은 그래프 간선으로 저장하지 않는다
        return self.retry_request(url)
'''


def parse(source: str = SOURCE, path: str = "src/payment.py"):
    result = PythonAdapter().parse(path, source)
    return result.symbols, result.references


def parse_imports(source: str, path: str = "src/api/repos.py"):
    return PythonAdapter().parse(path, source).imports


# ── 심볼 추출 ──────────────────────────────────────────────────────────────


def test_모듈_함수와_클래스_메서드를_모두_찾는다():
    symbols, _ = parse()
    assert {s.name for s in symbols if s.kind == "function"} == {
        "process_payment",
        "_send",
        "_finish",
        "PgClient.__init__",
        "PgClient.request",
        "PgClient.retry_request",
    }


def test_식별자는_파일경로와_이름을_잇는다():
    symbols, _ = parse()
    idents = {s.ident for s in symbols}
    assert "src/payment.py::process_payment" in idents
    assert "src/payment.py::PgClient.request" in idents


def test_라인_범위가_1부터_센다():
    symbols, _ = parse()
    fn = next(s for s in symbols if s.name == "process_payment")
    # 소스에서 def process_payment가 6번째 줄이다
    assert fn.start_line == 6
    assert fn.end_line > fn.start_line


# ── 파라미터 (PR 시그니처 판별의 기준) ──────────────────────────────────────


def test_기본값이_있어도_이름만_남긴다():
    symbols, _ = parse()
    fn = next(s for s in symbols if s.name == "process_payment")
    assert fn.params == ["order_id", "amount", "retry"]


def test_self는_파라미터에서_뺀다():
    symbols, _ = parse()
    method = next(s for s in symbols if s.name == "PgClient.request")
    assert method.params == ["url", "timeout"]


def test_타입_힌트가_이름에_섞이지_않는다():
    symbols, _ = parse()
    method = next(s for s in symbols if s.name == "PgClient.__init__")
    assert method.params == ["base_url"]


def test_가변인자도_이름으로_잡는다():
    symbols, _ = parse("def f(a, *args, **kwargs):\n    pass\n")
    assert symbols[0].params == ["a", "args", "kwargs"]


# ── 호출 추출 ──────────────────────────────────────────────────────────────


def test_호출이_일어난_함수를_출발점으로_기록한다():
    _, refs = parse()
    call = next(r for r in refs if r.target_name == "_send")
    assert call.source_ident == "src/payment.py::process_payment"
    assert call.ref_type == "call"


def test_메서드_안의_호출도_그_메서드에서_일어난_것으로_본다():
    _, refs = parse()
    call = next(r for r in refs if r.target_name == "process_payment")
    assert call.source_ident == "src/payment.py::PgClient.request"


def test_재귀_호출은_저장하지_않는다():
    _, refs = parse()
    assert not [r for r in refs if r.target_name == "retry_request"]


def test_중첩_함수는_가장_안쪽을_출발점으로_삼는다():
    source = """
def outer(a):
    def inner(b):
        return helper(b)
    return inner
"""
    _, refs = parse(source, "src/nested.py")
    call = next(r for r in refs if r.target_name == "helper")
    # 중첩 함수의 ident에는 감싼 함수 이름이 붙는다 (클래스 메서드와 같은 규칙)
    assert call.source_ident == "src/nested.py::outer.inner"


# ── 이름이 겹치는 정의 ─────────────────────────────────────────────────────


def test_다른_함수_안의_같은_이름은_서로_다른_심볼이_된다():
    """ident가 겹치면 Symbol 유니크 제약에 걸려 인덱싱 전체가 실패한다."""
    source = """
def test_a():
    def fake_request(method):
        pass
    return fake_request


def test_b():
    def fake_request(method):
        pass
    return fake_request
"""
    symbols, _ = parse(source, "tests/test_x.py")
    idents = [s.ident for s in symbols]
    assert len(idents) == len(set(idents))
    assert "tests/test_x.py::test_a.fake_request" in idents
    assert "tests/test_x.py::test_b.fake_request" in idents


def test_클래스가_다르면_같은_메서드_이름도_구분된다():
    source = """
class A:
    def run(self):
        pass


class B:
    def run(self):
        pass
"""
    symbols, _ = parse(source, "src/x.py")
    assert {s.ident for s in symbols} == {"src/x.py::A.run", "src/x.py::B.run"}


# ── 데코레이터 ─────────────────────────────────────────────────────────────


def test_데코레이터_줄부터_함수_범위로_잡는다():
    """근거 연결(git log -L)이 이 범위를 쓴다. 빼면 데코레이터만 바꾼 커밋이 누락된다."""
    source = """@router.get("/repos")
@requires_auth
def list_repos(ctx):
    pass
"""
    symbols, _ = parse(source, "src/api/repos.py")
    fn = symbols[0]
    assert fn.start_line == 1  # def는 3행이지만 데코레이터가 1행부터다
    assert fn.end_line == 4


def test_데코레이터_안의_호출은_그래프에_넣지_않는다():
    """@router.get(...)은 함수 본문이 부르는 대상이 아니라 함수에 걸린 장식이다."""
    source = """@router.get("/repos")
def list_repos(ctx):
    return fetch()
"""
    _, refs = parse(source, "src/api/repos.py")
    assert [r.target_name for r in refs] == ["fetch"]


def test_모듈_레벨_호출은_저장하지_않는다():
    _, refs = parse("import os\n\nsetup()\n", "src/boot.py")
    assert refs == []


def test_호출_줄_번호를_기록한다():
    _, refs = parse()
    call = next(r for r in refs if r.target_name == "_send")
    assert call.line == 8


# ── import 추출 (이슈 #20) ─────────────────────────────────────────────────


def test_모듈_import를_잡는다():
    items = parse_imports("import json\nimport os.path\n")
    assert [(i.local_name, i.module, i.origin_name) for i in items] == [
        ("json", "json", None),
        ("os", "os.path", None),
    ]


def test_from_import를_잡는다():
    items = parse_imports("from src.payment import process_payment, refund\n")
    assert [(i.local_name, i.module, i.origin_name) for i in items] == [
        ("process_payment", "src.payment", "process_payment"),
        ("refund", "src.payment", "refund"),
    ]


def test_별칭을_그_파일에서_쓰는_이름으로_남긴다():
    items = parse_imports("from src.payment import process_payment as pay\nimport numpy as np\n")
    assert [(i.local_name, i.origin_name) for i in items] == [
        ("pay", "process_payment"),
        ("np", None),
    ]


def test_상대_import를_절대_모듈_경로로_바꾼다():
    """src/api/repos.py 기준이다."""
    items = parse_imports("from .schemas import RepoOut\nfrom ..db.models import Repo\n")
    assert [(i.module, i.origin_name) for i in items] == [
        ("src.api.schemas", "RepoOut"),
        ("src.db.models", "Repo"),
    ]


def test_점만_있는_상대_import도_잡는다():
    items = parse_imports("from . import auth\n")
    assert [(i.module, i.origin_name) for i in items] == [("src.api", "auth")]


def test_레포_밖으로_올라가는_상대_import는_버린다():
    """확정할 수 없는 경로다. 추측으로 연결하지 않는다."""
    assert parse_imports("from ....outside import x\n", "src/x.py") == []


def test_와일드카드_import는_건너뛴다():
    """어떤 이름이 들어왔는지 알 수 없다."""
    assert parse_imports("from src.payment import *\n") == []


def test_함수_안의_지역_import도_잡는다():
    items = parse_imports("def f():\n    from src.payment import refund\n    return refund\n")
    assert [i.module for i in items] == ["src.payment"]


# ── 전역 상수 (이슈 #22) ───────────────────────────────────────────────────

CONSTANTS = '''from src.config import SECRET_KEY

TIMEOUT_SECONDS = 10
MAX_RETRY: int = 3
router = APIRouter()


def process(order_id):
    delay = TIMEOUT_SECONDS
    return sign(order_id, SECRET_KEY), delay


class Client:
    DEFAULT = 1

    def send(self):
        LOCAL_MAX = 5
        return TIMEOUT_SECONDS, LOCAL_MAX
'''


def parse_constants(source: str = CONSTANTS, path: str = "src/payment.py"):
    result = PythonAdapter().parse(path, source)
    return (
        [s for s in result.symbols if s.kind == "constant"],
        [r for r in result.references if r.ref_type == "constant"],
    )


def test_모듈_최상위_대문자_이름을_상수로_잡는다():
    symbols, _ = parse_constants()
    assert {s.ident for s in symbols} == {
        "src/payment.py::TIMEOUT_SECONDS",
        "src/payment.py::MAX_RETRY",
    }


def test_소문자_모듈_변수는_상수가_아니다():
    """router = APIRouter()는 상수가 아니다."""
    symbols, _ = parse_constants()
    assert "router" not in {s.name for s in symbols}


def test_클래스_안의_대문자는_전역_상수가_아니다():
    symbols, _ = parse_constants()
    assert "DEFAULT" not in {s.name for s in symbols}


def test_상수를_쓴_함수에서_참조가_나온다():
    _, refs = parse_constants()
    assert ("src/payment.py::process", "TIMEOUT_SECONDS") in {
        (r.source_ident, r.target_name) for r in refs
    }
    # 다른 파일에서 import한 상수도 이름으로 남는다. 대조는 인덱싱이 한다
    assert ("src/payment.py::process", "SECRET_KEY") in {
        (r.source_ident, r.target_name) for r in refs
    }


def test_메서드_안의_참조도_그_메서드에서_나온다():
    _, refs = parse_constants()
    assert ("src/payment.py::Client.send", "TIMEOUT_SECONDS") in {
        (r.source_ident, r.target_name) for r in refs
    }


def test_함수_안에서_만든_대문자_이름은_참조가_아니다():
    """지역 변수다. 남기면 다른 파일의 같은 이름 상수로 잘못 이어진다."""
    _, refs = parse_constants()
    assert "LOCAL_MAX" not in {r.target_name for r in refs}


def test_남의_객체_속성은_참조하지_않는다():
    """settings.SECRET_KEY의 SECRET_KEY는 이 파일 상수가 아니다."""
    _, refs = parse_constants("def f(settings):\n    return settings.SECRET_KEY\n")
    assert refs == []


def test_import_구문의_이름은_참조가_아니다():
    _, refs = parse_constants("from src.config import SECRET_KEY\n")
    assert refs == []


def test_튜플_대입도_각각_상수로_잡는다():
    symbols, _ = parse_constants("WIDTH, HEIGHT = 10, 20\n")
    assert {s.name for s in symbols} == {"WIDTH", "HEIGHT"}


def test_상수로_잡는_대입_형태():
    """지원 범위를 못박아 둔다. 오른쪽이 무엇이든 정의는 정의다."""
    assert {s.name for s in parse_constants("TIMEOUT: int = 30\n")[0]} == {"TIMEOUT"}
    assert {s.name for s in parse_constants("TIMEOUT = DEFAULT = 30\n")[0]} == {"TIMEOUT", "DEFAULT"}
    assert {s.name for s in parse_constants("TIMEOUT = get_value()\n")[0]} == {"TIMEOUT"}


def test_값이_없는_선언은_상수가_아니다():
    """TIMEOUT: int는 타입 선언이지 상수가 아니다."""
    assert parse_constants("TIMEOUT: int\n")[0] == []


def test_조건문_안의_대입은_상수가_아니다():
    """최상위가 아니다. 조건에 따라 달라지는 값이다."""
    assert parse_constants("if True:\n    TIMEOUT = 1\n")[0] == []


def test_상수_정의_자체는_참조가_아니다():
    """모듈 레벨이라 출발점이 없다. 자기 자신을 참조하는 간선을 만들지 않는다."""
    _, refs = parse_constants("TIMEOUT = 10\n")
    assert refs == []


# ── 어댑터 선택 ────────────────────────────────────────────────────────────


def test_파이썬_파일에만_어댑터가_붙는다():
    assert get_adapter("src/a.py") is not None
    assert get_adapter("README.md") is None


def test_지원하지_않는_파일도_언어를_묻는다():
    """파서가 없어도 파일 트리에는 나와야 한다."""
    assert language_of("src/a.py") == "python"
    assert language_of("src/a.ts") == "typescript"
    assert language_of("README.md") is None


def test_문법_오류가_있어도_예외를_내지_않는다():
    """작업 중인 코드가 섞여 있어도 인덱싱 전체가 멈추면 안 된다."""
    symbols, _ = parse("def broken(:\n    pass\n\ndef ok():\n    pass\n", "src/x.py")
    assert "ok" in {s.name for s in symbols}
