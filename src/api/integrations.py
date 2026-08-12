"""연동 설정 (GitHub App) API. docs/api-spec.md §2.

설치→콜백→레포 목록 조회까지의 흐름을 처리한다.
레포를 선택해 인덱싱을 시작하는 것(POST /repos)은 §3의 책임이라 여기 없다.
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from src.auth import Ctx, current_user, get_db, writable_user
from src.config import settings
from src.db.models import Repo
from src.db.query import org_query, query
from src.github.app_auth import GitHubAppError, get_installation_account
from src.github.client import list_installation_repos

router = APIRouter(prefix="/integrations", tags=["integrations"])

# GitHub는 설치 완료 후 우리가 넘긴 state를 그대로 콜백에 돌려준다.
# 이 값에 조직 식별자를 서명해 넣어, 누가 설치를 시작했는지 콜백 시점에 복원한다.
_STATE_PURPOSE = "github_install"
_STATE_TTL = timedelta(minutes=10)


def _create_install_state(organization_id: int) -> str:
    payload = {
        "org": organization_id,
        "purpose": _STATE_PURPOSE,
        "exp": datetime.now(timezone.utc) + _STATE_TTL,
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _read_install_state(state: str) -> int:
    try:
        payload = jwt.decode(state, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "설치 상태값이 유효하지 않습니다")
    if payload.get("purpose") != _STATE_PURPOSE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "설치 상태값이 유효하지 않습니다")
    return int(payload["org"])


@router.get("")
def get_integrations(ctx: Ctx = Depends(current_user), db: Session = Depends(get_db)):
    org = db.scalar(org_query(ctx.organization_id)) if ctx.organization_id is not None else None
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "조직을 찾을 수 없습니다")

    github = (
        {
            "status": "connected",
            "installation_id": org.github_installation_id,
            "account": org.github_account,
        }
        if org.github_installation_id
        else {"status": "not_connected"}
    )
    return {
        "github": github,
        "gitlab": {"status": "coming_soon"},
        "jira": {"status": "coming_soon"},
        "slack": {"status": "coming_soon"},
    }


@router.get("/github/install-url")
def get_install_url(ctx: Ctx = Depends(writable_user)):
    if ctx.organization_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "조직을 찾을 수 없습니다")
    state = _create_install_state(ctx.organization_id)
    url = f"https://github.com/apps/{settings.github_app_slug}/installations/new?state={state}"
    return {"url": url}


@router.get("/github/callback", include_in_schema=False)
def github_install_callback(
    installation_id: int = Query(...),
    setup_action: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    """GitHub App 설치 완료 후 GitHub이 브라우저를 이 엔드포인트로 리다이렉트한다.

    프론트가 호출하는 API가 아니라 GitHub이 직접 호출하는 콜백이라
    Authorization 헤더 대신 state로 요청자를 식별한다.
    """
    organization_id = _read_install_state(state)
    org = db.scalar(org_query(organization_id))
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "조직을 찾을 수 없습니다")

    if setup_action in ("install", "update"):
        try:
            account = get_installation_account(installation_id)
        except GitHubAppError:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub 설치 정보를 확인하지 못했습니다")
        org.github_installation_id = installation_id
        org.github_account = account
        db.commit()
    # setup_action == "request": 조직 승인 대기 상태. installation_id를 아직
    # 확정할 수 없으므로 저장하지 않고 연동 화면으로만 돌려보낸다.

    return RedirectResponse(f"{settings.frontend_url}/settings/integrations")


@router.get("/github/repos")
def list_github_repos(ctx: Ctx = Depends(writable_user), db: Session = Depends(get_db)):
    org = db.scalar(org_query(ctx.organization_id)) if ctx.organization_id is not None else None
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "조직을 찾을 수 없습니다")
    if not org.github_installation_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "GitHub App이 설치되어 있지 않습니다")

    try:
        repos = list_installation_repos(org.github_installation_id)
    except GitHubAppError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub 레포 목록을 가져오지 못했습니다")

    existing = {r.github_full_name for r in db.scalars(query(Repo, ctx.organization_id))}
    return {
        "repos": [
            {
                "github_full_name": repo["full_name"],
                "private": repo["private"],
                "already_added": repo["full_name"] in existing,
            }
            for repo in repos
        ]
    }
