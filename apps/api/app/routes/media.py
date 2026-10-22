"""项目媒体库：图片上传（素材轨道的存储侧）。"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Project
from ..services import storage

router = APIRouter(prefix="/api/projects/{project_id}/media", tags=["media"])

_ALLOWED = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_MAX_BYTES = 10 * 1024 * 1024


@router.post("")
def upload_media(project_id: str, file: UploadFile, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if file.content_type not in _ALLOWED:
        raise HTTPException(422, f"仅支持 {'/'.join(sorted(_ALLOWED))}，收到 {file.content_type}")
    data = file.file.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(422, "图片超过 10MB")
    if len(data) < 100:
        raise HTTPException(422, "空文件")
    key = f"projects/{project_id}/media/{uuid.uuid4().hex}{_ALLOWED[file.content_type]}"
    tmp = Path("/tmp") / f"media-up-{uuid.uuid4().hex}"
    tmp.write_bytes(data)
    storage.upload(tmp, key, file.content_type)
    tmp.unlink(missing_ok=True)
    return {"key": key, "url": storage.presigned_get(key),
            "bytes": len(data), "content_type": file.content_type}
