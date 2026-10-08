import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    ha_url: str
    ha_token: str
    app_user: str
    app_password: str
    db_path: str


def load_settings() -> Settings:
    return Settings(
        ha_url=os.environ.get("HA_URL", "http://localhost:8123").rstrip("/"),
        ha_token=os.environ.get("HA_TOKEN", ""),
        app_user=os.environ.get("APP_USER", ""),
        app_password=os.environ.get("APP_PASSWORD", ""),
        db_path=os.environ.get("DB_PATH", "/data/layout.db"),
    )
