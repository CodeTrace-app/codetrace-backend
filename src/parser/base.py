"""언어 어댑터 인터페이스.

언어를 추가할 때 이 인터페이스만 구현하면 되게 한다. 서비스 로직에 언어별 분기를
두지 않는다 (지원 언어는 Python과 TypeScript/JavaScript 두 가지).

추적하는 연결은 네 가지뿐이다: 함수 호출, import 의존성, 전역 상수 참조, 클래스 상속.
동적 호출·리플렉션·설정값 주입은 구문 트리로 확인할 수 없어 추적하지 않는다.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParsedSymbol:
    """파일 하나에서 찾아낸 심볼.

    ident는 "파일경로::이름" 형식이다. 그래프 노드 id, PR 경고, 질의 이력이
    공유하는 계약이므로 형식을 바꾸지 않는다. 클래스 안의 메서드는
    "파일경로::클래스명.메서드명"이 된다.
    """

    ident: str
    name: str
    path: str
    kind: str  # function | constant | class
    start_line: int
    end_line: int
    params: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedReference:
    """한 심볼이 다른 이름을 참조한 지점.

    파일 하나만 봐서는 그 이름이 어느 파일의 심볼인지 알 수 없다. 그래서 여기서는
    이름(target_name)만 남기고, 레포 전체를 파싱한 뒤 심볼 목록과 대조해 확정한다.
    """

    source_ident: str
    target_name: str
    ref_type: str  # call | import | constant | inheritance
    path: str
    line: int


class LanguageAdapter:
    """언어별 파서. 확장자로 선택된다."""

    extensions: tuple[str, ...] = ()

    def parse(self, path: str, source: str) -> tuple[list[ParsedSymbol], list[ParsedReference]]:
        raise NotImplementedError


def enclosing_symbol(line: int, symbols: list[ParsedSymbol]) -> str | None:
    """주어진 줄을 감싸는 심볼 중 가장 안쪽 것을 고른다.

    중첩 함수나 클래스 메서드에서는 여러 심볼이 같은 줄을 포함한다.
    범위가 가장 좁은 것이 실제로 그 코드가 속한 곳이다.
    """
    containing = [s for s in symbols if s.start_line <= line <= s.end_line]
    if not containing:
        return None
    return min(containing, key=lambda s: s.end_line - s.start_line).ident
