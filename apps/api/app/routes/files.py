"""文件服务：渲染产物（MP4 / 海报 / 快照）的下载与在线播放。

支持 Range（视频拖动播放），带路径穿越防护。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..services import storage

router = APIRouter(prefix="/api/files", tags=["files"])

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webm": "video/webm",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
}


@router.get("/{object_key:path}")
def get_file(object_key: str):
    try:
        p: Path = storage.local_path(object_key)
    except ValueError:
        raise HTTPException(400, "非法路径") from None
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    media_type = _MEDIA_TYPES.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=media_type, filename=p.name)
