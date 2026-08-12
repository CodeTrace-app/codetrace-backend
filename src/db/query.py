"""organization_id 필터를 강제하는 조회 래퍼.

데이터 격리 규칙:
- 조직 데이터를 담는 모든 테이블에 organization_id가 있다.
- 모든 DB 조회는 이 모듈의 query()를 거친다. raw 쿼리·select() 직접 사용 금지.
- 데모 세션도 데모 조직의 organization_id로 같은 경로를 통과한다. 예외 경로를 만들지 않는다.
"""

from sqlalchemy import Select, select


def query(model: type, organization_id: int) -> Select:
    """organization_id 필터가 적용된 SELECT 문을 반환한다.

    model에 organization_id 컬럼이 없으면 AttributeError로 즉시 실패한다.
    격리 대상이 아닌 테이블은 존재할 수 없다는 규칙을 코드로 강제하는 것이므로
    이 에러를 우회하는 분기를 추가하지 않는다.
    """
    return select(model).where(model.organization_id == organization_id)


def org_query(organization_id: int) -> Select:
    """organizations 테이블 자체를 조회한다.

    organizations에는 organization_id 컬럼이 없으므로 query()를 쓸 수 없다.
    대신 id를 같은 방식으로 강제해 다른 조직을 볼 수 없게 한다.
    """
    from src.db.models import Organization

    return select(Organization).where(Organization.id == organization_id)
