from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    api_base_url: str = "https://mielenosoitukset.fi/api"
    api_token: str = ""
    poll_minutes: int = 15
    db_path: str = "data/bot.db"
    admin_chat_ids: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
