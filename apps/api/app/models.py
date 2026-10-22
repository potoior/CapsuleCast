from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Aspect(str, enum.Enum):
    A16_9 = "16:9"
    A9_16 = "9:16"
    A1_1 = "1:1"


class ProjectStatus(str, enum.Enum):
    DRAFT = "draft"                    # 已创建（主题模式：待扩写）
    OUTLINING = "outlining"            # 大纲生成中
    OUTLINE_READY = "outline_ready"    # 大纲就绪（可编辑/确认/直出视频）
    STORYBOARDING = "storyboarding"    # 分镜生成中
    STORYBOARD_READY = "storyboard_ready"  # 分镜就绪（可编辑/确认）
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(120), default="未命名项目")
    mode: Mapped[str] = mapped_column(String(16), default="script")     # topic | script
    topic: Mapped[str | None] = mapped_column(String(200), nullable=True)
    outline: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 大纲（可编辑）
    direct_to_video: Mapped[bool] = mapped_column(Boolean, default=False)
    script_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    aspect: Mapped[str] = mapped_column(Enum(Aspect), default=Aspect.A16_9)
    duration_target_s: Mapped[int] = mapped_column(Integer, default=0)  # 看完剧本后选择；0=标准
    pacing_mode: Mapped[str] = mapped_column(String(16), default="standard")  # standard|compact|relaxed
    voice: Mapped[str] = mapped_column(String(32), default="zf_xiaobei")
    bgm: Mapped[str | None] = mapped_column(String(16), nullable=True)  # auto|none|warm|pulse|ambient
    theme: Mapped[str] = mapped_column(String(8), default="warm")  # warm|dark|brand
    brand_accent: Mapped[str | None] = mapped_column(String(9), nullable=True)  # #RRGGBB
    preset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 来源预设
    cover_brand: Mapped[str | None] = mapped_column(String(24), nullable=True)  # 封面落款
    sfx_enabled: Mapped[bool] = mapped_column(Boolean, default=True)     # 音效层
    progress_bar: Mapped[bool] = mapped_column(Boolean, default=True)    # 顶部进度条
    logo_path: Mapped[str | None] = mapped_column(String(200), nullable=True)  # 项目内相对路径
    logo_pos: Mapped[str] = mapped_column(String(4), default="br")       # tr|br|bl
    logo_opacity: Mapped[float] = mapped_column(Float, default=0.85)
    cap_style: Mapped[str] = mapped_column(String(16), default="standard")  # standard|large|high
    series: Mapped[str | None] = mapped_column(String(60), nullable=True)   # 栏目名
    episode_no: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 期数
    status: Mapped[str] = mapped_column(
        Enum(ProjectStatus), default=ProjectStatus.DRAFT, index=True
    )
    engine: Mapped[str] = mapped_column(String(16), default="rule")  # rule | llm
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    scenes: Mapped[list["Scene"]] = relationship(
        back_populates="project",
        order_by="Scene.idx",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )


class Scene(Base):
    __tablename__ = "scenes"
    __table_args__ = (Index("ix_scenes_project_idx", "project_id", "idx"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    voice_text: Mapped[str] = mapped_column(Text, nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, default=dict)
    voice_s: Mapped[float | None] = mapped_column(Float, nullable=True)   # 自然语速(1.0)的 TTS 时长
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)  # 当前 pacing 下的场景时长
    start_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    snapshot_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    voice_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # 预览试听语音
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped["Project"] = relationship(back_populates="scenes")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(24), default="render")  # storyboard | render
    status: Mapped[str] = mapped_column(Enum(JobStatus), default=JobStatus.QUEUED, index=True)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 任务参数（如 extra_aspects）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    project: Mapped["Project"] = relationship(back_populates="jobs")


class JobLog(Base):
    __tablename__ = "job_logs"
    __table_args__ = (
        Index("ix_job_logs_job_id_ts", "job_id", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    level: Mapped[str] = mapped_column(String(10), default="info")  # info | warn | error
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)


class Preset(Base):
    """风格预设：矩阵账号的视觉/听觉配置一键套用。"""
    __tablename__ = "presets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    # config: {theme, brand_accent, voice, bgm, aspect, cover_brand, title_prefix}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # mp4 | poster | checks | composition
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
