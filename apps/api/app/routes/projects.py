"""项目路由：创建/列表/详情 + 分镜生成。"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field
from rq import Queue
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..jobs.tasks import QUEUE_NAME, storyboard_task
from ..models import Artifact, Job, JobStatus, Preset, Project, ProjectStatus, Scene
from ..schemas import (
    JobOut,
    OutlineConfirm,
    PacingRequest,
    ProjectCreate,
    ProjectOut,
    SceneUpdate,
)
from ..services.template_engine import TemplateValidationError

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _redis_queue() -> Queue:
    from redis import Redis
    s = get_settings()
    return Queue(QUEUE_NAME, connection=Redis.from_url(s.redis_url))


def _project_out(db: Session, p: Project) -> ProjectOut:
    # artifacts 单独查（relationship 未挂在 Project 上）
    arts = db.scalars(select(Artifact).where(Artifact.project_id == p.id)
                      .order_by(Artifact.created_at.desc())).all()
    natural = sum(s.voice_s or 0.0 for s in p.scenes)
    opts: list[dict] = []
    if natural > 0:
        from ..services.pacing import duration_options
        opts = duration_options(natural)
    return ProjectOut(
        id=p.id, title=p.title,
        mode=p.mode, topic=p.topic, outline=p.outline,
        direct_to_video=p.direct_to_video,
        script_text=p.script_text,
        aspect=p.aspect.value if hasattr(p.aspect, "value") else str(p.aspect),
        duration_target_s=p.duration_target_s,
        pacing_mode=p.pacing_mode,
        voice=p.voice,
        bgm=p.bgm or "auto",
        theme=p.theme or "warm",
        brand_accent=p.brand_accent,
        status=p.status.value if hasattr(p.status, "value") else str(p.status),
        engine=p.engine, error=p.error,
        created_at=p.created_at, updated_at=p.updated_at,
        scenes=[s for s in p.scenes],
        jobs=[j for j in p.jobs],
        artifacts=list(arts),
        natural_duration_s=round(natural, 1),
        duration_options=opts,
        sfx_enabled=bool(getattr(p, "sfx_enabled", True)),
        progress_bar=bool(getattr(p, "progress_bar", True)),
        logo_set=bool(getattr(p, "logo_path", None)),
        logo_pos=getattr(p, "logo_pos", "br") or "br",
        logo_opacity=float(getattr(p, "logo_opacity", 0.85) or 0.85),
        cap_style=getattr(p, "cap_style", "standard") or "standard",
        series=getattr(p, "series", None),
        episode_no=getattr(p, "episode_no", None),
    )


@router.post("", response_model=ProjectOut)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    if payload.mode == "topic":
        if len(payload.topic.strip()) < 2:
            raise HTTPException(422, "主题模式需要至少 2 个字的主题")
    else:
        if len(payload.script_text.strip()) < 10:
            raise HTTPException(422, "文案至少 10 个字（或改用主题模式）")

    # 风格预设展开（显式传值优先于预设）
    if payload.preset_id:
        pr = db.get(Preset, payload.preset_id)
        if not pr:
            raise HTTPException(404, "预设不存在")
        cfg = pr.config or {}
        payload = payload.model_copy(update={
            "theme": cfg.get("theme", payload.theme),
            "brand_accent": cfg.get("brand_accent", "") or payload.brand_accent,
            "voice": cfg.get("voice", payload.voice),
            "bgm": cfg.get("bgm", payload.bgm),
            "aspect": cfg.get("aspect", payload.aspect),
        })

    p = Project(
        id=uuid.uuid4().hex,
        title=(payload.title or payload.topic or payload.script_text[:20]).strip() or "未命名项目",
        mode=payload.mode,
        topic=payload.topic or None,
        outline=None,
        script_text=payload.script_text or "",
        aspect=payload.aspect,
        voice=payload.voice,
        theme=payload.theme,
        brand_accent=(payload.brand_accent or None) if payload.theme == "brand" else None,
        direct_to_video=bool(payload.direct),
        duration_target_s=payload.duration_target_s or 0,
        preset_id=payload.preset_id or None,
        cover_brand=((db.get(Preset, payload.preset_id).config or {}).get("cover_brand")
                     if payload.preset_id and db.get(Preset, payload.preset_id) else None),
        status=ProjectStatus.OUTLINING if payload.mode == "topic" else ProjectStatus.STORYBOARDING,
    )
    db.add(p)
    db.commit()

    from ..jobs.tasks import outline_task
    kind = "outline" if payload.mode == "topic" else "storyboard"
    task = outline_task if payload.mode == "topic" else storyboard_task
    job = Job(id=uuid.uuid4().hex, project_id=p.id, kind=kind)
    db.add(job)
    db.commit()

    q = _redis_queue()
    q.enqueue(task, job.id, job_id=job.id, job_timeout=3600, ttl=7200)
    db.refresh(p)
    return _project_out(db, p)


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)):
    ps = db.scalars(select(Project).order_by(Project.created_at.desc()).limit(100)).all()
    return [_project_out(db, p) for p in ps]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    return _project_out(db, p)


@router.delete("/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    # 取消进行中的任务
    for j in p.jobs:
        if j.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            try:
                from redis import Redis
                from rq.job import Job as RQJob
                s = get_settings()
                RQJob.fetch(j.id, connection=Redis.from_url(s.redis_url)).cancel()
            except Exception:
                pass
    db.delete(p)
    db.commit()
    # 工作目录
    import shutil
    s = get_settings()
    shutil.rmtree(s.work_dir / f"project-{project_id}", ignore_errors=True)
    return {"ok": True}


# ── 大纲阶段（主题模式） ─────────────────────────────────────

@router.patch("/{project_id}/outline")
def update_outline(project_id: str, body: dict, db: Session = Depends(get_db)):
    """用户编辑大纲（宽松结构校验 + 长度钳制）。"""
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if p.status not in (ProjectStatus.OUTLINE_READY, ProjectStatus.FAILED):
        raise HTTPException(409, f"当前状态 {p.status} 不可编辑大纲")
    from ..services.outline import _MAX, TEMPLATE_HINTS, _clamp
    try:
        outline = {
            "title": _clamp(body.get("title") or p.title, _MAX["title"]),
            "summary": _clamp(body.get("summary", ""), 40),
            "hook": {"narration": _clamp((body.get("hook") or {}).get("narration"), _MAX["narration"])},
            "beats": [],
            "outro": {
                "quote": _clamp((body.get("outro") or {}).get("quote"), _MAX["quote"]),
                "cta": _clamp((body.get("outro") or {}).get("cta"), _MAX["cta"]),
                "narration": _clamp((body.get("outro") or {}).get("narration"), _MAX["narration"]),
            },
        }
        for b in (body.get("beats") or [])[:4]:
            hint = b.get("template_hint")
            outline["beats"].append({
                "title": _clamp(b.get("title"), _MAX["beat_title"]),
                "points": [_clamp(x, _MAX["point"]) for x in (b.get("points") or [])][:4],
                "narration": _clamp(b.get("narration"), _MAX["narration"]),
                **({"template_hint": hint} if hint in TEMPLATE_HINTS else {}),
            })
        # 基本完整性
        if len(outline["beats"]) < 1:
            raise HTTPException(422, "至少需要 1 个内容层次")
        if not outline["hook"]["narration"]:
            raise HTTPException(422, "开场旁白不能为空")
        for i, b in enumerate(outline["beats"]):
            if not b["narration"]:
                raise HTTPException(422, f"第 {i + 1} 层的旁白不能为空")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, f"大纲结构错误: {e}") from None
    p.outline = outline
    db.add(p)
    db.commit()
    return {"ok": True, "outline": outline}


@router.post("/{project_id}/outline/confirm", response_model=JobOut)
def confirm_outline(project_id: str, body: OutlineConfirm,
                    db: Session = Depends(get_db)):
    """确认大纲 → 生成分镜；direct=True 跳过分镜预览直接出片。"""
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if p.status != ProjectStatus.OUTLINE_READY:
        raise HTTPException(409, f"当前状态 {p.status}，大纲尚未就绪或已进入下一阶段")
    if not p.outline:
        raise HTTPException(409, "大纲为空，请先扩写或编辑")

    p.direct_to_video = bool(body.direct)
    # 清旧分镜（重新确认的场景）
    for sc in list(p.scenes):
        db.delete(sc)
    p.scenes.clear()
    p.status = ProjectStatus.STORYBOARDING
    db.add(p)
    job = Job(id=uuid.uuid4().hex, project_id=p.id, kind="storyboard")
    db.add(job)
    db.commit()

    q = _redis_queue()
    q.enqueue(storyboard_task, job.id, job_id=job.id, job_timeout=3600, ttl=7200)
    db.refresh(job)
    return job


@router.post("/{project_id}/outline/hook-variants")
def hook_variants(project_id: str, db: Session = Depends(get_db)):
    """LLM 生成 3 个开场钩子变体（反直觉/提问式/场景代入），供用户一键替换。"""
    import json

    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not p.outline:
        raise HTTPException(409, "大纲为空")
    from ..services.llm import chat_with_fallback

    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "hooks",
            "schema": {
                "type": "object",
                "properties": {
                    "hooks": {"type": "array",
                              "items": {"type": "string", "minLength": 10, "maxLength": 60},
                              "minItems": 3, "maxItems": 3},
                },
                "required": ["hooks"], "additionalProperties": False,
            },
            "strict": False,
        },
    }
    system = ("你是短视频编导。给同一条视频写 3 个不同的开场钩子（旁白第一句），"
              "风格分别是：1) 反直觉断言 2) 提问式 3) 场景代入。"
              "口语化中文，每个 15-45 字，直接可朗读，不含序号。只输出 JSON。")
    beats = [b.get("title", "") for b in (p.outline.get("beats") or [])]
    user = (f"标题：{p.outline.get('title', '')}\n"
            f"简介：{p.outline.get('summary', '')}\n"
            f"当前开场：{(p.outline.get('hook') or {}).get('narration', '')}\n"
            f"内容层次：{beats}")
    try:
        content, _ = chat_with_fallback(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            response_format=schema, temperature=0.8, max_tokens=8000)
        hooks = json.loads(content).get("hooks")
        if not isinstance(hooks, list) or len(hooks) != 3:
            raise ValueError("数量不对")
    except Exception as e:
        raise HTTPException(502, f"钩子生成失败: {e}") from None
    return {"hooks": [h.strip() for h in hooks]}


@router.post("/{project_id}/outline/regenerate", response_model=JobOut)
def regenerate_outline(project_id: str, db: Session = Depends(get_db)):
    """重新扩写（保留主题，覆盖大纲）。"""
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not p.topic:
        raise HTTPException(409, "仅主题模式可重新扩写")
    if p.status in (ProjectStatus.STORYBOARDING, ProjectStatus.RENDERING):
        raise HTTPException(409, "任务进行中，请稍后")
    p.status = ProjectStatus.OUTLINING
    db.add(p)
    job = Job(id=uuid.uuid4().hex, project_id=p.id, kind="outline")
    db.add(job)
    db.commit()
    from ..jobs.tasks import outline_task
    q = _redis_queue()
    q.enqueue(outline_task, job.id, job_id=job.id, job_timeout=3600, ttl=7200)
    db.refresh(job)
    return job


# ── 时长选择（看完剧本后） ───────────────────────────────────

@router.post("/{project_id}/pacing")
def choose_pacing(project_id: str, payload: PacingRequest,
                  db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if p.status == ProjectStatus.STORYBOARDING:
        raise HTTPException(409, "分镜尚未生成完成")
    p.pacing_mode = payload.mode
    p.duration_target_s = payload.target_s if payload.mode != "standard" else 0
    p.bgm = payload.bgm
    db.add(p)
    db.commit()
    natural = sum(s.voice_s or 0.0 for s in p.scenes)
    from ..services.pacing import choose_speed, estimate_after_speed
    speed = choose_speed(payload.mode, natural, payload.target_s)
    est = estimate_after_speed(natural, speed)
    return {
        "ok": True,
        "pacing_mode": p.pacing_mode,
        "duration_target_s": p.duration_target_s,
        "voice_speed": speed,
        "estimated_s": round(est, 1),
        "note": "标准=自然语速；紧凑/舒缓 首次渲染会重新合成配音（约多 20-30 秒）",
    }


class ProjectSettings(BaseModel):
    sfx_enabled: bool | None = None
    progress_bar: bool | None = None
    cap_style: str | None = Field(default=None, pattern="^(standard|large|high)$")
    series: str | None = Field(default=None, max_length=60)
    episode_no: int | None = Field(default=None, ge=1, le=999)
    logo_pos: str | None = Field(default=None, pattern="^(tr|br|bl)$")
    logo_opacity: float | None = Field(default=None, ge=0.1, le=1.0)
    logo_clear: bool | None = None   # true = 删除水印


@router.patch("/{project_id}/settings")
def patch_settings(project_id: str, body: ProjectSettings,
                   db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if body.sfx_enabled is not None:
        p.sfx_enabled = body.sfx_enabled
    if body.progress_bar is not None:
        p.progress_bar = body.progress_bar
    if body.logo_pos is not None:
        p.logo_pos = body.logo_pos
    if body.cap_style is not None:
        p.cap_style = body.cap_style
    if body.series is not None:
        p.series = body.series.strip() or None
    if body.episode_no is not None:
        p.episode_no = body.episode_no
    if body.logo_opacity is not None:
        p.logo_opacity = body.logo_opacity
    if body.logo_clear:
        p.logo_path = None
    db.commit()
    return {"ok": True, "sfx_enabled": bool(p.sfx_enabled),
            "progress_bar": bool(p.progress_bar),
            "logo_set": bool(p.logo_path), "logo_pos": p.logo_pos,
            "logo_opacity": p.logo_opacity, "cap_style": p.cap_style,
            "series": p.series, "episode_no": p.episode_no}


@router.post("/{project_id}/logo")
def upload_logo(project_id: str, file: UploadFile,
                db: Session = Depends(get_db)):
    """品牌水印：png/jpg/webp，存 work_dir/logos/<项目id>.<ext>。"""
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    allowed = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    if file.content_type not in allowed:
        raise HTTPException(422, "仅支持 png/jpg/webp")
    data = file.file.read()
    if len(data) > 4 * 1024 * 1024:
        raise HTTPException(422, "logo 超过 4MB")
    if len(data) < 100:
        raise HTTPException(422, "空文件")
    from ..config import get_settings
    logos = Path(get_settings().work_dir) / "logos"
    logos.mkdir(parents=True, exist_ok=True)
    name = f"{project_id}{allowed[file.content_type]}"
    (logos / name).write_bytes(data)
    # 同项目旧扩展名清掉
    for old_f in logos.glob(f"{project_id}.*"):
        if old_f.name != name:
            old_f.unlink(missing_ok=True)
    p.logo_path = name
    db.commit()
    return {"ok": True, "logo_set": True, "logo_path": name}


@router.get("/{project_id}/captions.srt")
def export_srt(project_id: str, db: Session = Depends(get_db)):
    """导出字幕 SRT（分镜/渲染完成后可用）。"""
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    s = get_settings()
    srt = s.work_dir / f"project-{project_id}" / "current" / "captions.srt"
    if not srt.exists():
        raise HTTPException(404, "字幕尚未生成（先完成分镜）")
    from fastapi.responses import Response

    return Response(
        content=srt.read_text(encoding="utf-8"),
        media_type="application/x-subrip; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=\"{project_id}.srt\""},
    )


# ── 场景编辑 ─────────────────────────────────────────────────

@router.patch("/{project_id}/scenes/{scene_id}")
def update_scene(project_id: str, scene_id: str, payload: SceneUpdate,
                 db: Session = Depends(get_db)):
    sc = db.get(Scene, scene_id)
    if not sc or sc.project_id != project_id:
        raise HTTPException(404, "场景不存在")
    if payload.voice_text is not None:
        sc.voice_text = payload.voice_text.strip()
    if payload.fields is not None or payload.template_id is not None:
        from ..services.template_engine import get_registry
        target_tpl = payload.template_id or sc.template_id
        try:
            get_registry().validate(target_tpl, payload.fields or sc.fields)
        except TemplateValidationError as e:
            raise HTTPException(422, {"template": e.template_id, "issues": e.issues}) from None
        if payload.template_id and payload.template_id != sc.template_id:
            sc.template_id = payload.template_id
        if payload.fields is not None:
            sc.fields = payload.fields
    db.add(sc)
    db.commit()
    db.refresh(sc)
    return {"ok": True, "scene": {"id": sc.id, "idx": sc.idx,
                                  "template_id": sc.template_id,
                                  "voice_text": sc.voice_text, "fields": sc.fields}}


@router.post("/{project_id}/scenes/reorder")
def reorder_scenes(project_id: str, body: dict, db: Session = Depends(get_db)):
    ids = body.get("scene_ids") or []
    scenes = {s.id: s for s in db.scalars(
        select(Scene).where(Scene.project_id == project_id)).all()}
    if set(ids) != set(scenes.keys()) or len(ids) != len(scenes):
        raise HTTPException(422, "scene_ids 必须恰好包含全部场景一次")
    for i, sid in enumerate(ids):
        scenes[sid].idx = i
        db.add(scenes[sid])
    db.commit()
    return {"ok": True}
