from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ── 项目 ────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    title: str = Field(default="", max_length=120)
    mode: Literal["topic", "script"] = "script"
    topic: str = Field(default="", max_length=200)
    script_text: str = Field(default="", max_length=4000)
    aspect: Literal["16:9", "9:16", "1:1"] = "16:9"
    voice: str = Field(default="zf_xiaobei", max_length=32)
    bgm: str = Field(default="auto", max_length=16)
    theme: Literal["warm", "dark", "broadsheet", "neon", "journal", "swiss",
                   "inkwash", "vaporwave", "film", "blueprint", "chalkboard", "brand"] = ""
    style: str = Field(default="", max_length=16)  # theme 的别名（两者给一个即可）
    brand_accent: str = Field(default="", max_length=9)
    direct: bool = False  # 全自动直出（大纲→分镜→渲染一气呵成，跳过确认）
    preset_id: str = Field(default="", max_length=32)  # 风格预设：服务端展开覆盖各字段
    duration_target_s: int = Field(default=0, ge=0, le=300)  # 目标时长（0=自动~75s；决定大纲层次量）

    @field_validator("topic")
    @classmethod
    def topic_ok(cls, v: str, info) -> str:
        if info.data.get("mode") == "topic" and len((v or "").strip()) < 2:
            raise ValueError("主题模式需要至少 2 个字的主题")
        return (v or "").strip()

    @field_validator("script_text")
    @classmethod
    def not_blank(cls, v: str, info) -> str:
        if info.data.get("mode", "script") == "script" and len((v or "").strip()) < 10:
            raise ValueError("文案至少 10 个字（或改用主题模式）")
        return (v or "").strip()


class SceneOut(BaseModel):
    id: str
    idx: int
    template_id: str
    voice_text: str
    fields: dict[str, Any]
    voice_s: float | None
    duration_s: float | None
    start_s: float | None
    snapshot_url: str | None
    voice_url: str | None

    model_config = {"from_attributes": True}


class JobOut(BaseModel):
    id: str
    kind: str
    status: str
    stage: str | None
    progress: int
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ArtifactOut(BaseModel):
    id: str
    kind: str
    url: str
    bytes: int | None
    meta: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class PacingRequest(BaseModel):
    mode: Literal["standard", "compact", "relaxed"] = "standard"
    target_s: int = Field(default=0, ge=0, le=300)
    bgm: Literal["auto", "none", "warm", "pulse", "ambient"] = "auto"


class PacingOption(BaseModel):
    mode: str
    label: str
    target_s: int
    hint: str


class OutlineConfirm(BaseModel):
    direct: bool = False   # True = 跳过分镜预览，直接生成视频


class ProjectOut(BaseModel):
    id: str
    title: str
    mode: str
    topic: str | None
    outline: dict[str, Any] | None
    direct_to_video: bool
    script_text: str
    aspect: str
    duration_target_s: int
    pacing_mode: str
    voice: str
    bgm: str = "auto"
    theme: str = "warm"
    brand_accent: str | None = None
    status: str
    engine: str
    error: str | None
    created_at: datetime
    updated_at: datetime
    scenes: list[SceneOut] = []
    jobs: list[JobOut] = []
    artifacts: list[ArtifactOut] = []
    natural_duration_s: float = 0.0          # 自然语速时长（分镜后有效）
    duration_options: list[PacingOption] = []
    sfx_enabled: bool = True
    progress_bar: bool = True
    logo_set: bool = False
    logo_pos: str = "br"
    logo_opacity: float = 0.85
    cap_style: str = "standard"
    series: str | None = None
    episode_no: int | None = None

    model_config = {"from_attributes": True}


# ── 场景编辑 ────────────────────────────────────────────────

class SceneUpdate(BaseModel):
    voice_text: str | None = Field(default=None, min_length=4, max_length=500)
    fields: dict[str, Any] | None = None
    template_id: str | None = Field(default=None, max_length=64)  # 换模板


class ScenesReorder(BaseModel):
    scene_ids: list[str]


# ── 渲染 ────────────────────────────────────────────────────

class RenderRequest(BaseModel):
    force: bool = False  # 忽略阶段缓存，全量重跑
    extra_aspects: list[Literal["16:9", "9:16", "1:1"]] = []  # 同时输出的附加画幅
    ab_variants: list[dict[str, str]] = []  # A/B 对照版 [{"voice": "..."}, {"theme": "..."}] 最多 2 组


class LogLine(BaseModel):
    ts: datetime
    level: str
    stage: str | None
    message: str


# ── 模板 ────────────────────────────────────────────────────

class TemplateFieldSchema(BaseModel):
    key: str
    label: str
    type: str  # text | textarea | list
    max_length: int | None = None
    required: bool = True
    item_max_length: int | None = None
    min_items: int | None = None
    max_items: int | None = None
    description: str = ""


class TemplateOut(BaseModel):
    id: str
    name: str
    description: str
    role: str
    aspects: list[str]
    motion: str
    min_duration_s: float
    max_duration_s: float
    fields: list[TemplateFieldSchema]
    example: dict[str, Any]
    user: bool = False
