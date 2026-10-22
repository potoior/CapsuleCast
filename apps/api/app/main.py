"""FastAPI 入口：建表、CORS、路由挂载、健康检查。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import Base, engine
from .routes import files, jobs, projects, system, templates

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    _migrate()
    _seed_presets()
    # 存储目录预热
    from .services import storage
    storage._storage_dir()
    yield


def _seed_presets() -> None:
    """内置风格预设：首次启动播种；已存在同名则跳过。"""
    import uuid

    from sqlalchemy import select

    from .db import SessionLocal
    from .models import Preset

    BUILTINS = [
        ("夜航科技", {"theme": "dark", "voice": "zm_yunjian", "bgm": "pulse",
                    "aspect": "16:9", "cover_brand": "科技夜谈", "builtin": True}),
        ("手账日常", {"theme": "journal", "voice": "zf_xiaoni", "bgm": "ambient",
                    "aspect": "9:16", "cover_brand": "知识手账", "builtin": True}),
        ("社论观察", {"theme": "broadsheet", "voice": "zm_yunyang", "bgm": "warm",
                    "aspect": "16:9", "cover_brand": "深度观察", "builtin": True}),
        ("瑞士课堂", {"theme": "swiss", "voice": "zf_xiaobei", "bgm": "none",
                    "aspect": "16:9", "cover_brand": "三分钟课堂", "builtin": True}),
        ("水墨讲谈", {"theme": "inkwash", "voice": "zm_yunxi", "bgm": "warm",
                    "aspect": "16:9", "cover_brand": "水墨讲谈", "builtin": True}),
    ]
    db = SessionLocal()
    try:
        existing = {p.name for p in db.scalars(select(Preset)).all()}
        added = 0
        for name, cfg in BUILTINS:
            if name not in existing:
                db.add(Preset(id=uuid.uuid4().hex, name=name, config=cfg))
                added += 1
        if added:
            db.commit()
    finally:
        db.close()


def _migrate() -> None:
    """轻量幂等迁移：create_all 不会给已存在的表加列/枚举值。"""
    from sqlalchemy import text

    stmts = [
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS pacing_mode varchar(16) DEFAULT 'standard'",
        "ALTER TABLE scenes ADD COLUMN IF NOT EXISTS voice_s double precision",
        "ALTER TABLE scenes ADD COLUMN IF NOT EXISTS voice_url text",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS mode varchar(16) DEFAULT 'script'",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS topic varchar(200)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS outline jsonb",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS direct_to_video boolean DEFAULT false",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS bgm varchar(16)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS theme varchar(8) DEFAULT 'warm'",
        "ALTER TABLE projects ALTER COLUMN theme TYPE varchar(16)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS brand_accent varchar(9)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS payload jsonb",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS preset_id varchar(32)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS cover_brand varchar(24)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS sfx_enabled boolean DEFAULT true",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS progress_bar boolean DEFAULT true",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS logo_path varchar(200)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS logo_pos varchar(4) DEFAULT 'br'",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS logo_opacity double precision DEFAULT 0.85",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS cap_style varchar(16) DEFAULT 'standard'",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS series varchar(60)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS episode_no integer",
        """CREATE TABLE IF NOT EXISTS presets (
            id varchar(32) PRIMARY KEY,
            name varchar(60) NOT NULL,
            config jsonb DEFAULT '{}',
            created_at timestamptz DEFAULT now()
        )""",
    ]
    enum_stmts = [
        "ALTER TYPE projectstatus ADD VALUE IF NOT EXISTS 'OUTLINING'",
        "ALTER TYPE projectstatus ADD VALUE IF NOT EXISTS 'OUTLINE_READY'",
    ]
    with engine.begin() as conn:
        for s in stmts:
            conn.execute(text(s))
    # ALTER TYPE ADD VALUE 用自动提交（避免同事务限制）
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for s in enum_stmts:
            try:
                conn.execute(text(s))
            except Exception:
                pass


app = FastAPI(title="llm2video 控制台 API", version="0.1.0", lifespan=lifespan)

s = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=s.cors_origin_list or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(jobs.router)
app.include_router(templates.router)
app.include_router(system.router)
app.include_router(files.router)
from .routes import presets  # noqa: E402

app.include_router(presets.router)
from .routes import media  # noqa: E402

app.include_router(media.router)


@app.get("/api/health")
def health():
    return {"ok": True, "service": "llm2video-api"}
