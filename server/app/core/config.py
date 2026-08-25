from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    app_name: str = "kosmos-hub"
    app_env: str = "development"
    app_secret_key: str = Field(min_length=32)
    database_url: str
    public_base_url: str = "http://127.0.0.1:8000"
    auto_create_tables: bool = True
    auto_verify_domains: str = ""
    wordpress_bridge_min_version: str = "0.1.0"
    fleet_updates_auto_refresh: bool = True
    fleet_updates_refresh_interval_hours: int = Field(default=24, ge=1, le=168)
    fleet_updates_initial_delay_seconds: int = Field(default=300, ge=0, le=3600)

    @property
    def auto_verify_domain_list(self) -> list[str]:
        values = [value.strip().lower() for value in self.auto_verify_domains.split(",")]
        return [value for value in values if value]


@lru_cache
def get_settings() -> Settings:
    return Settings()
