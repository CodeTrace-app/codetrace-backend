"""구독 문의 접수 (이슈 #42).

결제 연동·자동 갱신·영수증은 범위 밖이다. 조직명·담당자·연락처를 받아 두고
안내 문구를 돌려주는 데까지가 전부다 (api-spec §7).
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from src.auth import get_db
from src.db.models import Inquiry
from src.schemas import InquiryCreatedOut, InquiryCreateRequest

router = APIRouter(prefix="/inquiries", tags=["inquiries"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_inquiry(
    payload: InquiryCreateRequest, db: Session = Depends(get_db)
) -> InquiryCreatedOut:
    """구독 문의를 접수한다.

    가입 전 방문자도 요금제 화면에서 문의할 수 있어야 하므로 인증을 요구하지 않는다.
    Inquiry는 조직에 속하지 않는 유일한 테이블이라 query() 래퍼를 쓰지 않는다.
    """
    inquiry = Inquiry(
        organization_name=payload.organization_name,
        contact_name=payload.contact_name,
        contact=payload.contact,
        plan=payload.plan,
    )
    db.add(inquiry)
    db.commit()
    db.refresh(inquiry)

    return InquiryCreatedOut(
        id=inquiry.id,
        message="문의가 접수되었습니다. 1영업일 내 연락드립니다.",
    )
