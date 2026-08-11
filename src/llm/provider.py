"""LLM provider 구현체. LLM_PROVIDER 설정값으로 선택된다."""


class LLMProvider:
    """모든 provider가 구현하는 인터페이스."""

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


def get_provider(name: str) -> LLMProvider:
    # 데이터 담당(F-JFZSAT)이 provider 구현체를 추가하면서 채운다.
    # openai를 붙일 때는 settings.llm_org_id를 클라이언트에 함께 넘겨야
    # 팀 크레딧에서 차감된다. 요약은 짧은 텍스트라 저렴한 소형 모델로 충분하다.
    raise NotImplementedError(f"LLM provider 미구현: {name}")
