from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/portfolio_intel"
    openai_api_key: str = ""
    firebase_project_id: str = ""
    resend_api_key: str = ""
    slack_bot_token: str = ""
    app_base_url: str = "http://localhost:8000"
    newsapi_key: str = ""
    newsapi_url: str = "https://newsapi.org/v2/everything"
    newsapi_query: str = "private equity OR portfolio company OR M&A OR funding"
    newsapi_page_size: int = 25


settings = Settings()

