"""소스 파싱.

확장자로 어댑터를 고른다. 언어를 추가할 때는 어댑터를 만들어 _ADAPTERS에 넣기만 하면 된다.
"""

from pathlib import PurePosixPath

from src.parser.base import (
    LanguageAdapter,
    ParsedImport,
    ParsedReference,
    ParsedSymbol,
    ParseResult,
)
from src.parser.python import PythonAdapter

_ADAPTERS: tuple[LanguageAdapter, ...] = (PythonAdapter(),)

# 대시보드·파일 트리에 표시할 언어 이름 (api-spec §4).
LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
}


def get_adapter(path: str) -> LanguageAdapter | None:
    """파일에 맞는 어댑터를 돌려준다. 지원하지 않는 확장자면 None."""
    suffix = PurePosixPath(path).suffix.lower()
    for adapter in _ADAPTERS:
        if suffix in adapter.extensions:
            return adapter
    return None


def language_of(path: str) -> str | None:
    """파일 트리에 표시할 언어 이름. 지원 언어가 아니면 None.

    파서가 없는 파일(README 등)도 트리에는 나와야 하므로, 이 값이 None이라고
    파일을 건너뛰지 않는다. 뷰어에서 하이라이트 없이 표시한다.
    """
    return LANGUAGE_BY_EXTENSION.get(PurePosixPath(path).suffix.lower())


__all__ = [
    "LANGUAGE_BY_EXTENSION",
    "LanguageAdapter",
    "ParseResult",
    "ParsedImport",
    "ParsedReference",
    "ParsedSymbol",
    "get_adapter",
    "language_of",
]
