"""테이블 생성·초기화.

마이그레이션 도구를 쓰지 않는다. 해커톤 기간에는 스키마가 자주 바뀌고
마이그레이션 파일 충돌을 관리하는 비용이 얻는 것보다 크기 때문이다.

대신 규칙을 지킨다:
- 테이블은 앱 시작 시 create_all로 만든다 (없는 것만 생성).
- **컬럼 추가·변경은 create_all이 반영하지 못한다.**
  모델을 고쳤으면 DB를 리셋한다: `python -m src.db.init --reset`
- 리셋하면 데이터가 사라지지만, 인덱싱은 다시 돌리면 복구된다.
- 모델을 고쳐 올릴 때는 PR의 "PM이 알아야 할 것"에 반드시 적는다.
  팀원은 로컬 DB를, PM은 배포 DB를 리셋해야 한다.
"""

import sys

from sqlalchemy import text

from src.db import models  # noqa: F401  (모델을 metadata에 등록하기 위한 import)
from src.db.session import Base, engine


def create_tables() -> None:
    Base.metadata.create_all(bind=engine)


def reset_tables() -> None:
    """스키마를 통째로 비우고 다시 만든다.

    drop_all은 현재 모델에 있는 테이블만 지운다. 테이블 이름을 바꾸거나 모델을
    삭제하면 옛 테이블이 DB에 남아 다른 테이블의 삭제를 막는다.
    그래서 스키마 단위로 지운다.
    """
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        else:
            Base.metadata.drop_all(bind=conn)
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset_tables()
        print("DB를 리셋했습니다. 테이블", len(Base.metadata.tables), "개 생성")
    else:
        create_tables()
        print("테이블을 생성했습니다.", len(Base.metadata.tables), "개")
