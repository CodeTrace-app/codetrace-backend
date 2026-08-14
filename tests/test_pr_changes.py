"""PR 변경 판별 (이슈 #24). 인터페이스는 #24 코멘트에서 데이터 담당과 합의했다."""

import pytest

from src.analysis import ChangedFile, detect_changes
from src.db.models import Organization, Reference, Repo, Symbol

# 데모 레포 PR #12가 시연 대상이다 (이슈 #24).
BASE_AUTH = '''SECRET_KEY = "dev-only"


def verify_token(token):
    """토큰을 검증한다."""
    return decode(token, SECRET_KEY)
'''

HEAD_AUTH = '''SECRET_KEY = "dev-only"


def verify_token(token, verify_exp):
    """토큰을 검증한다."""
    return decode(token, SECRET_KEY, verify_exp)
'''


@pytest.fixture
def repo(db_session):
    org = Organization(name="에이크미", slug="acme")
    db_session.add(org)
    db_session.flush()
    row = Repo(organization_id=org.id, name="api", github_full_name="acme/api")
    db_session.add(row)
    db_session.commit()
    return row


def _index(db_session, repo, *, symbols=(), references=()):
    """마지막 인덱싱 결과. 영향받는 위치를 찾는 데만 쓰인다."""
    for ident in symbols:
        path, name = ident.split("::")
        db_session.add(
            Symbol(
                organization_id=repo.organization_id,
                repo_id=repo.id,
                ident=ident,
                name=name,
                path=path,
                kind="function",
                start_line=1,
                end_line=2,
            )
        )
    for source, target, ref_type, line in references:
        db_session.add(
            Reference(
                organization_id=repo.organization_id,
                repo_id=repo.id,
                source_ident=source,
                target_ident=target,
                ref_type=ref_type,
                path=source.split("::")[0],
                line=line,
            )
        )
    db_session.commit()


def _changed(base, head, path="src/auth.py", **kwargs):
    return [ChangedFile(path=path, base_source=base, head_source=head, **kwargs)]


# ── 데모 시나리오 (이슈 #24 완료 조건) ──────────────────────────────────────


def test_데모_PR의_시그니처_변경을_판별한다(db_session, repo):
    _index(
        db_session,
        repo,
        references=[
            ("src/middleware.py::require_login", "src/auth.py::verify_token", "call", 27),
            ("tests/test_auth.py::test_verify", "src/auth.py::verify_token", "call", 12),
        ],
    )

    warnings = detect_changes(db_session, repo, _changed(BASE_AUTH, HEAD_AUTH))

    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.change_type == "signature_changed"
    assert warning.symbol == "src/auth.py::verify_token"
    assert warning.detail == "파라미터가 (token)에서 (token, verify_exp)로 바뀌었습니다"


def test_영향받는_위치_2곳이_나온다(db_session, repo):
    _index(
        db_session,
        repo,
        references=[
            ("src/middleware.py::require_login", "src/auth.py::verify_token", "call", 27),
            ("tests/test_auth.py::test_verify", "src/auth.py::verify_token", "call", 12),
        ],
    )

    impacted = detect_changes(db_session, repo, _changed(BASE_AUTH, HEAD_AUTH))[0].impacted

    assert [(i.symbol, i.path, i.line, i.type) for i in impacted] == [
        ("src/middleware.py::require_login", "src/middleware.py", 27, "call"),
        ("tests/test_auth.py::test_verify", "tests/test_auth.py", 12, "call"),
    ]


# ── 경고하지 않는 변경 (소음 방지) ──────────────────────────────────────────


def test_타입_힌트만_바꾸면_경고하지_않는다(db_session, repo):
    base = "def verify(token):\n    return token\n"
    head = "def verify(token: str) -> bool:\n    return token\n"

    assert detect_changes(db_session, repo, _changed(base, head)) == []


def test_기본값만_바꾸면_경고하지_않는다(db_session, repo):
    base = "def verify(token, retry=3):\n    return token\n"
    head = "def verify(token, retry=5):\n    return token\n"

    assert detect_changes(db_session, repo, _changed(base, head)) == []


def test_주석과_공백만_바꾸면_경고하지_않는다(db_session, repo):
    base = "def verify(token):\n    # 검증\n    return token\n"
    head = "def verify(token):\n\n    # 토큰을 검증한다\n\n    return token\n"

    assert detect_changes(db_session, repo, _changed(base, head)) == []


def test_내부_로직만_바꾸면_경고하지_않는다(db_session, repo):
    base = "def verify(token):\n    return decode(token)\n"
    head = "def verify(token):\n    checked = decode(token)\n    return checked\n"

    assert detect_changes(db_session, repo, _changed(base, head)) == []


def test_함수를_새로_추가하면_경고하지_않는다(db_session, repo):
    """깨질 것이 없다."""
    base = "def verify(token):\n    return token\n"
    head = "def verify(token):\n    return token\n\n\ndef refresh(token):\n    return token\n"

    assert detect_changes(db_session, repo, _changed(base, head)) == []


def test_새_파일은_경고하지_않는다(db_session, repo):
    head = "def verify(token):\n    return token\n"

    assert detect_changes(db_session, repo, _changed(None, head)) == []


def test_소스가_둘_다_없으면_무시한다(db_session, repo):
    assert detect_changes(db_session, repo, _changed(None, None)) == []


def test_파서가_없는_파일은_건너뛴다(db_session, repo):
    assert detect_changes(db_session, repo, _changed("# 문서\n", "", path="README.md")) == []


def test_문법_오류가_있어도_다른_파일_검사는_계속한다(db_session, repo):
    """작업 중인 코드가 섞여 있어도 PR 검사 전체가 실패하면 안 된다."""
    broken = ChangedFile(path="src/broken.py", base_source="def f(:\n", head_source="def f(\n")
    real = ChangedFile(
        path="src/auth.py",
        base_source="def verify(token):\n    return token\n",
        head_source="def verify(token, retry):\n    return token\n",
    )

    warnings = detect_changes(db_session, repo, [broken, real])

    # 깨진 파일이 뒤 파일 검사를 막지 않는다. 깨진 쪽이 무엇으로 판정되든 상관없다.
    assert ("signature_changed", "src/auth.py::verify") in [
        (w.change_type, w.symbol) for w in warnings
    ]


# ── 삭제·이름 변경 ─────────────────────────────────────────────────────────


def test_함수가_사라지면_삭제로_본다(db_session, repo):
    _index(
        db_session,
        repo,
        references=[("src/api.py::run", "src/auth.py::verify", "call", 9)],
    )
    base = "def verify(token):\n    return token\n"

    warnings = detect_changes(db_session, repo, _changed(base, "x = 1\n"))

    assert [(w.change_type, w.symbol) for w in warnings] == [("deleted", "src/auth.py::verify")]
    assert warnings[0].impacted[0].symbol == "src/api.py::run"


def test_파라미터가_같으면_이름_변경으로_본다(db_session, repo):
    base = "def verify_token(token):\n    return token\n"
    head = "def check_token(token):\n    return token\n"

    warnings = detect_changes(db_session, repo, _changed(base, head))

    assert [(w.change_type, w.detail) for w in warnings] == [
        ("renamed", "verify_token에서 check_token로 이름이 바뀌었습니다")
    ]


def test_짝이_여럿이면_이름_변경으로_추측하지_않는다(db_session, repo):
    """어느 것이 어느 것으로 바뀌었는지 알 수 없다. 틀린 경고를 내느니 삭제로 둔다."""
    base = "def a(token):\n    return token\n"
    head = "def b(token):\n    return token\n\n\ndef c(token):\n    return token\n"

    warnings = detect_changes(db_session, repo, _changed(base, head))

    assert [w.change_type for w in warnings] == ["deleted"]


def test_파일_이름이_바뀌어도_같은_함수는_경고하지_않는다(db_session, repo):
    """previous_path가 없으면 안의 함수가 전부 삭제로 쏟아진다."""
    source = "def verify(token):\n    return token\n"

    warnings = detect_changes(
        db_session,
        repo,
        _changed(source, source, path="src/auth_service.py", previous_path="src/auth.py"),
    )

    assert warnings == []


def test_파일_이름이_바뀐_경우_경고는_옛_경로로_낸다(db_session, repo):
    """저장된 인덱스가 옛 경로로 참조를 갖고 있다."""
    _index(
        db_session,
        repo,
        references=[("src/api.py::run", "src/auth.py::verify", "call", 3)],
    )
    base = "def verify(token):\n    return token\n"
    head = "def verify(token, retry):\n    return token\n"

    warning = detect_changes(
        db_session,
        repo,
        _changed(base, head, path="src/auth_service.py", previous_path="src/auth.py"),
    )[0]

    assert warning.symbol == "src/auth.py::verify"
    assert warning.impacted[0].symbol == "src/api.py::run"


# ── 상수 ───────────────────────────────────────────────────────────────────


def test_상수_값이_바뀌면_경고한다(db_session, repo):
    _index(
        db_session,
        repo,
        references=[("src/payment.py::process", "src/config.py::TIMEOUT", "constant", 8)],
    )
    base = "TIMEOUT = 10\n"
    head = "TIMEOUT = 30\n"

    warnings = detect_changes(db_session, repo, _changed(base, head, path="src/config.py"))

    assert [(w.change_type, w.symbol, w.detail) for w in warnings] == [
        ("constant_changed", "src/config.py::TIMEOUT", "값이 10에서 30로 바뀌었습니다")
    ]
    assert warnings[0].impacted[0].type == "constant"


def test_상수_줄만_밀리면_경고하지_않는다(db_session, repo):
    """줄 번호가 아니라 내용을 비교한다."""
    base = "TIMEOUT = 10\n"
    head = "# 설정\n\nTIMEOUT = 10\n"

    assert detect_changes(db_session, repo, _changed(base, head, path="src/config.py")) == []


def test_상수의_포맷만_바꾸면_경고하지_않는다(db_session, repo):
    """값을 그대로 두고 줄을 나눈 것은 값 변경이 아니다."""
    base = 'PLANS = {"starter": 50, "team": 120}\n'
    head = 'PLANS = {\n    "starter": 50,\n    "team": 120,\n}\n'

    assert detect_changes(db_session, repo, _changed(base, head, path="src/config.py")) == []


def test_튜플의_후행_쉼표는_값의_일부다(db_session, repo):
    """(1,)은 튜플이고 (1)은 숫자다. 포맷 차이가 아니다."""
    base = "SIZES = (1,)\n"
    head = "SIZES = (1)\n"

    warnings = detect_changes(db_session, repo, _changed(base, head, path="src/config.py"))

    assert [w.change_type for w in warnings] == ["constant_changed"]


def test_문자열_상수_안의_공백_변화는_잡는다(db_session, repo):
    """공백을 전부 지워서 비교하면 이걸 놓친다."""
    base = 'OWNER = "김 철수"\n'
    head = 'OWNER = "김철수"\n'

    warnings = detect_changes(db_session, repo, _changed(base, head, path="src/config.py"))

    assert [w.change_type for w in warnings] == ["constant_changed"]


def test_긴_값은_줄여서_안내한다(db_session, repo):
    """코멘트와 화면에 그대로 실리는 문구다."""
    base = f'MESSAGE = "{"가" * 200}"\n'
    head = 'MESSAGE = "짧게"\n'

    detail = detect_changes(db_session, repo, _changed(base, head, path="src/config.py"))[0].detail

    assert "..." in detail
    assert len(detail) < 120


# ── impacted 구성 ──────────────────────────────────────────────────────────


def test_import도_영향으로_본다(db_session, repo):
    """그래프 노드로는 안 만들지만 영향 범위의 근거는 된다 (#24 합의)."""
    _index(
        db_session,
        repo,
        references=[("src/api.py::<module>", "src/auth.py::verify", "import", 1)],
    )
    base = "def verify(token):\n    return token\n"
    head = "def verify(token, retry):\n    return token\n"

    impacted = detect_changes(db_session, repo, _changed(base, head))[0].impacted

    assert [(i.symbol, i.type) for i in impacted] == [("src/api.py::<module>", "import")]


def test_같은_곳에서_여러_번_불러도_한_번만_알린다(db_session, repo):
    _index(
        db_session,
        repo,
        references=[
            ("src/api.py::run", "src/auth.py::verify", "call", 5),
            ("src/api.py::run", "src/auth.py::verify", "call", 5),
            ("src/api.py::run", "src/auth.py::verify", "call", 9),
        ],
    )
    base = "def verify(token):\n    return token\n"
    head = "def verify(token, retry):\n    return token\n"

    impacted = detect_changes(db_session, repo, _changed(base, head))[0].impacted

    assert [i.line for i in impacted] == [5, 9]


def test_다른_조직의_참조는_섞이지_않는다(db_session, repo):
    other = Organization(name="남의회사", slug="other")
    db_session.add(other)
    db_session.flush()
    db_session.add(
        Reference(
            organization_id=other.id,
            repo_id=repo.id,
            source_ident="src/spy.py::peek",
            target_ident="src/auth.py::verify",
            ref_type="call",
            path="src/spy.py",
            line=1,
        )
    )
    db_session.commit()
    base = "def verify(token):\n    return token\n"
    head = "def verify(token, retry):\n    return token\n"

    assert detect_changes(db_session, repo, _changed(base, head))[0].impacted == []
