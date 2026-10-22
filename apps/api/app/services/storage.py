"""对象存储抽象：MVP 用本地磁盘实现（无外部依赖）。

接口与 S3/MinIO 同形（upload / presigned_get / object_stat），
以后接多租户或分布式时，替换此实现为 S3 即可，调用方不用动。

存储布局：STORAGE_DIR/<object_key>，object_key 形如
  projects/{id}/renders/{job}.mp4 · projects/{id}/scenes/{idx}.png
URL：/api/files/<object_key>（由 routes/files.py 提供，支持 Range，
前端经 Next.js rewrites 代理到 FastAPI）。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)


def _storage_dir() -> Path:
    s = get_settings()
    d = s.storage_dir_path
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_key(key: str) -> Path:
    """object_key → 绝对路径，拒绝任何越界（路径穿越防护）。"""
    p = (_storage_dir() / key).resolve()
    root = _storage_dir().resolve()
    if root != p and root not in p.parents:
        raise ValueError(f"非法对象 key: {key}")
    return p


def upload(local: Path, object_key: str, content_type: str = "application/octet-stream") -> str:
    dst = _safe_key(object_key)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local, dst)
    return object_key


def presigned_get(object_key: str, expires_days: int = 7) -> str:
    # 本地实现：无签名概念，直接给 API 文件路由的稳定相对 URL
    return f"/api/files/{object_key}"


def object_stat(object_key: str) -> int | None:
    try:
        return _safe_key(object_key).stat().st_size
    except FileNotFoundError:
        return None


def local_path(object_key: str) -> Path:
    return _safe_key(object_key)
