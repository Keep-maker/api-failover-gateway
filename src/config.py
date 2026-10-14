"""配置管理模块"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel, Field


class Provider(BaseModel):
    name: str
    base_url: str
    api_key: str
    timeout: int = 30
    enabled: bool = True
    consecutive_failures: int = 0
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None
    last_checked_at: Optional[float] = None


class RouteRule(BaseModel):
    pattern: str
    provider_name: str
    priority: int = 0


class Settings(BaseModel):
    failover_strategy: str = "failover"
    request_timeout: int = 30
    health_check_enabled: bool = False
    health_check_interval: int = 60
    log_level: str = "info"
    host: str = "0.0.0.0"
    port: int = 8000


class AppConfig(BaseModel):
    providers: List[Provider] = []
    routes: List[RouteRule] = []
    settings: Settings = Field(default_factory=Settings)


def _expand_env(value: str) -> str:
    def replacer(match):
        var = match.group(1)
        return os.environ.get(var, match.group(0))
    return re.sub(r"\$\{(\w+)\}", replacer, value)


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    expanded = "\n".join(_expand_env(line) for line in raw.splitlines())
    data = yaml.safe_load(expanded)
    return AppConfig(**data)


def get_config() -> AppConfig:
    config_path = Path(__file__).resolve().parent.parent / "config" / "providers.yaml"
    return load_config(config_path)