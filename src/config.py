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

    github_app_id: str = ""
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


settings = Settings()
