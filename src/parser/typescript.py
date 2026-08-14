"""TypeScript·JavaScript 어댑터 (이슈 #21, #25).

파이썬 어댑터와 같은 것을 뽑는다: 함수 정의, 함수 호출, import 의존성, 전역 상수,
클래스 정의와 상속.
서비스 로직에 언어별 분기를 두지 않기 위해 결과 형태(ParseResult)를 똑같이 맞춘다.

파이썬과 다른 점은 두 가지다.
- 함수 모양이 여러 가지다. 선언문, 화살표 함수, default export, 클래스 메서드
- import가 점 표기가 아니라 상대 경로 문자열이다. './client'가 client.ts일 수도
  client/index.ts일 수도 있어서, 확장자와 index를 붙여보는 해석이 필요하다
"""

import tree_sitter_typescript
from tree_sitter import Language, Node, Parser, Query, QueryCursor

from src.parser.base import (
    LanguageAdapter,
    ParsedImport,
    ParsedReference,
    ParsedSymbol,
    ParseResult,
    enclosing_symbol,
)

_TYPESCRIPT = Language(tree_sitter_typescript.language_typescript())
# JSX는 문법이 달라 별도 파서가 필요하다. .tsx·.jsx가 이쪽을 쓴다.
_TSX = Language(tree_sitter_typescript.language_tsx())

# 함수의 네 가지 모양. 이름이 없는 것(익명 default export, 콜백)은 잡지 않는다 —
# 그래프 노드가 될 이름이 없다.
_FUNCTIONS = """
(function_declaration
  name: (identifier) @name
  parameters: (formal_parameters) @params) @func

(method_definition
  name: (property_identifier) @name
  parameters: (formal_parameters) @params) @func

(variable_declarator
  name: (identifier) @name
  value: (arrow_function parameters: (formal_parameters) @params)) @func

(variable_declarator
  name: (identifier) @name
  value: (function_expression parameters: (formal_parameters) @params)) @func
"""

# 괄호 없는 화살표 함수(x => x)는 파라미터 노드 모양이 다르다.
_SINGLE_PARAM_ARROW = """
(variable_declarator
  name: (identifier) @name
  value: (arrow_function parameter: (identifier) @param)) @func
"""

# 클래스 정의 (이슈 #25). 이름 있는 선언만 잡는다 — `export default class {}`는
# 그래프 노드가 될 이름이 없다. 파이썬과 달리 이름 노드가 type_identifier다.
# export·default가 붙어도 노드 타입은 그대로라 따로 잡지 않아도 된다.
# interface·type은 잡지 않는다. api-spec §4의 노드 종류에 없다.
_CLASSES = """
(class_declaration name: (type_identifier) @name) @class
(abstract_class_declaration name: (type_identifier) @name) @class
"""

_CALLS = """
(call_expression function: (identifier) @callee) @call
(call_expression function: (member_expression property: (property_identifier) @callee)) @call
"""

_IMPORTS = "(import_statement) @import"

_IDENTIFIERS = "(identifier) @name"

# 정의를 감쌌을 때 이름 앞에 붙는 노드. 파이썬의 "클래스명.메서드명" 규칙과 같다.
_QUALIFIER_NODES = {
    "class_declaration": "name",
    # abstract class의 메서드도 클래스 이름을 앞에 달아야 한다. 빠지면 두 클래스에
    # 같은 이름의 메서드가 있을 때 ident가 겹쳐 인덱싱이 실패한다.
    "abstract_class_declaration": "name",
    "function_declaration": "name",
    "method_definition": "name",
    "variable_declarator": "name",
}

_FUNCTION_VALUES = ("arrow_function", "function_expression")


def _text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _is_constant_name(name: str) -> bool:
    """상수로 볼 이름인가. 파이썬 어댑터와 같은 기준이다."""
    return name.isupper() and any(char.isalpha() for char in name)


def _qualifier(source: bytes, node: Node) -> str | None:
    """정의를 감싸는 클래스·함수 이름을 바깥에서 안쪽 순으로 잇는다.

    한 파일 안에서 이름이 겹치면 ident가 충돌해 인덱싱이 실패한다.
    """
    names: list[str] = []
    current = node.parent
    while current is not None:
        field = _QUALIFIER_NODES.get(current.type)
        if field is not None:
            name_node = current.child_by_field_name(field)
            if name_node is not None:
                names.append(_text(source, name_node))
        current = current.parent
    return ".".join(reversed(names)) or None


def _param_names(source: bytes, params: Node) -> list[str]:
    """파라미터 이름만 순서대로. 타입과 기본값은 뺀다.

    PR 시그니처 판별의 비교 대상이라, 타입만 바꾸거나 기본값만 바꾼 변경이
    경고로 새어 나가지 않게 이름만 남긴다. 구조 분해({ id, name })는 이름이 없어
    적힌 모양을 그대로 쓴다 — 그래야 항목이 늘고 주는 것을 알아챌 수 있다.
    """
    names: list[str] = []
    for child in params.named_children:
        node = child
        if child.type in ("required_parameter", "optional_parameter"):
            node = child.child_by_field_name("pattern") or child
        elif child.type == "assignment_pattern":
            node = child.child_by_field_name("left") or child

        if node.type == "identifier":
            names.append(_text(source, node))
        elif node.type in ("object_pattern", "array_pattern", "rest_pattern"):
            names.append(" ".join(_text(source, node).split()))
    return names


def _resolve_specifier(path: str, specifier: str) -> str:
    """import 경로를 레포 기준 경로로 바꾼다. 확장자는 떼어 둔다.

    'admin-web/src/pages/X.tsx'에서 '../api/refund'는 'admin-web/src/api/refund'가 된다.
    상대 경로가 아니면(react, @scope/pkg) 그대로 둔다 — 레포 안에 파일이 없으므로
    인덱싱 단계에서 외부 라이브러리로 걸러진다.

    실제로 어느 파일인지(refund.ts인지 refund/index.ts인지)는 레포 전체를 아는
    인덱싱 단계가 정한다. 여기서는 파일 하나만 보고 있어 알 수 없다.
    """
    if not specifier.startswith("."):
        return specifier

    parts = path.split("/")[:-1]
    for piece in specifier.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not parts:
                return specifier  # 레포 밖으로 나간다. 확정할 수 없다
            parts.pop()
        else:
            parts.append(piece)
    return "/".join(parts)


def _is_borrowed_name(node: Node) -> bool:
    """이 파일의 이름이 아닌 자리인가 (남의 속성, import 선언부)."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "member_expression":
        return parent.child_by_field_name("property") == node
    return parent.type in ("import_specifier", "import_clause", "namespace_import", "property_identifier")


def _superclass(source: bytes, node: Node) -> tuple[str, str | None] | None:
    """extends 대상에서 (이름, 수신자)를 꺼낸다. 확정할 수 없으면 None.

        class A extends Base      -> ("Base", None)
        class A extends ns.Base   -> ("Base", "ns")      수신자로 어느 파일인지 좁힌다
        class A extends mixin(B)  -> None. 호출로 만든 부모는 이름을 확정할 수 없다

    implements는 보지 않는다. 인터페이스는 심볼로 잡지 않아 이을 곳이 없다.
    """
    if node.type == "identifier":
        return _text(source, node), None
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        obj = node.child_by_field_name("object")
        if prop is None:
            return None
        receiver = _text(source, obj) if obj is not None and obj.type == "identifier" else None
        return _text(source, prop), receiver
    return None


def _is_declaration_site(node: Node) -> bool:
    """그 이름이 만들어지는 자리인가."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "variable_declarator":
        return parent.child_by_field_name("name") == node
    if parent.type == "assignment_expression":
        return parent.child_by_field_name("left") == node
    return False


class TypeScriptAdapter(LanguageAdapter):
    extensions = (".ts", ".tsx", ".js", ".jsx")

    def _language(self, path: str) -> Language:
        return _TSX if path.endswith((".tsx", ".jsx")) else _TYPESCRIPT

    def parse(self, path: str, source: str) -> ParseResult:
        raw = source.encode("utf-8")
        language = self._language(path)
        tree = Parser(language).parse(raw)

        functions = self._functions(path, raw, tree.root_node, language)
        constants = self._constants(path, raw, tree.root_node)
        # 파이썬 어댑터와 같다. 클래스는 심볼로만 담고 호출·상수 참조의 출발점으로는
        # 쓰지 않는다 (메서드 밖에서 일어난 것은 그래프의 출발 노드가 없다).
        classes, inheritance = self._classes(path, raw, tree.root_node, language)

        return ParseResult(
            symbols=functions + classes + constants,
            references=(
                self._calls(path, raw, tree.root_node, functions, language)
                + self._constant_refs(path, raw, tree.root_node, functions, language)
                + inheritance
            ),
            imports=self._imports(path, raw, tree.root_node, language),
        )

    def _classes(
        self, path: str, source: bytes, root: Node, language: Language
    ) -> tuple[list[ParsedSymbol], list[ParsedReference]]:
        """클래스 정의를 심볼로, extends를 참조로 담는다 (이슈 #25).

        메서드는 _functions가 따로 잡는다. 클래스 자신과 `A.method`가 둘 다 남는다.
        """
        symbols: list[ParsedSymbol] = []
        references: list[ParsedReference] = []

        for _, captured in QueryCursor(Query(language, _CLASSES)).matches(root):
            node = captured["class"][0]
            name = _text(source, captured["name"][0])

            owner = _qualifier(source, node)
            full_name = f"{owner}.{name}" if owner else name
            ident = f"{path}::{full_name}"

            symbols.append(
                ParsedSymbol(
                    ident=ident,
                    name=full_name,
                    path=path,
                    kind="class",
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    # 클래스에는 비교할 시그니처가 없다. constructor의 파라미터는
                    # 그 메서드 심볼이 이미 들고 있다.
                    params=[],
                )
            )

            heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
            if heritage is None:
                continue
            for clause in heritage.named_children:
                if clause.type != "extends_clause":
                    continue  # implements_clause는 이을 심볼이 없다
                for child in clause.named_children:
                    parent = _superclass(source, child)
                    if parent is None:
                        continue
                    target_name, receiver = parent
                    references.append(
                        ParsedReference(
                            source_ident=ident,
                            target_name=target_name,
                            ref_type="inheritance",
                            path=path,
                            line=child.start_point[0] + 1,
                            receiver=receiver,
                        )
                    )

        return symbols, references

    def _symbol(self, path: str, source: bytes, name_node: Node, func: Node, params: list[str]):
        name = _text(source, name_node)
        owner = _qualifier(source, func)
        full_name = f"{owner}.{name}" if owner else name
        return ParsedSymbol(
            ident=f"{path}::{full_name}",
            name=full_name,
            path=path,
            kind="function",
            # tree-sitter의 좌표는 0부터 시작한다. 화면·git은 1부터 센다.
            start_line=func.start_point[0] + 1,
            end_line=func.end_point[0] + 1,
            params=params,
        )

    def _functions(
        self, path: str, source: bytes, root: Node, language: Language
    ) -> list[ParsedSymbol]:
        symbols: list[ParsedSymbol] = []
        for _, captured in QueryCursor(Query(language, _FUNCTIONS)).matches(root):
            symbols.append(
                self._symbol(
                    path,
                    source,
                    captured["name"][0],
                    captured["func"][0],
                    _param_names(source, captured["params"][0]),
                )
            )
        for _, captured in QueryCursor(Query(language, _SINGLE_PARAM_ARROW)).matches(root):
            symbols.append(
                self._symbol(
                    path,
                    source,
                    captured["name"][0],
                    captured["func"][0],
                    [_text(source, captured["param"][0])],
                )
            )
        return symbols

    def _constants(self, path: str, source: bytes, root: Node) -> list[ParsedSymbol]:
        """모듈 최상위의 대문자 이름을 상수로 잡는다.

        값이 함수면 상수가 아니라 함수다 (const f = () => {}).
        """
        constants: list[ParsedSymbol] = []
        for statement in root.named_children:
            declarations = [statement]
            if statement.type == "export_statement":
                declarations = list(statement.named_children)

            for declaration in declarations:
                if declaration.type not in ("lexical_declaration", "variable_declaration"):
                    continue
                for declarator in declaration.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    name_node = declarator.child_by_field_name("name")
                    value = declarator.child_by_field_name("value")
                    if name_node is None or name_node.type != "identifier":
                        continue
                    if value is not None and value.type in _FUNCTION_VALUES:
                        continue
                    name = _text(source, name_node)
                    if not _is_constant_name(name):
                        continue
                    constants.append(
                        ParsedSymbol(
                            ident=f"{path}::{name}",
                            name=name,
                            path=path,
                            kind="constant",
                            start_line=declarator.start_point[0] + 1,
                            end_line=declarator.end_point[0] + 1,
                        )
                    )
        return constants

    def _calls(
        self, path: str, source: bytes, root: Node, symbols: list[ParsedSymbol], language: Language
    ) -> list[ParsedReference]:
        references: list[ParsedReference] = []
        for _, captured in QueryCursor(Query(language, _CALLS)).matches(root):
            call = captured["call"][0]
            line = call.start_point[0] + 1
            source_ident = enclosing_symbol(line, symbols)
            if source_ident is None:
                # 모듈 레벨 호출. 그래프의 출발점이 될 심볼이 없다.
                continue

            callee = captured["callee"][0]
            target = _text(source, callee)
            if target == source_ident.split("::", 1)[-1].rsplit(".", 1)[-1]:
                continue  # 재귀 호출

            receiver = None
            member = callee.parent
            if member is not None and member.type == "member_expression":
                obj = member.child_by_field_name("object")
                if obj is not None and obj.type == "identifier":
                    receiver = _text(source, obj)

            references.append(
                ParsedReference(
                    source_ident=source_ident,
                    target_name=target,
                    ref_type="call",
                    path=path,
                    line=line,
                    receiver=receiver,
                )
            )
        return references

    def _constant_refs(
        self, path: str, source: bytes, root: Node, symbols: list[ParsedSymbol], language: Language
    ) -> list[ParsedReference]:
        references: list[ParsedReference] = []
        declared_locally: set[tuple[str, str]] = set()

        for _, captured in QueryCursor(Query(language, _IDENTIFIERS)).matches(root):
            node = captured["name"][0]
            name = _text(source, node)
            if not _is_constant_name(name) or _is_borrowed_name(node):
                continue

            line = node.start_point[0] + 1
            source_ident = enclosing_symbol(line, symbols)
            if source_ident is None:
                continue

            if _is_declaration_site(node):
                declared_locally.add((source_ident, name))
                continue

            references.append(
                ParsedReference(
                    source_ident=source_ident,
                    target_name=name,
                    ref_type="constant",
                    path=path,
                    line=line,
                )
            )

        return [
            ref for ref in references if (ref.source_ident, ref.target_name) not in declared_locally
        ]

    def _imports(
        self, path: str, source: bytes, root: Node, language: Language
    ) -> list[ParsedImport]:
        """import { a }, import b, import * as c, import './x' 네 가지를 잡는다."""
        imports: list[ParsedImport] = []
        for _, captured in QueryCursor(Query(language, _IMPORTS)).matches(root):
            node = captured["import"][0]
            line = node.start_point[0] + 1

            source_node = node.child_by_field_name("source")
            if source_node is None:
                continue
            specifier = _text(source, source_node).strip("\"'`")
            module = _resolve_specifier(path, specifier)

            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            if clause is None:
                # import './setup' — 부수 효과만 있는 import. 의존은 있으므로 근거로 남긴다.
                imports.append(
                    ParsedImport(local_name="", module=module, origin_name=None, line=line)
                )
                continue

            for child in clause.named_children:
                if child.type == "identifier":
                    # import def from './x' — 기본 내보내기
                    imports.append(
                        ParsedImport(
                            local_name=_text(source, child),
                            module=module,
                            origin_name="default",
                            line=line,
                        )
                    )
                elif child.type == "namespace_import":
                    alias = next((c for c in child.named_children if c.type == "identifier"), None)
                    if alias is not None:
                        imports.append(
                            ParsedImport(
                                local_name=_text(source, alias),
                                module=module,
                                origin_name=None,  # 모듈 자체를 들여온다
                                line=line,
                            )
                        )
                elif child.type == "named_imports":
                    for specifier_node in child.named_children:
                        if specifier_node.type != "import_specifier":
                            continue
                        name_node = specifier_node.child_by_field_name("name")
                        alias_node = specifier_node.child_by_field_name("alias")
                        if name_node is None:
                            continue
                        origin = _text(source, name_node)
                        imports.append(
                            ParsedImport(
                                local_name=_text(source, alias_node) if alias_node else origin,
                                module=module,
                                origin_name=origin,
                                line=line,
                            )
                        )
        return imports
