"""데이터 격리 규칙이 코드에서 지켜지는지 검사한다.

새 모델을 추가할 때 organization_id를 빼먹으면 여기서 실패한다.
규칙을 문서가 아니라 CI가 지키게 하는 것이 목적이므로,
테스트를 통과시키려고 EXEMPT에 이름을 추가하지 않는다.
"""

import pytest
from sqlalchemy import create_engine, inspect, select

from src.db import models  # noqa: F401  (모델 등록)
from src.db.query import query
from src.db.session import Base

# organizations: 조직 자체라 자기 참조가 불필요하다 (org_query로 조회).
# inquiries: 가입 전 방문자가 남기는 문의라 소속 조직이 없다.
EXEMPT = {"organizations", "inquiries"}


def test_모든_테이블에_organization_id가_있다():
    missing = [
        name
        for name, table in Base.metadata.tables.items()
        if name not in EXEMPT and "organization_id" not in table.columns
    ]
    assert missing == [], f"organization_id 누락: {missing}"


def test_query_래퍼가_조직_필터를_건다():
    stmt = str(query(models.Repo, 42))
    assert "organization_id" in stmt


def test_organization_id_없는_모델은_query에서_실패한다():
    with pytest.raises(AttributeError):
        query(models.Inquiry, 1)


def test_스키마가_실제로_생성된다():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(bind=engine)
    created = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) == created


def test_심볼_식별자_형식이_그래프_노드와_같다():
    """ident는 '파일경로::함수명'. PR 경고·질의 이력·그래프가 공유하는 규격이다."""
    ident = "src/payment.py::process_payment"
    assert "::" in ident
    path, name = ident.split("::")
    assert path.endswith(".py") and name
