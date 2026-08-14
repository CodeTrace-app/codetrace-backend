"""데모 시드.

인덱싱 자체는 test_history_collector.py·test_parsing.py가 검증한다.
여기서는 시드가 데모 조직을 정확히 겨냥하는지, 여러 번 돌려도 안전한지를 본다.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from src import demo as demo_module
from src.db.models import Organization, Repo, User
from src.demo import (
    DEMO_EMAIL,
    DEMO_SLUG,
    get_or_create_demo_org,
    get_or_create_demo_user,
    resolve_installation_id,
    seed_demo,
    summary_blocker,
)

BASE = "/api/v1"
DEMO_REPO = "acme-payments/acme-payment-service"


@pytest.fixture
def fake_indexing(monkeypatch):
    """인덱싱을 돌리지 않고 done으로 만든다. 호출 인자를 남긴다."""
    calls = []

    def fake(repo_id, organization_id, installation_id):
        calls.append((repo_id, organization_id, installation_id))

    monkeypatch.setattr(demo_module, "run_indexing", fake)
    return calls


# ── 조직·사용자 ────────────────────────────────────────────────────────────


def test_두_번_불러도_조직이_하나다(db_session):
    first = get_or_create_demo_org(db_session)
    second = get_or_create_demo_org(db_session)

    assert first.id == second.id
    assert db_session.query(Organization).count() == 1


def test_데모_조직은_데모로_표시된다(db_session):
    org = get_or_create_demo_org(db_session)

    assert org.slug == DEMO_SLUG
    assert org.is_demo is True
    # starter(3개)면 레포를 더 넣을 때 한도에 걸린다
    assert org.repo_limit > 3


def test_데모_사용자는_member다(db_session):
    """admin이면 관리자 설정 화면이 데모 세션에 열린다."""
    org = get_or_create_demo_org(db_session)
    user = get_or_create_demo_user(db_session, org)

    assert user.role == "member"
    assert user.organization_id == org.id


def test_사용자도_두_번_불러도_하나다(db_session):
    org = get_or_create_demo_org(db_session)
    get_or_create_demo_user(db_session, org)
    get_or_create_demo_user(db_session, org)

    assert db_session.query(User).filter_by(email=DEMO_EMAIL).count() == 1


# ── 설치 ID 찾기 ───────────────────────────────────────────────────────────


def test_인자로_준_설치_ID를_그대로_쓴다(db_session):
    assert resolve_installation_id(db_session, "acme-payments", 999) == 999


def test_연동된_조직에서_설치_ID를_가져온다(db_session):
    """PM이 이미 앱을 설치했다면 번호를 다시 찾지 않아도 된다."""
    db_session.add(
        Organization(
            name="에이크미", slug="acme", github_account="acme-payments", github_installation_id=4321
        )
    )
    db_session.commit()

    assert resolve_installation_id(db_session, "acme-payments", None) == 4321


def test_데모_조직에_심어둔_값을_다시_쓴다(db_session):
    """첫 실행이 이 값을 심으므로 두 번째 실행은 이 길로 온다."""
    assert resolve_installation_id(db_session, "acme-payments", None, current=777) == 777


def test_찾을_수_없으면_안내하고_멈춘다(db_session):
    """조용히 0을 쓰면 인덱싱이 인증 오류로 죽고 원인을 알기 어렵다."""
    with pytest.raises(SystemExit) as caught:
        resolve_installation_id(db_session, "acme-payments", None)

    assert "installation-id" in str(caught.value)


def test_다른_조직이_쓰는_설치는_빼앗지_않는다(db_session, fake_indexing):
    """한 설치는 한 조직만 가진다.

    예전에는 다른 조직의 값을 그대로 복사했고, 그 결과 같은 설치를 가진 조직이 둘 생겨
    PR 웹훅이 먼저 만들어진 쪽을 집었다. 데모에는 경고가 영영 쌓이지 않았다.
    """
    db_session.add(
        Organization(
            name="에이크미", slug="acme", github_account="acme-payments", github_installation_id=4321
        )
    )
    db_session.commit()

    with pytest.raises(SystemExit) as caught:
        seed_demo(db_session, DEMO_REPO)

    assert "에이크미" in str(caught.value)
    assert fake_indexing == []


# ── 시드 ───────────────────────────────────────────────────────────────────


def test_레포가_데모_조직에_들어간다(db_session, fake_indexing):
    seed_demo(db_session, DEMO_REPO, installation_id=1234)

    org = db_session.query(Organization).filter_by(slug=DEMO_SLUG).one()
    repo = db_session.query(Repo).one()

    assert repo.organization_id == org.id
    assert repo.github_full_name == DEMO_REPO
    assert repo.name == "acme-payment-service"
    # 연동 없이는 인덱싱도 재인덱싱도 못 한다
    assert org.github_installation_id == 1234
    assert org.github_account == "acme-payments"


def test_인덱싱에_데모_조직_ID가_넘어간다(db_session, fake_indexing):
    """다른 조직 ID로 돌면 데이터가 엉뚱한 조직에 쌓인다."""
    seed_demo(db_session, DEMO_REPO, installation_id=1234)

    org = db_session.query(Organization).filter_by(slug=DEMO_SLUG).one()
    repo = db_session.query(Repo).one()

    assert fake_indexing == [(repo.id, org.id, 1234)]


def test_두_번_돌려도_레포가_하나다(db_session, fake_indexing):
    seed_demo(db_session, DEMO_REPO, installation_id=1234)
    seed_demo(db_session, DEMO_REPO, installation_id=1234)

    assert db_session.query(Repo).count() == 1
    assert len(fake_indexing) == 2


def test_설치_ID_없이_다시_돌려도_동작한다(db_session, fake_indexing):
    """파서가 바뀔 때마다 다시 돌리는 명령이다. 두 번째부터 막히면 안 된다.

    첫 실행이 데모 조직에 설치 ID를 심는다. 그 값을 자기 것으로 알아보지 못하면
    '이미 다른 조직이 쓰고 있다'며 자기 자신을 상대로 멈춘다.
    """
    seed_demo(db_session, DEMO_REPO, installation_id=4321)
    seed_demo(db_session, DEMO_REPO)  # 이번엔 데모 조직에 심어둔 값을 쓴다

    org = db_session.query(Organization).filter_by(slug=DEMO_SLUG).one()
    assert org.github_installation_id == 4321
    assert len(fake_indexing) == 2


def test_실패한_레포도_다시_돌릴_수_있다(db_session, fake_indexing):
    """failed로 남은 상태에서 다시 시드하면 collecting부터 시작한다."""
    seed_demo(db_session, DEMO_REPO, installation_id=1234)
    repo = db_session.query(Repo).one()
    repo.indexing_status = "failed"
    repo.progress_current = 5
    repo.progress_total = 10
    db_session.commit()

    seed_demo(db_session, DEMO_REPO, installation_id=1234)

    db_session.refresh(repo)
    # fake_indexing이 done으로 바꾸지 않으므로 collecting이 남는 것이 정상이다
    assert repo.indexing_status == "collecting"
    assert repo.progress_current is None


# ── 한 설치 = 한 조직 ──────────────────────────────────────────────────────


def test_같은_설치를_두_조직이_가질_수_없다(db_session):
    """앱 코드의 검사를 우회하는 경로가 생겨도 DB가 막는다.

    실제로 데모 시드가 콜백의 검사를 건너뛰고 같은 값을 심은 적이 있다.
    """
    db_session.add(Organization(name="가", slug="a", github_installation_id=555))
    db_session.commit()
    db_session.add(Organization(name="나", slug="b", github_installation_id=555))

    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_연동_전_조직은_여러_개여도_된다(db_session):
    """설치 ID가 비어 있는 것은 '아직 연동 안 함'이지 중복이 아니다."""
    for slug in ("a", "b", "c"):
        db_session.add(Organization(name=slug, slug=slug))
    db_session.commit()

    assert db_session.query(Organization).count() == 3


# ── 요약이 안 만들어질 조건을 미리 알리는가 ────────────────────────────────


def test_키가_없으면_미리_알린다(monkeypatch):
    """다 돌린 뒤에 알면 몇 분을 다시 써야 한다."""
    monkeypatch.setattr(demo_module.settings, "llm_api_key", "")

    assert "LLM_API_KEY" in (summary_blocker() or "")


def test_구현되지_않은_provider도_미리_알린다(monkeypatch):
    """키가 있어도 provider 이름이 틀리면 요약이 전부 실패한다."""
    monkeypatch.setattr(demo_module.settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(demo_module.settings, "llm_provider", "anthropic")

    assert "anthropic" in (summary_blocker() or "")


def test_제대로_설정되면_경고하지_않는다(monkeypatch):
    monkeypatch.setattr(demo_module.settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(demo_module.settings, "llm_provider", "openai")

    assert summary_blocker() is None


# ── 데모 세션과 같은 조직을 보는가 ──────────────────────────────────────────


def test_데모_세션이_시드한_레포를_본다(client, db_session, fake_indexing):
    """시드와 엔드포인트가 서로 다른 조직을 만들면 화면이 비어 보인다."""
    seed_demo(db_session, DEMO_REPO, installation_id=1234)

    session = client.post(f"{BASE}/auth/demo").json()
    listed = client.get(
        f"{BASE}/repos", headers={"Authorization": f"Bearer {session['access_token']}"}
    ).json()

    assert session["read_only"] is True
    assert [r["github_full_name"] for r in listed["repos"]] == [DEMO_REPO]
    assert listed["summary"]["github_connected"] is True
    assert listed["summary"]["github_account"] == "acme-payments"


def test_데모_세션은_레포를_추가할_수_없다(client, db_session, fake_indexing):
    seed_demo(db_session, DEMO_REPO, installation_id=1234)
    session = client.post(f"{BASE}/auth/demo").json()
    headers = {"Authorization": f"Bearer {session['access_token']}"}

    added = client.post(f"{BASE}/repos", json={"github_full_name": "acme-payments/other"}, headers=headers)
    repo_id = db_session.query(Repo).one().id
    reindexed = client.post(f"{BASE}/repos/{repo_id}/reindex", headers=headers)

    assert added.status_code == 403
    assert reindexed.status_code == 403


def test_데모_세션은_관리자_설정을_볼_수_없다(client, db_session, fake_indexing):
    seed_demo(db_session, DEMO_REPO, installation_id=1234)
    session = client.post(f"{BASE}/auth/demo").json()
    headers = {"Authorization": f"Bearer {session['access_token']}"}

    assert client.get(f"{BASE}/admin/plan", headers=headers).status_code == 403
