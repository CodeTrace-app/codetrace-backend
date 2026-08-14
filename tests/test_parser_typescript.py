"""TypeScript·JavaScript 파서 (이슈 #21). DB 없이 소스 문자열만으로 검사한다.

예시는 데모 레포(acme-payments/acme-payment-service)의 admin-web에서 가져왔다.
"""

from src.parser import get_adapter, language_of
from src.parser.typescript import TypeScriptAdapter

REFUND_API = """import { request } from './client'

export interface Refund {
  id: number
}

export async function listRefunds(): Promise<Refund[]> {
  return request<Refund[]>('/refunds')
}

export async function getRefund(id: number): Promise<Refund> {
  return request<Refund>(`/refunds/${id}`)
}
"""

REFUND_PAGE = """import { useEffect, useState } from 'react'

import { listRefunds, type Refund } from '../api/refund'

export default function RefundListPage() {
  const [items, setItems] = useState<Refund[]>([])

  useEffect(() => {
    listRefunds().then(setItems)
  }, [])

  return <table><tbody>{items.map((i) => <tr key={i.id}><td>{i.id}</td></tr>)}</tbody></table>
}
"""


def parse(source: str, path: str = "admin-web/src/api/refund.ts"):
    return TypeScriptAdapter().parse(path, source)


def names(result, kind="function"):
    return {s.name for s in result.symbols if s.kind == kind}


# ── 함수의 네 가지 모양 ────────────────────────────────────────────────────


def test_함수_선언을_잡는다():
    assert names(parse(REFUND_API)) == {"listRefunds", "getRefund"}


def test_화살표_함수를_잡는다():
    result = parse("export const fetchAll = async (path: string) => request(path)\n")
    assert names(result) == {"fetchAll"}
    assert result.symbols[0].params == ["path"]


def test_괄호_없는_화살표_함수도_잡는다():
    """x => x 는 파라미터 노드 모양이 다르다."""
    result = parse("const double = x => x * 2\n")
    assert names(result) == {"double"}
    assert result.symbols[0].params == ["x"]


def test_default_export_함수를_잡는다():
    assert "RefundListPage" in names(parse(REFUND_PAGE, "admin-web/src/pages/RefundListPage.tsx"))


def test_클래스_메서드는_클래스명을_붙인다():
    """파이썬 어댑터와 같은 규칙이다."""
    result = parse("class RefundService {\n  getRefund(id) { return request(id) }\n}\n")
    assert names(result) == {"RefundService.getRefund"}


def test_이름_없는_함수는_잡지_않는다():
    """그래프 노드가 될 이름이 없다."""
    assert names(parse("export default function () { return 1 }\n")) == set()


# ── 파라미터 (PR 시그니처 판별의 기준) ──────────────────────────────────────


def test_타입과_기본값은_파라미터_이름에_섞이지_않는다():
    result = parse("export function pay(id: number, retry: number = 3) { return id }\n")
    assert result.symbols[0].params == ["id", "retry"]


def test_구조_분해_파라미터는_적힌_모양을_쓴다():
    """이름이 없다. 항목이 늘고 주는 것은 알아챌 수 있어야 한다."""
    result = parse("export function pay({ id, amount }) { return id }\n")
    assert result.symbols[0].params == ["{ id, amount }"]


def test_나머지_파라미터도_잡는다():
    result = parse("export function log(first, ...rest) { return first }\n")
    assert result.symbols[0].params == ["first", "...rest"]


# ── 호출 ───────────────────────────────────────────────────────────────────


def test_호출이_일어난_함수를_출발점으로_기록한다():
    calls = [r for r in parse(REFUND_API).references if r.ref_type == "call"]
    assert ("admin-web/src/api/refund.ts::listRefunds", "request") in {
        (r.source_ident, r.target_name) for r in calls
    }


def test_수신자가_있는_호출은_수신자를_남긴다():
    """client.get() 의 client. import한 모듈이면 대상이 확정된다 (#20 규칙)."""
    result = parse("export function load() { return client.get('/x') }\n")
    call = next(r for r in result.references if r.target_name == "get")
    assert call.receiver == "client"


def test_모듈_레벨_호출은_저장하지_않는다():
    assert [r for r in parse("setup()\n").references if r.ref_type == "call"] == []


def test_재귀_호출은_저장하지_않는다():
    result = parse("export function retry(n) { return retry(n - 1) }\n")
    assert [r for r in result.references if r.ref_type == "call"] == []


# ── import ─────────────────────────────────────────────────────────────────


def test_이름_import를_잡는다():
    items = parse(REFUND_API).imports
    assert [(i.local_name, i.module, i.origin_name) for i in items] == [
        ("request", "admin-web/src/api/client", "request")
    ]


def test_상대_경로를_레포_기준_경로로_바꾼다():
    """../api/refund → admin-web/src/api/refund"""
    items = parse(REFUND_PAGE, "admin-web/src/pages/RefundListPage.tsx").imports
    modules = {i.module for i in items}
    assert "admin-web/src/api/refund" in modules
    # 외부 라이브러리는 그대로 둔다. 인덱싱 단계에서 걸러진다
    assert "react" in modules


def test_기본_import와_네임스페이스_import를_구분한다():
    items = parse("import client from './client'\nimport * as helpers from './helpers'\n").imports
    assert [(i.local_name, i.origin_name) for i in items] == [
        ("client", "default"),
        ("helpers", None),
    ]


def test_부수_효과_import도_근거로_남긴다():
    items = parse("import './setup'\n").imports
    assert [(i.local_name, i.module) for i in items] == [("", "admin-web/src/api/setup")]


def test_레포_밖으로_나가는_상대_경로는_그대로_둔다():
    """확정할 수 없다. 추측으로 연결하지 않는다."""
    items = parse("import { x } from '../../../../outside'\n", "a/b.ts").imports
    assert items[0].module == "../../../../outside"


# ── 상수 ───────────────────────────────────────────────────────────────────


def test_대문자_모듈_변수를_상수로_잡는다():
    result = parse("export const BASE_URL = '/api'\nconst MAX_RETRY = 3\n")
    assert names(result, "constant") == {"BASE_URL", "MAX_RETRY"}


def test_값이_함수면_상수가_아니다():
    result = parse("export const FETCH = () => request('/x')\n")
    assert names(result, "constant") == set()
    assert names(result) == {"FETCH"}


def test_함수_안의_대문자는_상수가_아니다():
    result = parse("export function f() {\n  const LOCAL = 1\n  return LOCAL\n}\n")
    assert names(result, "constant") == set()
    assert [r for r in result.references if r.ref_type == "constant"] == []


def test_상수를_쓴_함수에서_참조가_나온다():
    result = parse("const MAX = 3\nexport function f() { return MAX }\n")
    refs = [r for r in result.references if r.ref_type == "constant"]
    assert [(r.source_ident, r.target_name) for r in refs] == [
        ("admin-web/src/api/refund.ts::f", "MAX")
    ]


def test_남의_객체_속성은_참조하지_않는다():
    result = parse("export function f(settings) { return settings.MAX }\n")
    assert [r for r in result.references if r.ref_type == "constant"] == []


# ── 클래스와 상속 (이슈 #25) ───────────────────────────────────────────────

CLASSES = """import { Base } from './base'

export class RefundClient extends Base {
  constructor(private url: string) {
    super()
  }

  async fetch(id: number) {
    return null
  }
}

class Plain {}

export default class Wrapper extends ns.Base {}
"""


def test_클래스를_심볼로_잡는다():
    result = parse(CLASSES)
    assert names(result, "class") == {"RefundClient", "Plain", "Wrapper"}


def test_export와_default가_붙어도_잡는다():
    assert names(parse("export class A {}"), "class") == {"A"}
    assert names(parse("export default class B {}"), "class") == {"B"}
    assert names(parse("class C {}"), "class") == {"C"}


def test_abstract_클래스도_잡는다():
    assert names(parse("abstract class D {}"), "class") == {"D"}


def test_클래스와_그_메서드가_둘_다_남는다():
    """클래스를 담느라 기존 메서드 추출을 덮어쓰면 안 된다."""
    by_ident = {s.ident: s.kind for s in parse(CLASSES).symbols}
    assert by_ident["admin-web/src/api/refund.ts::RefundClient"] == "class"
    assert by_ident["admin-web/src/api/refund.ts::RefundClient.fetch"] == "function"


def test_abstract_클래스의_메서드도_클래스_이름을_붙인다():
    """빠지면 같은 이름의 메서드끼리 ident가 겹쳐 인덱싱이 실패한다."""
    source = "abstract class A {\n  run() {}\n}\nclass B {\n  run() {}\n}\n"
    idents = [s.ident for s in parse(source).symbols if s.kind == "function"]
    assert set(idents) == {
        "admin-web/src/api/refund.ts::A.run",
        "admin-web/src/api/refund.ts::B.run",
    }
    assert len(idents) == len(set(idents))


def test_이름이_없는_default_export는_잡지_않는다():
    """그래프 노드가 될 이름이 없다."""
    assert names(parse("export default class {}"), "class") == set()


def test_인터페이스는_클래스가_아니다():
    """api-spec §4의 노드 종류에 interface는 없다."""
    assert names(parse("export interface Refund {\n  id: number\n}\n"), "class") == set()


def test_클래스에는_파라미터가_없다():
    assert all(s.params == [] for s in parse(CLASSES).symbols if s.kind == "class")


def test_extends를_참조로_남긴다():
    refs = [r for r in parse(CLASSES).references if r.ref_type == "inheritance"]
    assert ("admin-web/src/api/refund.ts::RefundClient", "Base", None) in [
        (r.source_ident, r.target_name, r.receiver) for r in refs
    ]


def test_점_표기_부모는_이름과_수신자를_나눠_담는다():
    refs = [r for r in parse(CLASSES).references if r.ref_type == "inheritance"]
    wrapper = next(r for r in refs if r.source_ident.endswith("::Wrapper"))
    assert (wrapper.target_name, wrapper.receiver) == ("Base", "ns")


def test_implements는_상속이_아니다():
    """인터페이스는 심볼로 잡지 않아 이을 곳이 없다."""
    source = "interface F {}\nclass C extends D implements F {}\n"
    refs = [r for r in parse(source).references if r.ref_type == "inheritance"]
    assert {r.target_name for r in refs} == {"D"}


def test_이름을_확정할_수_없는_부모는_버린다():
    """mixin(Base)로 만든 부모는 이름을 확정할 수 없다. 추측으로 연결하지 않는다."""
    refs = [r for r in parse("class C extends mixin(Base) {}").references]
    assert [r for r in refs if r.ref_type == "inheritance"] == []


def test_상속이_없는_클래스는_간선을_만들지_않는다():
    refs = parse("class Plain {}").references
    assert [r for r in refs if r.ref_type == "inheritance"] == []


def test_tsx에서도_클래스를_잡는다():
    """JSX는 별도 파서를 쓴다. 한쪽만 되면 안 된다."""
    result = parse("export class Widget extends Base {}", "admin-web/src/pages/Widget.tsx")
    assert names(result, "class") == {"Widget"}
    assert [r.target_name for r in result.references if r.ref_type == "inheritance"] == ["Base"]


def test_js_파일에서도_클래스를_잡는다():
    result = parse("class Legacy extends Base {}", "scripts/legacy.js")
    assert names(result, "class") == {"Legacy"}


# ── 어댑터 선택 ────────────────────────────────────────────────────────────


def test_네_가지_확장자에_어댑터가_붙는다():
    for path in ("a.ts", "a.tsx", "a.js", "a.jsx"):
        assert get_adapter(path) is not None, path
    assert language_of("a.tsx") == "typescript"
    assert language_of("a.jsx") == "javascript"


def test_jsx가_있어도_파싱된다():
    """.tsx는 문법이 달라 별도 파서를 쓴다."""
    result = parse(REFUND_PAGE, "admin-web/src/pages/RefundListPage.tsx")
    assert result.symbols


def test_문법_오류가_있어도_예외를_내지_않는다():
    result = parse("export function broken( {\n", "a.ts")
    assert isinstance(result.symbols, list)
