from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_root() -> Path:
    """定位项目根目录：环境变量 > 相对本文件的 ../../.."""
    env = os.environ.get("APP_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_find_root() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_root: Path = _find_root()

    # 服务
    database_url: str = "postgresql+psycopg://llm2video:llm2video@127.0.0.1:5433/llm2video"
    redis_url: str = "redis://127.0.0.1:6380/0"
    api_port: int = 8300
    cors_origins: str = "http://localhost:3300,http://127.0.0.1:3300"

    # 工作目录（生成的 hyperframes 项目、TTS 缓存、日志）
    work_dir: Path = Path("/home/nv5000/data/llm2video-work")

    # 对象存储（MVP：本地磁盘；以后换 S3/MinIO 只改 services/storage.py）
    storage_dir: str = ""  # 空 → work_dir/storage

    # HyperFrames
    hf_cli_version: str = "0.8.180"
    hyperframes_browser_path: str = ""
    hf_render_workers: int = 4
    hf_render_quality: str = "delivery"
    hf_render_timeout_s: int = 900
    hf_check_timeout_s: int = 300
    hf_tts_timeout_s: int = 600

    # TTS：edge（微软 Edge TTS，标准普通话，需外网/代理）| kokoro（本地离线）
    tts_provider: str = "edge"
    tts_proxy: str = ""              # Edge TTS 专用代理（留空则用 http(s)_proxy）
    edge_tts_timeout_s: int = 90

    # LLM（可选）
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_fallback_models: str = ""   # 逗号分隔的备用模型（主模型失败时依次切换）
    llm_timeout_s: int = 120

    # 代理
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = "127.0.0.1,localhost"

    # 渲染预算（秒）——超时告警线
    render_budget_s: int = 180

    @property
    def templates_dir(self) -> Path:
        return self.app_root / "templates"

    @property
    def shared_assets_dir(self) -> Path:
        return self.templates_dir / "_shared"

    @property
    def storage_dir_path(self) -> Path:
        return Path(self.storage_dir) if self.storage_dir else (self.work_dir / "storage")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.work_dir.mkdir(parents=True, exist_ok=True)
    return s
