"""任务路由：发起渲染、状态轮询、日志分页。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from rq import Queue
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..jobs.tasks import QUEUE_NAME, render_task, storyboard_task
from ..models import Job, JobLog, JobStatus, Project, ProjectStatus, Scene
from ..schemas import JobOut, LogLine, RenderRequest

router = APIRouter(prefix="/api/projects/{project_id}/jobs", tags=["jobs"])


def _redis_queue() -> Queue:
    from redis import Redis
    s = get_settings()
    return Queue(QUEUE_NAME, connection=Redis.from_url(s.redis_url))


@router.post("/render", response_model=JobOut)
def start_render(project_id: str, body: RenderRequest | None = None,
                 db: Session = Depends(get_db)):
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    scenes = db.scalars(select(Scene).where(Scene.project_id == p.id)
                        .order_by(Scene.idx)).all()
    if not scenes:
        raise HTTPException(409, "项目还没有分镜，请先提交文案生成分镜")

    # 同项目进行中的渲染任务 → 拒绝
    running = db.scalars(
        select(Job).where(Job.project_id == p.id, Job.kind == "render",
                          Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
    ).all()
    if running:
        return running[0]

    force = bool(body.force) if body else False
    if force:
        import shutil
        s = get_settings()
        shutil.rmtree(s.work_dir / f"project-{p.id}" / "markers", ignore_errors=True)

    job = Job(id=uuid.uuid4().hex, project_id=p.id, kind="render",
              payload={"extra_aspects": (body.extra_aspects if body else []) or [],
                       "ab_variants": ((body.ab_variants if body else []) or [])[:2]})
    db.add(job)
    p.status = ProjectStatus.RENDERING
    p.error = None
    db.add(p)
    db.commit()

    q = _redis_queue()
    q.enqueue(render_task, job.id, job_id=job.id, job_timeout=1800, ttl=7200)
    db.refresh(job)
    return job


@router.post("/storyboard", response_model=JobOut)
def rerun_storyboard(project_id: str, db: Session = Depends(get_db)):
    """重新生成分镜（script 模式失败重试 / 换文案后重跑）。"""
    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if p.status in (ProjectStatus.STORYBOARDING, ProjectStatus.RENDERING):
        raise HTTPException(409, "任务进行中")
    for sc in list(p.scenes):
        db.delete(sc)
    p.scenes.clear()
    p.status = ProjectStatus.STORYBOARDING
    p.error = None
    db.add(p)
    job = Job(id=uuid.uuid4().hex, project_id=p.id, kind="storyboard")
    db.add(job)
    db.commit()
    q = _redis_queue()
    q.enqueue(storyboard_task, job.id, job_id=job.id, job_timeout=3600, ttl=7200)
    db.refresh(job)
    return job


@router.get("", response_model=list[JobOut])
def list_jobs(project_id: str, kind: str | None = None,
              db: Session = Depends(get_db)):
    q = select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc())
    if kind:
        q = q.where(Job.kind == kind)
    return list(db.scalars(q.limit(50)).all())


@router.get("/{job_id}", response_model=JobOut)
def get_job(project_id: str, job_id: str, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if not j or j.project_id != project_id:
        raise HTTPException(404, "任务不存在")
    return j


@router.get("/{job_id}/logs", response_model=list[LogLine])
def job_logs(project_id: str, job_id: str,
             after_id: int = Query(0, ge=0),
             limit: int = Query(200, ge=1, le=1000),
             db: Session = Depends(get_db)):
    q = (select(JobLog)
         .where(JobLog.job_id == job_id, JobLog.id > after_id)
         .order_by(JobLog.id)
         .limit(limit))
    return list(db.scalars(q).all())
