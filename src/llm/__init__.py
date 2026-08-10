"""LLM 호출 단일 진입점 (CLAUDE.md §4).

- 모든 LLM 호출은 이 모듈의 complete()를 경유한다. 다른 곳에서 SDK 직접 호출 금지.
- provider는 LLM_PROVIDER 설정값으로 교체 가능해야 한다.
- 전송 허용: 커밋 메시지, PR 본문, PR 리뷰 코멘트, 사용자가 선택한 함수 단위 스니펫.
- 전송 금지: 레포 전체 소스, AST 구조체.
"""

from src.config import settings
from src.llm.provider import get_provider


def complete(system: str, user: str) -> str:
    return get_provider(settings.llm_provider).complete(system, user)
