"""테스트는 실제 DB에 붙지 않는다.

로컬 Postgres나 배포 DB를 테스트가 건드리지 않도록 메모리 SQLite를 쓴다.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.auth import get_db  # noqa: E402
from src.db import models  # noqa: E402,F401  (모델 등록)
from src.db.session import Base  # noqa: E402
from src.main import app  # noqa: E402


@pytest.fixture
def db_session():
    """테스트마다 빈 DB를 준다.

    메모리 SQLite는 연결마다 별도 DB가 만들어지므로 StaticPool로 하나를 공유한다.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
