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
