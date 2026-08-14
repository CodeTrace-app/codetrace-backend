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
