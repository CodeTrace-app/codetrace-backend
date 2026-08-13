"""Python 어댑터 (이슈 #14, #20).

이번 범위는 함수·메서드 정의, 함수 호출, import 의존성까지다.
전역 상수·클래스 상속은 다음 이슈에서 같은 구조로 붙인다.
"""

import tree_sitter_python
from tree_sitter import Language, Node, Parser, Query, QueryCursor

from src.parser.base import (
    LanguageAdapter,
    ParsedImport,
    ParsedReference,
    ParsedSymbol,
    ParseResult,
    enclosing_symbol,
)

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

# import. 함수 안의 지역 import나 if TYPE_CHECKING 블록 안에 있어도 잡히도록
# 위치를 지정하지 않는다.
_IMPORTS = """
(import_statement) @import
(import_from_statement) @import
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


def _qualifier(source: bytes, node: Node) -> str | None:
    """정의를 감싸는 클래스·함수 이름을 바깥에서 안쪽 순으로 잇는다.

    클래스 메서드가 "클래스명.메서드명"인 것과 같은 규칙을 중첩 함수에도 적용한다.
    감싼 쪽 이름을 붙이지 않으면 한 파일 안에서 이름이 같은 중첩 함수끼리 ident가
    겹치고, Symbol의 UniqueConstraint(repo_id, ident)에 걸려 인덱싱 전체가 실패한다.
    """
    names: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in ("class_definition", "function_definition"):
            name_node = current.child_by_field_name("name")
            if name_node is not None:
                names.append(_text(source, name_node))
        current = current.parent
    return ".".join(reversed(names)) or None


def _definition_start(func: Node) -> int:
    """함수의 시작 줄. 데코레이터가 있으면 데코레이터 첫 줄부터다.

    tree-sitter는 데코레이터를 def 바깥의 decorated_definition에 두므로 def 줄만
    보면 범위에서 빠진다. 근거 연결(git log -L)이 이 범위로 이력을 뒤지기 때문에,
    빼면 라우트 경로나 인증 데코레이터만 바꾼 커밋이 근거에서 통째로 누락된다.
    """
    parent = func.parent
    if parent is not None and parent.type == "decorated_definition":
        return parent.start_point[0] + 1
    return func.start_point[0] + 1


def _in_decorator(node: Node) -> bool:
    """데코레이터 안에서 일어난 호출인지 본다.

    데코레이터 줄이 함수 범위에 들어오면서 @router.get(...) 같은 것도 호출로 잡힌다.
    이건 함수 본문이 부르는 대상이 아니라 함수에 걸린 장식이므로 그래프에 넣지 않는다.
    """
    current: Node | None = node
    while current is not None:
        if current.type == "decorator":
            return True
        if current.type == "block":  # 함수 본문에 닿았다. 데코레이터 바깥이다.
            return False
        current = current.parent
    return False


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


def _name_and_alias(source: bytes, node: Node) -> tuple[str | None, str | None]:
    """import 항목에서 (이름, 별칭)을 꺼낸다. `x as y`면 별칭이 y다."""
    if node.type == "aliased_import":
        name_node = node.child_by_field_name("name")
        alias_node = node.child_by_field_name("alias")
        name = _text(source, name_node) if name_node else None
        return name, _text(source, alias_node) if alias_node else None
    if node.type == "dotted_name":
        return _text(source, node), None
    return None, None


def _receiver(source: bytes, callee: Node) -> str | None:
    """`a.b()` 호출에서 앞의 이름 a를 꺼낸다. `b()`나 `a.b.c()`면 None.

    a가 import한 모듈이면 b가 어느 파일의 함수인지 그 자리에서 확정된다.
    a.b.c()처럼 중간이 낀 경우는 무엇을 거쳐 왔는지 알 수 없어 비워 둔다.
    """
    attribute = callee.parent
    if attribute is None or attribute.type != "attribute":
        return None
    obj = attribute.child_by_field_name("object")
    if obj is None or obj.type != "identifier":
        return None
    return _text(source, obj)


def _absolute_module(path: str, dots: int, tail: str | None) -> str | None:
    """상대 import를 절대 모듈 경로로 바꾼다.

    점 하나는 이 파일이 있는 디렉터리, 둘이면 그 위다.
    `src/api/repos.py`에서 `from .schemas import X`는 `src.api.schemas`가 된다.

    레포 밖으로 올라가는 경로는 확정할 수 없으므로 버린다. 추측으로 연결하지 않는다.
    """
    parts = path.split("/")[:-1]  # 파일이 들어 있는 디렉터리
    up = dots - 1
    if up:
        if up > len(parts):
            return None
        parts = parts[: len(parts) - up]
    if tail:
        parts = parts + tail.split(".")
    return ".".join(parts) if parts else None


class PythonAdapter(LanguageAdapter):
    extensions = (".py",)

    def parse(self, path: str, source: str) -> ParseResult:
        raw = source.encode("utf-8")
        tree = Parser(_LANGUAGE).parse(raw)
        symbols = self._functions(path, raw, tree.root_node)
        return ParseResult(
            symbols=symbols,
            references=self._calls(path, raw, tree.root_node, symbols),
            imports=self._imports(path, raw, tree.root_node),
        )

    def _functions(self, path: str, source: bytes, root: Node) -> list[ParsedSymbol]:
        symbols: list[ParsedSymbol] = []
        for _, captured in QueryCursor(Query(_LANGUAGE, _FUNCTIONS)).matches(root):
            func = captured["func"][0]
            name = _text(source, captured["name"][0])

            owner = _qualifier(source, func)
            full_name = f"{owner}.{name}" if owner else name

            symbols.append(
                ParsedSymbol(
                    ident=f"{path}::{full_name}",
                    name=full_name,
                    path=path,
                    kind="function",
                    # tree-sitter의 좌표는 0부터 시작한다. 화면·git은 1부터 센다.
                    start_line=_definition_start(func),
                    end_line=func.end_point[0] + 1,
                    params=_param_names(source, captured["params"][0]),
                )
            )
        return symbols

    def _imports(self, path: str, source: bytes, root: Node) -> list[ParsedImport]:
        """세 가지 형태를 모두 잡는다: `import x` · `from x import y` · `from . import y`.

        `from x import *`는 어떤 이름이 들어왔는지 알 수 없어 건너뛴다.
        """
        imports: list[ParsedImport] = []
        for _, captured in QueryCursor(Query(_LANGUAGE, _IMPORTS)).matches(root):
            node = captured["import"][0]
            line = node.start_point[0] + 1

            if node.type == "import_statement":
                for child in node.children_by_field_name("name"):
                    module, alias = _name_and_alias(source, child)
                    if module is None:
                        continue
                    # `import a.b`는 이름 a를 들여온다. `as`가 있으면 그 이름이다.
                    imports.append(
                        ParsedImport(
                            local_name=alias or module.split(".")[0],
                            module=module,
                            origin_name=None,
                            line=line,
                        )
                    )
                continue

            module = self._from_module(path, source, node)
            if module is None:
                continue
            for child in node.children_by_field_name("name"):
                name, alias = _name_and_alias(source, child)
                if name is None:
                    continue
                imports.append(
                    ParsedImport(
                        local_name=alias or name,
                        module=module,
                        origin_name=name,
                        line=line,
                    )
                )
        return imports

    def _from_module(self, path: str, source: bytes, node: Node) -> str | None:
        """`from ... import`의 모듈 경로. 상대 import는 절대 경로로 바꾼다."""
        module_node = node.child_by_field_name("module_name")
        if module_node is None:
            return None
        if module_node.type == "dotted_name":
            return _text(source, module_node)
        if module_node.type == "relative_import":
            prefix = next((c for c in module_node.children if c.type == "import_prefix"), None)
            if prefix is None:
                return None
            tail_node = next((c for c in module_node.children if c.type == "dotted_name"), None)
            tail = _text(source, tail_node) if tail_node is not None else None
            return _absolute_module(path, len(_text(source, prefix)), tail)
        return None

    def _calls(
        self, path: str, source: bytes, root: Node, symbols: list[ParsedSymbol]
    ) -> list[ParsedReference]:
        references: list[ParsedReference] = []
        for _, captured in QueryCursor(Query(_LANGUAGE, _CALLS)).matches(root):
            call = captured["call"][0]
            if _in_decorator(call):
                continue

            line = call.start_point[0] + 1
            source_ident = enclosing_symbol(line, symbols)
            if source_ident is None:
                # 모듈 레벨 호출. 그래프의 출발점이 될 심볼이 없어 저장하지 않는다.
                continue

            callee = captured["callee"][0]
            target = _text(source, callee)
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
                    receiver=_receiver(source, callee),
                )
            )
        return references
