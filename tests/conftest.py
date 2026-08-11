"""테스트는 실제 DB에 붙지 않는다.

DB가 필요한 테스트를 쓸 때는 이 파일에 SQLite 세션 픽스처를 추가한다.
로컬 Postgres나 배포 DB를 테스트가 건드리게 하지 않는다.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
