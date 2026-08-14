"""데모 조직·사용자와 시드 데이터.

데모 세션은 격리 규칙을 우회하지 않는다. 데모 조직의 organization_id를 가진
읽기 전용 세션일 뿐이고, 그 조직에 데이터가 들어 있어야 화면이 채워진다.

조직을 찾는 규칙이 엔드포인트와 시드 스크립트에 따로 있으면 서로 다른 조직을
만들 수 있다. 그러면 시드는 성공했는데 데모 화면은 비어 있다. 그래서 여기 모은다.

    python -m src.demo                      # 데모 조직에 레포를 넣고 인덱싱한다
    python -m src.demo --repo owner/name    # 다른 레포로
"""

import argparse
import logging
import secrets
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth import hash_password
from src.config import settings
from src.db.models import Organization, Repo, User
from src.db.session import SessionLocal
from src.github.installation import InstallationTaken, bind_installation
from src.indexing.runner import run_indexing
from src.llm.provider import LLMUnavailable, get_provider

logger = logging.getLogger(__name__)

DEMO_SLUG = "demo"
DEMO_EMAIL = "demo@codetrace.app"
DEMO_ORG_NAME = "Acme Corp (데모)"
DEMO_USER_NAME = "데모 사용자"
# 레포 3개까지인 starter로 두면 시드가 한도에 걸릴 수 있다.
DEMO_PLAN = "team"


def get_or_create_demo_org(db: Session) -> Organization:
    """데모 조직을 찾거나 만든다. flush까지만 하고 commit은 부르는 쪽이 한다."""
    org = db.scalar(select(Organization).where(Organization.slug == DEMO_SLUG))
    if org is None:
        org = Organization(
            name=DEMO_ORG_NAME, slug=DEMO_SLUG, plan=DEMO_PLAN, is_demo=True
        )
        db.add(org)
        db.flush()
    return org


def get_or_create_demo_user(db: Session, org: Organization) -> User:
    """데모 사용자를 찾거나 만든다.

    role은 member다. 관리자 설정 화면이 데모 세션에 열리지 않게 하는 기준이다.
    """
    user = db.scalar(select(User).where(User.email == DEMO_EMAIL))
    if user is None:
        user = User(
            organization_id=org.id,
            email=DEMO_EMAIL,
            # 이 계정으로는 로그인할 수 없다. 데모 세션 발급으로만 쓰인다.
            password_hash=hash_password(secrets.token_urlsafe(32)),
            name=DEMO_USER_NAME,
            role="member",
        )
        db.add(user)
        db.flush()
    return user


def resolve_installation_id(
    db: Session, owner: str, explicit: int | None, current: int | None = None
) -> int:
    """데모 레포를 읽을 GitHub App 설치 ID를 정한다.

    인자 > 설정값 > 데모 조직에 이미 심어둔 값 > 같은 계정에 연동된 다른 조직 순.

    `current`(데모 조직에 심어둔 값)를 먼저 보는 이유: 첫 실행이 이 값을 심으므로,
    두 번째 실행부터는 데모 조직 자신이 후보에 섞여 "어느 쪽인지 모르겠다"로 멈춘다.

    마지막 경로는 서버에서 PM이 직접 돌리는 스크립트에서만 쓴다. 요청을 처리하는
    코드가 아니므로 조직 격리 규칙과 무관하다.
    """
    if explicit:
        return explicit
    if settings.demo_installation_id:
        return settings.demo_installation_id
    if current:
        return current

    found = db.scalars(
        select(Organization).where(
            Organization.github_account == owner,
            Organization.github_installation_id.is_not(None),
            # 데모 조직은 이 함수가 값을 심는 대상이다. 후보로 세면 자기 자신과 경쟁한다.
            Organization.slug != DEMO_SLUG,
        )
    ).all()
    if len(found) == 1:
        # 값을 알려줄 뿐 가져오지는 못한다. 한 설치는 한 조직만 가지므로
        # 아래 bind_installation이 이 조직의 연결을 보고 거절한다.
        logger.info("설치 ID를 %s 조직에서 확인했습니다", found[0].name)
        return found[0].github_installation_id  # type: ignore[return-value]

    raise SystemExit(
        f"'{owner}'의 GitHub App 설치 ID를 찾지 못했습니다.\n"
        "--installation-id 로 넘기거나 DEMO_INSTALLATION_ID 환경변수를 설정하세요."
    )


def seed_demo(db: Session, full_name: str, installation_id: int | None = None) -> Repo:
    """데모 조직에 레포를 등록하고 인덱싱한다. 여러 번 돌려도 안전하다.

    인덱싱을 백그라운드로 던지지 않고 여기서 끝까지 돌린다. 스크립트가 먼저
    끝나버리면 결과를 확인할 수 없기 때문이다.
    """
    owner = full_name.split("/")[0]

    org = get_or_create_demo_org(db)
    get_or_create_demo_user(db, org)

    resolved = resolve_installation_id(db, owner, installation_id, org.github_installation_id)
    try:
        # 콜백과 같은 규칙을 지난다. 예전에는 여기서 값을 직접 대입해 검사를 건너뛰었고,
        # 그 결과 같은 설치를 가진 조직이 둘 생겨 PR 경고가 데모에 쌓이지 않았다.
        bind_installation(db, org, resolved, owner)
    except InstallationTaken as taken:
        raise SystemExit(
            f"설치 {resolved}은(는) 이미 '{taken.owner.name}' 조직에 연결되어 있습니다.\n"
            "한 설치는 한 조직만 가질 수 있습니다. 그 조직의 GitHub 연결을 해제한 뒤 다시 돌리세요."
        ) from None

    repo = db.scalar(
        select(Repo).where(
            Repo.organization_id == org.id, Repo.github_full_name == full_name
        )
    )
    if repo is None:
        repo = Repo(
            organization_id=org.id,
            name=full_name.split("/")[-1],
            github_full_name=full_name,
            indexing_status="collecting",
        )
        db.add(repo)
    else:
        # 다시 돌리는 경우다. 이전 상태가 failed로 남아 있어도 여기서 되돌린다.
        repo.indexing_status = "collecting"
        repo.progress_current = None
        repo.progress_total = None

    db.commit()
    db.refresh(repo)
    db.refresh(org)

    run_indexing(repo.id, org.id, org.github_installation_id)

    db.expire_all()
    return db.get(Repo, repo.id)  # type: ignore[return-value]


def summary_blocker() -> str | None:
    """요약이 만들어지지 않을 이유를 찾는다. 없으면 None.

    요약은 인덱싱 중에 만들어 저장한다. 조회할 때 만들지 않는다.
    그래서 설정이 잘못되면 몇 분을 다 돌린 뒤에야 요약이 비어 있는 걸 알게 되고,
    고친 뒤 처음부터 다시 돌려야 한다. 시작 전에 알려준다.
    """
    if not settings.llm_api_key:
        return "LLM_API_KEY가 비어 있습니다"
    try:
        get_provider(settings.llm_provider)
    except LLMUnavailable as reason:
        return str(reason)
    return None


def main() -> None:
    # 윈도우 콘솔 기본 인코딩(cp949)에 없는 글자가 섞이면 출력에서 죽는다.
    # 인덱싱을 다 끝내놓고 마지막 print에서 실패하면 결과를 못 본다.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="데모 조직에 시드 데이터를 넣는다")
    parser.add_argument("--repo", default=settings.demo_repo, help="owner/name")
    parser.add_argument("--installation-id", type=int, default=None)
    args = parser.parse_args()

    blocker = summary_blocker()
    if blocker is not None:
        print(f"경고: 배경 요약이 만들어지지 않습니다 — {blocker}")
        print("      근거(커밋·PR)는 그대로 수집되지만 맥락 탭의 요약 칸이 빕니다.")
        print("      .env를 고치고 다시 돌리려면 지금 Ctrl+C 하세요.\n")

    db = SessionLocal()
    try:
        print(f"{args.repo} 인덱싱을 시작합니다. 몇 분 걸립니다.\n")
        repo = seed_demo(db, args.repo, args.installation_id)

        if repo.indexing_status != "done":
            sys.exit(f"\n인덱싱이 '{repo.indexing_status}' 상태로 끝났습니다. 위 로그를 확인하세요.")

        print(
            f"\n완료. 파일 {repo.files_count} · 함수 {repo.functions_count} · "
            f"커밋 {repo.commits_count} · PR {repo.prs_count}"
        )
        print("랜딩에서 '데모 체험'을 눌러 확인하세요.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
