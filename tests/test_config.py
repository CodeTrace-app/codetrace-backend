"""설정이 로컬·배포 환경 양쪽에서 값을 제대로 읽는지 검사한다."""

from src.config import Settings

PEM = "-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n-----END RSA PRIVATE KEY-----"


def test_접속_문자열에_psycopg_드라이버가_붙는다():
    for raw in ("postgres://u:p@h/db", "postgresql://u:p@h/db"):
        assert Settings(database_url=raw).database_url.startswith("postgresql+psycopg://")


def test_이미_드라이버가_있으면_그대로_둔다():
    raw = "postgresql+psycopg://u:p@h/db"
    assert Settings(database_url=raw).database_url == raw


def test_개인키를_환경변수_내용으로_읽는다():
    """배포 환경에는 파일을 올릴 수 없어 내용을 직접 넣는다."""
    assert Settings(github_app_private_key=PEM).github_private_key == PEM


def test_줄바꿈이_이스케이프된_키를_되돌린다():
    """환경변수로 넣으면 줄바꿈이 \\n 문자로 들어오는 경우가 많다."""
    escaped = PEM.replace("\n", "\\n")
    assert Settings(github_app_private_key=escaped).github_private_key == PEM


def test_파일_경로로도_읽는다(tmp_path):
    pem_file = tmp_path / "app.pem"
    pem_file.write_text(PEM, encoding="utf-8")
    settings = Settings(github_app_private_key_path=str(pem_file))
    assert settings.github_private_key == PEM


def test_내용이_있으면_경로보다_우선한다(tmp_path):
    pem_file = tmp_path / "app.pem"
    pem_file.write_text("파일에서 읽은 키", encoding="utf-8")
    settings = Settings(github_app_private_key=PEM, github_app_private_key_path=str(pem_file))
    assert settings.github_private_key == PEM


def test_둘_다_없으면_빈_문자열():
    assert Settings().github_private_key == ""
