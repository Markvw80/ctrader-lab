"""Settings: secrets from environment (.env), research defaults from YAML."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ctrader_client_id: str = ""
    ctrader_client_secret: SecretStr = SecretStr("")
    ctrader_access_token: SecretStr = SecretStr("")
    ctrader_refresh_token: SecretStr = SecretStr("")
    ctrader_account_id: str = ""
    ctrader_env: Literal["demo", "live"] = "demo"

    ctlab_data_dir: Path = Field(default=Path("data"))
    ctlab_results_dir: Path = Field(default=Path("results"))
    ctlab_config_dir: Path = Field(default=Path("config"))
    ctlab_git_commit: str = "unknown"


@lru_cache
def env() -> Env:
    return Env()


def load_yaml(name: str) -> dict:
    path = env().ctlab_config_dir / name
    with path.open() as f:
        return yaml.safe_load(f) or {}


def settings() -> dict:
    return load_yaml("settings.yaml")


def symbol_spec(symbol: str) -> dict:
    return load_yaml(f"symbols/{symbol.upper()}.yaml")
