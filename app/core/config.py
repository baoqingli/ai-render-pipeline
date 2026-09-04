from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARP_", env_file=".env", extra="ignore")

    llm_base_url: str = "http://localhost:8001/v1"
    llm_model: str = "Qwen/Qwen2.5-7B-Instruct"
    llm_api_key: str | None = None
    comfy_url: str = "http://localhost:8188"
    oda_exe: str = "ODAFileConverter"
    blender_exe: str = "blender"
    workspace_dir: str = "experiments"
    registry_db_url: str = "sqlite+aiosqlite:///./registry.db"
    pg_dsn: str = "postgresql://arp:arp@localhost:5432/arp"
    valkey_url: str = "redis://localhost:6379/0"
    render_variants: int = 2
    data_dir: str = "experiments/data"
    gemini_api_key: str | None = None
    openai_api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
