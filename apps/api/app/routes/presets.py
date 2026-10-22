"""风格预设：矩阵账号的视觉/听觉配置一键套用。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Preset

router = APIRouter(prefix="/api/presets", tags=["presets"])

_VALID = {
    "theme": {"warm", "dark", "broadsheet", "neon", "journal", "swiss", "inkwash", "vaporwave", "film", "blueprint", "chalkboard", "brand"},
    "bgm": {"auto", "none", "warm", "pulse", "ambient"},
    "aspect": {"16:9", "9:16", "1:1"},
}


class PresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    theme: str = "warm"
    brand_accent: str = Field(default="", max_length=9)
    voice: str = Field(default="zf_xiaobei", max_length=32)
    bgm: str = "auto"
    aspect: str = "16:9"
    cover_brand: str = Field(default="", max_length=24)
    title_prefix: str = Field(default="", max_length=24)


class PresetOut(BaseModel):
    id: str
    name: str
    config: dict

    model_config = {"from_attributes": True}


def _validate(p: PresetIn) -> None:
    for k, allowed in _VALID.items():
        v = getattr(p, k)
        if v not in allowed:
            raise HTTPException(422, f"{k} 不合法: {v}")
    if p.theme == "brand" and not p.brand_accent.startswith("#"):
        raise HTTPException(422, "brand 主题需要 #RRGGBB 强调色")


@router.get("", response_model=list[PresetOut])
def list_presets(db: Session = Depends(get_db)):
    return db.scalars(select(Preset).order_by(Preset.created_at.desc())).all()


@router.post("", response_model=PresetOut)
def save_preset(payload: PresetIn, db: Session = Depends(get_db)):
    _validate(payload)
    cfg = {
        "theme": payload.theme, "brand_accent": payload.brand_accent,
        "voice": payload.voice, "bgm": payload.bgm, "aspect": payload.aspect,
        "cover_brand": payload.cover_brand, "title_prefix": payload.title_prefix,
    }
    p = Preset(id=uuid.uuid4().hex, name=payload.name.strip(), config=cfg)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/{preset_id}")
def delete_preset(preset_id: str, db: Session = Depends(get_db)):
    p = db.get(Preset, preset_id)
    if not p:
        raise HTTPException(404, "预设不存在")
    if (p.config or {}).get("builtin"):
        raise HTTPException(409, "内置预设不可删除")
    db.delete(p)
    db.commit()
    return {"ok": True}
