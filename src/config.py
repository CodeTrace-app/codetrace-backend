from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+psycopg://codetrace:codetrace@localhost:5432/codetrace"
    cors_origins: str = "http://localhost:5173"
    secret_key: str = "change_me"

    llm_provider: str = "anthropic"
    llm_api_key: str = ""
    llm_model: str = ""

    github_app_id: str = ""
    github_app_private_key_path: str = ""
    github_webhook_secret: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
