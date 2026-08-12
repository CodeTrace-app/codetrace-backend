"""Python 어댑터 (이슈 #14).

이번 범위는 함수·메서드 정의와 함수 호출까지다.
import·전역 상수·클래스 상속은 다음 이슈에서 같은 구조로 붙인다.
"""

import tree_sitter_python
from tree_sitter import Language, Node, Parser, Query, QueryCursor

from src.parser.base import LanguageAdapter, ParsedReference, ParsedSymbol, enclosing_symbol

_LANGUAGE = Language(tree_sitter_python.language())

# 함수 정의. 클래스 메서드도 같은 노드 타입이라 함께 잡히고, 소속 클래스는 부모를 타고 찾는다.
_FUNCTIONS = """
(function_definition
  name: (identifier) @name
  parameters: (parameters) @params) @func
"""

# 호출. f() 형태와 obj.method() 형태를 모두 잡는다.
_CALLS = """
(call function: (identifier) @callee) @call
(call function: (attribute attribute: (identifier) @callee)) @call
"""

# 파라미터 노드에서 이름을 꺼낼 때 들여다볼 타입.
# 타입 힌트와 기본값은 PR 시그니처 판별의 비교 대상이 아니므로 이름만 취한다.
_PARAM_WRAPPERS = (
    "default_parameter",
    "typed_parameter",
    "typed_default_parameter",
    "list_splat_pattern",  # *args
    "dictionary_splat_pattern",  # **kwargs
)

# 메서드 첫 인자는 호출부에 나타나지 않으므로 시그니처 비교에서 뺀다.
_IMPLICIT_PARAMS = ("self", "cls")


def _text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _enclosing_class_name(source: bytes, node: Node) -> str | None:
    """정의 노드를 감싸는 클래스 이름을 찾는다. 없으면 모듈 레벨 함수다."""
    current = node.parent
    while current is not None:
        if current.type == "class_definition":
            name_node = current.child_by_field_name("name")
            return _text(source, name_node) if name_node else None
        current = current.parent
    return None


def _param_names(source: bytes, params_node: Node) -> list[str]:
    """파라미터 이름만 순서대로 뽑는다.

    PR 검사에서 "파라미터 개수 또는 이름이 달라졌는가"를 비교하는 기준이 된다.
    타입 힌트만 바꾸거나 기본값만 바꾼 변경은 경고 대상이 아니므로 이름만 남긴다.
    """
    names: list[str] = []
    for child in params_node.named_children:
        if child.type == "identifier":
            names.append(_text(source, child))
        elif child.type in _PARAM_WRAPPERS:
            inner = child.child_by_field_name("name")
            if inner is None:
                inner = next((c for c in child.named_children if c.type == "identifier"), None)
            if inner is not None:
                names.append(_text(source, inner))
    return [n for n in names if n not in _IMPLICIT_PARAMS]


def _is_self_call(source_ident: str, target: str) -> bool:
    """자기 자신을 부르는 호출인지 본다.

    메서드의 ident는 "경로::클래스명.메서드명"이라 호출부에 보이는 이름과 형태가 다르다.
    마지막 마디끼리 비교해야 self.retry_request() 같은 재귀를 걸러낼 수 있다.
    """
    name = source_ident.split("::", 1)[-1]
    return target == name.rsplit(".", 1)[-1]


class PythonAdapter(LanguageAdapter):
    extensions = (".py",)

    def parse(self, path: str, source: str) -> tuple[list[ParsedSymbol], list[ParsedReference]]:
        raw = source.encode("utf-8")
        tree = Parser(_LANGUAGE).parse(raw)
        symbols = self._functions(path, raw, tree.root_node)
        references = self._calls(path, raw, tree.root_node, symbols)
        return symbols, references

    def _functions(self, path: str, source: bytes, root: Node) -> list[ParsedSymbol]:
        symbols: list[ParsedSymbol] = []
        for _, captured in QueryCursor(Query(_LANGUAGE, _FUNCTIONS)).matches(root):
            func = captured["func"][0]
            name = _text(source, captured["name"][0])

            owner = _enclosing_class_name(source, func)
            full_name = f"{owner}.{name}" if owner else name

            symbols.append(
                ParsedSymbol(
                    ident=f"{path}::{full_name}",
                    name=full_name,
                    path=path,
                    kind="function",
                    # tree-sitter의 좌표는 0부터 시작한다. 화면·git은 1부터 센다.
                    start_line=func.start_point[0] + 1,
                    end_line=func.end_point[0] + 1,
                    params=_param_names(source, captured["params"][0]),
                )
            )
        return symbols

    def _calls(
        self, path: str, source: bytes, root: Node, symbols: list[ParsedSymbol]
    ) -> list[ParsedReference]:
        references: list[ParsedReference] = []
        for _, captured in QueryCursor(Query(_LANGUAGE, _CALLS)).matches(root):
            call = captured["call"][0]
            line = call.start_point[0] + 1

            source_ident = enclosing_symbol(line, symbols)
            if source_ident is None:
                # 모듈 레벨 호출. 그래프의 출발점이 될 심볼이 없어 저장하지 않는다.
                continue

            target = _text(source, captured["callee"][0])
            if _is_self_call(source_ident, target):
                # 재귀 호출. 자기 자신을 가리키는 간선은 그래프에서 의미가 없다.
                continue

            references.append(
                ParsedReference(
                    source_ident=source_ident,
                    target_name=target,
                    ref_type="call",
                    path=path,
                    line=line,
                )
            )
        return references
