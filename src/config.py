from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+psycopg://codetrace:codetrace@localhost:5432/codetrace"
    cors_origins: str = "http://localhost:5173"
    # 프리뷰 배포 주소는 배포마다 달라져 고정 목록으로 감당할 수 없다.
    cors_origin_regex: str = r"https://codetrace-frontend(-[a-z0-9-]+)?\.vercel\.app"
    # 배포 환경에서는 반드시 환경변수로 덮어쓴다 (Render는 자동 생성값을 주입한다).
    secret_key: str = "local-development-secret-key-change-in-production"

    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_org_id: str = ""
    llm_model: str = ""

    # 프론트 배포 주소. 설치 콜백 리다이렉트, PR 경고 코멘트 링크(§8)가 공통으로 쓴다.
    frontend_url: str = "http://localhost:5173"

    # 인덱싱용 레포 클론이 쌓이는 곳. git log -L에 전체 이력이 필요해 얕은 클론을 못 쓴다.
    # 배포 환경의 디스크는 재시작하면 비워지지만, 없으면 다시 클론하므로 문제되지 않는다.
    clone_root: str = "./local/clones"

    # 데모 세션이 보여줄 레포. 시드 스크립트(python -m src.demo)가 쓴다.
    demo_repo: str = "acme-payments/acme-payment-service"
    # 데모 조직에 심을 GitHub App 설치 ID. 0이면 스크립트가 연동된 조직에서 찾는다.
    # .env.example을 복사하면 빈 문자열로 들어오는데, 그대로 두면 int 파싱이 실패해
    # 서버가 아예 뜨지 않는다. 값을 안 넣은 것은 오류가 아니라 기본값이다.
    demo_installation_id: int = 0

    @field_validator("demo_installation_id", mode="before")
    @classmethod
    def _blank_is_zero(cls, value: object) -> object:
        return 0 if value == "" else value

    github_app_id: str = ""
    # 설치 URL(https://github.com/apps/<slug>/installations/new)에 쓰는 앱 slug.
    # app_id(JWT iss)와 다른 값이라 별도로 받는다.
    github_app_slug: str = ""
    # 로컬은 .pem 파일 경로, 배포 환경은 키 내용을 직접 넣는다
    # (Render 같은 PaaS에는 파일을 올릴 수 없다). 내용이 있으면 그쪽을 쓴다.
    github_app_private_key: str = ""
    github_app_private_key_path: str = ""
    github_webhook_secret: str = ""

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, value: str) -> str:
        """호스팅 서비스가 주입하는 접속 문자열에 드라이버를 붙인다.

        Render·AWS는 postgres:// 또는 postgresql:// 형식으로 DATABASE_URL을 준다.
        SQLAlchemy가 psycopg3를 쓰게 하려면 postgresql+psycopg:// 여야 한다.
        """
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def github_private_key(self) -> str:
        """GitHub App 개인키 내용. 없으면 빈 문자열을 반환한다.

        환경변수로 넣은 키는 줄바꿈이 \\n 문자로 들어오는 경우가 많아 되돌려준다.
        """
        if self.github_app_private_key:
            return self.github_app_private_key.replace("\\n", "\n")
        if self.github_app_private_key_path:
            return Path(self.github_app_private_key_path).read_text(encoding="utf-8")
        return ""


settings = Settings()
