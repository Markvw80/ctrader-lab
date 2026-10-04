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


def symbol_spec(symbol: str, broker: bool = True) -> dict:
    """Symbol config from YAML, overridden by broker values fetched from the API (data/broker/)."""
    spec = load_yaml(f"symbols/{symbol.upper()}.yaml")
    if broker:
        from ctlab.broker.spec import _deep_merge, load_overrides

        spec = _deep_merge(spec, load_overrides(symbol))
    return spec


def _token_file() -> Path:
    return env().ctlab_data_dir / "broker" / ".tokens.json"


def tokens() -> tuple[str, str]:
    """(access_token, refresh_token). Tokens saved by `ctlab api refresh-token` win over .env."""
    import json

    p = _token_file()
    if p.exists():
        t = json.loads(p.read_text())
        return t["access_token"], t["refresh_token"]
    e = env()
    return e.ctrader_access_token.get_secret_value(), e.ctrader_refresh_token.get_secret_value()


def save_tokens(access_token: str, refresh_token: str, expires_in: int) -> Path:
    import json
    import os
    from datetime import UTC, datetime, timedelta

    p = _token_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    expires = datetime.now(UTC) + timedelta(seconds=expires_in)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"access_token": access_token, "refresh_token": refresh_token,
                   "expires_at": expires.isoformat(timespec="seconds")}, f)
    return p
