from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/portfolio_intel"
    openai_api_key: str = ""
    firebase_project_id: str = ""
    resend_api_key: str = ""
    slack_bot_token: str = ""
    app_base_url: str = "http://localhost:8000"


settings = Settings()

