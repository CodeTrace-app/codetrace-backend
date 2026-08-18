"""요약 생성이 레이트리밋을 어떻게 다루는가.

레이트리밋은 장애가 아니라 대기 신호다. 실패로 세면 심볼이 조금만 많아도
연속 실패 한도에 걸려 레포 대부분이 요약 없이 끝난다. 실제로 심볼 2,023개인
레포에서 37개만 만들어지고 중단된 적이 있다.
"""

from datetime import datetime, timezone

import pytest

from src.db.models import Organization, Repo, Symbol
from src.indexing import history
from src.llm.provider import LLMRateLimited


@pytest.fixture
def repo_with_symbols(db_session):
    org = Organization(name="에이크미", slug="acme")
    db_session.add(org)
    db_session.flush()
    repo = Repo(organization_id=org.id, name="a", github_full_name="acme/a")
    db_session.add(repo)
    db_session.flush()
    for i in range(8):
        db_session.add(
            Symbol(
                organization_id=org.id,
                repo_id=repo.id,
                ident=f"f.py::x{i}",
                name=f"x{i}",
                path="f.py",
                kind="function",
                start_line=1,
                end_line=2,
            )
        )
    db_session.commit()
    return repo


def test_레이트리밋은_기다렸다_같은_심볼을_다시_시도한다(db_session, repo_with_symbols, monkeypatch):
    """그냥 넘기면 심볼만 소진하고 요약은 거의 안 생긴다."""
    attempts: list[str] = []
    slept: list[float] = []
    monkeypatch.setattr(history.time, "sleep", lambda s: slept.append(s))

    def flaky(db, repo, symbol, snippet):
        attempts.append(symbol.ident)
        # 심볼마다 첫 시도는 레이트리밋, 두 번째에 성공
        if attempts.count(symbol.ident) == 1:
            raise LLMRateLimited(3.0)
        return object()

    monkeypatch.setattr(history, "generate_summary", flaky)

    made = history.generate_repo_summaries(db_session, repo_with_symbols)

    assert made == 8, "여덟 심볼 모두 재시도로 성공해야 한다"
    assert len(slept) == 8, "심볼마다 한 번씩 기다려야 한다"
    assert slept == [3.0] * 8, "서버가 알려준 대기 시간을 그대로 쓴다"


def test_레이트리밋은_연속_실패로_세지_않는다(db_session, repo_with_symbols, monkeypatch):
    """세면 다섯 심볼 만에 중단된다."""
    monkeypatch.setattr(history.time, "sleep", lambda s: None)
    seen: list[str] = []

    def always_limited_once(db, repo, symbol, snippet):
        seen.append(symbol.ident)
        if seen.count(symbol.ident) <= 1:
            raise LLMRateLimited(1.0)
        return object()

    monkeypatch.setattr(history, "generate_summary", always_limited_once)

    made = history.generate_repo_summaries(db_session, repo_with_symbols)

    assert made == 8, "레이트리밋이 여덟 번 나도 중단되지 않아야 한다"


def test_계속_레이트리밋이면_그_심볼은_포기한다(db_session, repo_with_symbols, monkeypatch):
    """무한정 기다리면 인덱싱이 끝나지 않는다."""
    monkeypatch.setattr(history.time, "sleep", lambda s: None)

    def never_ok(db, repo, symbol, snippet):
        raise LLMRateLimited(1.0)

    monkeypatch.setattr(history, "generate_summary", never_ok)

    made = history.generate_repo_summaries(db_session, repo_with_symbols)

    assert made == 0
    # 심볼마다 상한만큼 기다린 뒤 포기 → 연속 실패가 쌓여 중단된다
    assert db_session.query(Symbol).count() == 8


def test_진짜_실패는_다섯_번이면_중단한다(db_session, repo_with_symbols, monkeypatch):
    """키가 잘못됐거나 OpenAI가 죽은 상황까지 끝까지 두드리지 않는다."""
    calls: list[str] = []

    def broken(db, repo, symbol, snippet):
        calls.append(symbol.ident)
        raise RuntimeError("LLM 응답 오류: 401")

    monkeypatch.setattr(history, "generate_summary", broken)

    made = history.generate_repo_summaries(db_session, repo_with_symbols)

    assert made == 0
    assert len(calls) == 5, "연속 실패 한도에서 멈춰야 한다"
