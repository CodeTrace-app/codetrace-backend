"""GitHub App 설치를 조직에 연결한다.

**한 설치는 한 조직만 가진다.** GitHub App은 계정당 한 번만 설치되므로 설치 ID도
하나뿐인데, 두 조직이 그것을 나눠 가지면 PR 웹훅이 어느 조직 것인지 확정하지 못하고
먼저 만들어진 쪽을 집는다. 다른 조직은 경고를 영영 못 받으면서 에러도 보지 못한다.

연결을 이 모듈로 모은 이유: 검사를 호출부마다 따로 두면 새로 생긴 경로가 빠뜨린다.
실제로 데모 시드 스크립트가 콜백의 검사를 우회해 같은 설치를 두 조직에 심었다.

organizations는 조직 자신의 테이블이라 query() 래퍼 대상이 아니다.
설치 ID로 조직을 찾는 것은 조직 경계를 넘나드는 정당한 조회다.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Organization


class InstallationTaken(Exception):
    """이미 다른 조직이 쓰고 있는 설치다."""

    def __init__(self, owner: Organization):
        self.owner = owner
        super().__init__(f"설치가 이미 '{owner.name}' 조직에 연결되어 있습니다")


def owner_of(db: Session, installation_id: int, exclude_id: int | None = None) -> Organization | None:
    """이 설치를 쓰고 있는 조직. exclude_id는 검사에서 빼는 조직(보통 자기 자신)."""
    stmt = select(Organization).where(Organization.github_installation_id == installation_id)
    if exclude_id is not None:
        stmt = stmt.where(Organization.id != exclude_id)
    return db.scalar(stmt)


def bind_installation(db: Session, org: Organization, installation_id: int, account: str) -> None:
    """설치를 조직에 연결한다. 이미 다른 조직이 쓰고 있으면 InstallationTaken.

    commit은 부르는 쪽이 한다.
    """
    taken = owner_of(db, installation_id, exclude_id=org.id)
    if taken is not None:
        raise InstallationTaken(taken)

    org.github_installation_id = installation_id
    org.github_account = account
