"""LLM provider 구현체. LLM_PROVIDER 설정값으로 선택된다.

openai SDK를 쓰지 않고 httpx로 직접 호출한다. 필요한 건 chat/completions 하나뿐이라
의존성을 늘릴 이유가 없고, requirements.txt에 이미 httpx가 있다.
"""

import logging
import time

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_TIMEOUT = 60.0
_DEFAULT_MODEL = "gpt-4o-mini"

# 레이트리밋·일시 장애는 기다렸다 다시 하면 된다. 팀 조직은 분당 10회 제한이라
# 심볼이 조금만 많아도 반드시 걸린다 — 재시도가 없으면 레포 대부분이 요약 없이 끝난다.
_RETRY_STATUSES = (429, 500, 502, 503, 504)
_MAX_RETRIES = 3
_DEFAULT_BACKOFF = 8.0


class LLMUnavailable(Exception):
    """LLM 호출 실패. 호출자는 요약을 비우고 넘어간다 (인덱싱을 중단시키지 않는다)."""


def _retry_after(response: httpx.Response) -> float:
    """서버가 알려준 대기 시간. 없으면 기본값을 쓴다.

    OpenAI는 Retry-After 헤더 또는 본문 메시지("Please try again in 6s")로 알려준다.
    임의로 짧게 잡고 다시 두드리면 레이트리밋만 더 태운다.
    """
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 60.0)
        except ValueError:
            pass
    return _DEFAULT_BACKOFF


class LLMProvider:
    """모든 provider가 구현하는 인터페이스."""

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class OpenAIProvider(LLMProvider):
    def complete(self, system: str, user: str) -> str:
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        # 팀 Organization 크레딧에서 차감되게 하는 헤더. 빠지면 키 소유자 개인 요금으로 청구된다.
        if settings.llm_org_id:
            headers["OpenAI-Organization"] = settings.llm_org_id

        payload = {
            "model": settings.llm_model or _DEFAULT_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # 같은 근거에 매번 다른 요약이 나오면 근거 기반이라는 주장이 무너진다.
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = httpx.post(_OPENAI_URL, headers=headers, json=payload, timeout=_TIMEOUT)
            except httpx.HTTPError as error:
                raise LLMUnavailable(f"LLM 호출 실패: {error}") from error

            if response.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                wait = _retry_after(response)
                logger.warning(
                    "LLM %s, %.0f초 뒤 재시도 (%d/%d)", response.status_code, wait, attempt, _MAX_RETRIES
                )
                time.sleep(wait)
                continue

            if response.is_error:
                # 본문에 키가 섞여 나오지는 않지만 길어서 앞부분만 남긴다.
                raise LLMUnavailable(f"LLM 응답 오류: {response.status_code} {response.text[:200]}")

            try:
                return response.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as error:
                raise LLMUnavailable(f"LLM 응답 형식이 예상과 다릅니다: {error}") from error

        raise LLMUnavailable("LLM 재시도 한도를 넘겼습니다")


class UnconfiguredProvider(LLMProvider):
    """API 키가 없을 때 쓰는 자리채움.

    키가 없다고 앱이 못 뜨면 안 된다. 요약 외의 기능은 키 없이도 전부 동작해야 하므로,
    임포트 시점이 아니라 실제 호출 시점에만 실패한다.
    """

    def complete(self, system: str, user: str) -> str:
        raise LLMUnavailable("LLM_API_KEY가 설정되지 않았습니다")


def get_provider(name: str) -> LLMProvider:
    if not settings.llm_api_key:
        return UnconfiguredProvider()
    if name == "openai":
        return OpenAIProvider()
    raise LLMUnavailable(f"LLM provider 미구현: {name}")
