"""PR의 변경이 경고 대상인지 판별한다 (이슈 #24).

데이터 담당의 웹훅(#29)이 detect_changes를 호출해 코멘트와 DB 저장에 쓴다.
반환 형태는 api-spec §5의 /pr-warnings 응답 항목과 1:1이다.

**저장된 인덱스로 변경을 판별하지 않는다.** base/head 실제 소스가 진실이고,
변경된 파일만 두 리비전에서 즉시 다시 파싱해 비교한다. 저장된 인덱스는
마지막 인덱싱 시점의 상태라 오래됐을 수 있어서, 판별까지 맡기면 오래된 인덱스가
변경 자체를 잘못 판정하는 원인이 된다. 인덱스는 "영향받는 위치"를 찾는 데만 쓴다.

경고는 네 가지뿐이다. 내부 로직·주석·포맷·타입 힌트·기본값 변경은 경고하지 않는다.
소음이 되면 아무도 보지 않게 되고, 그러면 경고 자체가 무의미해진다.
"""

import io
import logging
import token
import tokenize
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.orm import Session

from src.db.models import Reference, Repo
from src.db.query import query
from src.parser import ParsedSymbol, get_adapter

logger = logging.getLogger(__name__)

ChangeType = Literal["signature_changed", "deleted", "renamed", "constant_changed"]
RefType = Literal["call", "import", "constant", "inheritance"]


@dataclass(frozen=True)
class ChangedFile:
    """PR에서 바뀐 파일 하나.

    수정: base·head 둘 다 소스가 있다
    신규: base_source가 None
    삭제: head_source가 None
    이름 변경: previous_path에 옛 경로가 들어온다 (GitHub의 previous_filename)
    """

    path: str
    base_source: str | None = None
    head_source: str | None = None
    previous_path: str | None = None


@dataclass(frozen=True)
class Impacted:
    """변경된 심볼을 참조하고 있어 영향을 받는 위치."""

    symbol: str
    path: str
    line: int  # 1-based
    type: RefType


@dataclass(frozen=True)
class Warning:
    change_type: ChangeType
    symbol: str
    detail: str
    impacted: list[Impacted] = field(default_factory=list)


def _parse_symbols(path: str, source: str | None) -> dict[str, ParsedSymbol]:
    """파일 하나를 파싱해 이름 → 심볼로 돌려준다. 파서가 없거나 소스가 없으면 빈 값.

    이름으로 묶는다. 파일 이름이 바뀌면 ident가 통째로 달라져 비교가 되지 않는다.
    """
    if source is None:
        return {}
    adapter = get_adapter(path)
    if adapter is None:
        return {}
    try:
        result = adapter.parse(path, source)
    except Exception:
        # 파일 하나가 파서를 넘어뜨려도 PR 검사 전체가 실패하면 안 된다.
        logger.exception("PR 검사 중 파싱 실패, 건너뜁니다: %s", path)
        return {}
    return {symbol.name: symbol for symbol in result.symbols}


def _of_kind(symbols: dict[str, ParsedSymbol], kind: str) -> dict[str, ParsedSymbol]:
    return {name: symbol for name, symbol in symbols.items() if symbol.kind == kind}


def _statement(source: str, symbol: ParsedSymbol) -> str:
    """심볼이 차지한 줄의 소스. 상수 값 비교와 안내 문구에 쓴다.

    줄 번호가 아니라 내용을 비교한다. 위아래 코드가 밀려 줄만 바뀐 것은 변경이 아니다.
    """
    lines = source.splitlines()[symbol.start_line - 1 : symbol.end_line]
    return " ".join(" ".join(lines).split())


def _tokens(statement: str) -> list[tuple[int, str]] | None:
    """문장을 토큰 목록으로. 공백·줄바꿈·주석은 빼고 남긴다.

    글자로 비교하면 값을 그대로 두고 줄만 나눈 포맷 변경이 값 변경으로 잡힌다.
    반대로 공백을 전부 지워서 비교하면 문자열 안의 공백 변화("김 철수" → "김철수")를
    놓친다. 토큰이 둘 다 피하는 기준이다.

    토큰으로 나눌 수 없으면 None. 부르는 쪽이 글자 비교로 돌아간다.
    """
    ignored = {
        token.NEWLINE, token.NL, token.INDENT, token.DEDENT, token.COMMENT, token.ENDMARKER,
    }
    try:
        stream = tokenize.generate_tokens(io.StringIO(statement).readline)
        tokens = [(item.type, item.string) for item in stream if item.type not in ignored]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None

    # 목록·사전을 여러 줄로 펼 때 붙는 후행 쉼표는 값이 아니라 포맷이다.
    # 괄호 앞은 건드리지 않는다 — (1,)과 (1)은 튜플과 숫자로 실제로 다른 값이다.
    cleaned: list[tuple[int, str]] = []
    for index, item in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if item[1] == "," and following is not None and following[1] in ("}", "]"):
            continue
        cleaned.append(item)
    return cleaned


def _same_value(old: str, new: str) -> bool:
    """두 대입문이 같은 값인가. 포맷만 다른 것은 같은 값으로 본다."""
    old_tokens, new_tokens = _tokens(old), _tokens(new)
    if old_tokens is None or new_tokens is None:
        return old == new
    return old_tokens == new_tokens


def _value_of(statement: str) -> str:
    """대입문에서 값 부분만. `TIMEOUT: int = 30` → `30`

    코멘트와 화면에 그대로 실리는 문구라 너무 길면 줄인다.
    """
    _, _, value = statement.partition("=")
    value = value.strip() or statement
    return value if len(value) <= 60 else f"{value[:57]}..."


def _find_impacted(db: Session, repo: Repo, ident: str) -> list[Impacted]:
    """저장된 인덱스에서 이 심볼을 참조하는 위치를 찾는다.

    import 참조도 포함한다. 그래프에는 import 노드를 만들지 않지만, 변경된 심볼이
    다른 파일에서 import되고 있다는 사실은 영향 범위의 근거다 (#24 인터페이스 합의).
    """
    rows = db.scalars(
        query(Reference, repo.organization_id)
        .where(Reference.repo_id == repo.id)
        .where(Reference.target_ident == ident)
    )
    impacted = [
        Impacted(symbol=row.source_ident, path=row.path, line=row.line, type=row.ref_type)
        for row in rows
    ]
    # 같은 곳에서 여러 번 부르면 한 번만 알린다. 화면에 같은 줄이 반복되면 읽기 어렵다.
    unique: dict[tuple[str, int], Impacted] = {}
    for item in impacted:
        unique.setdefault((item.symbol, item.line), item)
    return sorted(unique.values(), key=lambda item: (item.path, item.line))


def _match_renames(
    gone: dict[str, ParsedSymbol], added: dict[str, ParsedSymbol]
) -> list[tuple[ParsedSymbol, ParsedSymbol]]:
    """사라진 함수와 새로 생긴 함수를 짝지어 이름 변경을 찾는다.

    파라미터가 같은 후보가 정확히 하나일 때만 짝짓는다. 여럿이면 어느 것이 어느 것으로
    바뀌었는지 알 수 없다 — 추측해서 틀린 경고를 내느니 삭제와 추가로 둔다.
    """
    matched: list[tuple[ParsedSymbol, ParsedSymbol]] = []
    for old in list(gone.values()):
        candidates = [new for new in added.values() if new.params == old.params]
        if len(candidates) != 1:
            continue
        new = candidates[0]
        matched.append((old, new))
        gone.pop(old.name, None)
        added.pop(new.name, None)
    return matched


def _compare_file(
    db: Session, repo: Repo, changed: ChangedFile, base: dict, head: dict
) -> list[Warning]:
    base_path = changed.previous_path or changed.path
    warnings: list[Warning] = []

    base_functions = _of_kind(base, "function")
    head_functions = _of_kind(head, "function")

    # 1) 시그니처 변경 — 파라미터의 개수 또는 이름이 달라졌을 때만.
    #    타입 힌트와 기본값은 파서가 파라미터 이름만 남기므로 여기 걸리지 않는다.
    for name, old in base_functions.items():
        new = head_functions.get(name)
        if new is None or old.params == new.params:
            continue
        warnings.append(
            Warning(
                change_type="signature_changed",
                symbol=f"{base_path}::{name}",
                detail=(
                    f"파라미터가 ({', '.join(old.params)})에서 "
                    f"({', '.join(new.params)})로 바뀌었습니다"
                ),
                impacted=_find_impacted(db, repo, f"{base_path}::{name}"),
            )
        )

    gone = {n: s for n, s in base_functions.items() if n not in head_functions}
    added = {n: s for n, s in head_functions.items() if n not in base_functions}

    # 2) 이름 변경 — 사라진 것과 생긴 것을 먼저 짝지어야 삭제로 잘못 세지 않는다.
    for old, new in _match_renames(gone, added):
        warnings.append(
            Warning(
                change_type="renamed",
                symbol=f"{base_path}::{old.name}",
                detail=f"{old.name}에서 {new.name}로 이름이 바뀌었습니다",
                impacted=_find_impacted(db, repo, f"{base_path}::{old.name}"),
            )
        )

    # 3) 삭제 — 짝을 찾지 못하고 사라진 것.
    for name in gone:
        warnings.append(
            Warning(
                change_type="deleted",
                symbol=f"{base_path}::{name}",
                detail=f"{name} 함수가 삭제되었습니다",
                impacted=_find_impacted(db, repo, f"{base_path}::{name}"),
            )
        )

    # 4) 상수 값 변경 — 참조하는 쪽의 동작이 조용히 달라진다.
    base_constants = _of_kind(base, "constant")
    head_constants = _of_kind(head, "constant")
    for name, old in base_constants.items():
        new = head_constants.get(name)
        if new is None:
            continue
        old_statement = _statement(changed.base_source or "", old)
        new_statement = _statement(changed.head_source or "", new)
        if _same_value(old_statement, new_statement):
            continue
        warnings.append(
            Warning(
                change_type="constant_changed",
                symbol=f"{base_path}::{name}",
                detail=(
                    f"값이 {_value_of(old_statement)}에서 "
                    f"{_value_of(new_statement)}로 바뀌었습니다"
                ),
                impacted=_find_impacted(db, repo, f"{base_path}::{name}"),
            )
        )

    return warnings


def detect_changes(
    db: Session, repo: Repo, changed_files: list[ChangedFile]
) -> list[Warning]:
    """PR의 변경이 경고 대상인지 판별한다.

    경고할 것이 없으면 빈 목록. 경고 유형 네 가지에 해당하지 않는 변경은 여기 오지 않는다.
    """
    warnings: list[Warning] = []
    for changed in changed_files:
        if changed.base_source is None and changed.head_source is None:
            # 정상적인 변경 파일 상태가 아니다.
            continue
        base = _parse_symbols(changed.previous_path or changed.path, changed.base_source)
        head = _parse_symbols(changed.path, changed.head_source)
        warnings.extend(_compare_file(db, repo, changed, base, head))
    return warnings
