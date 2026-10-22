"""RQ 任务入口：队列消费 → pipeline。"""

from __future__ import annotations

import logging

from rq import get_current_job

from ..services import pipeline

log = logging.getLogger(__name__)


def outline_task(job_id: str) -> None:
    try:
        pipeline.run_outline_job(job_id)
    except Exception:
        log.exception("outline_task %s", job_id)


def _detach_inherited_pool() -> None:
    """RQ 的马仔子进程会原样继承父进程的连接池（连同活连接与服务端
    预编译语句）。开工先"弃用不关闭"——子进程开新连接，父进程的连接
    完好无损。防 DuplicatePreparedStatement / 协议串台。"""
    try:
        from ..db import engine

        engine.dispose(close=False)
    except Exception:  # noqa: BLE001
        pass


def storyboard_task(job_id: str) -> None:
    j = get_current_job()
    _detach_inherited_pool()
    try:
        pipeline.run_storyboard_job(job_id)
    except Exception:
        # 状态已由 pipeline 落库；这里吞掉避免 RQ 反复重试同一失败任务
        log.exception("storyboard_task %s", job_id)
        _mark_orphan_failed(job_id)


def _mark_orphan_failed(job_id: str) -> None:
    """pipeline 在落库前崩溃（如 DB 列缺失）→ 任务永远卡 queued。这里兜底标失败。"""
    try:
        from ..db import SessionLocal
        from ..models import Job, JobStatus, Project, ProjectStatus

        db = SessionLocal()
        try:
            j = db.get(Job, job_id)
            if j and j.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                j.status = JobStatus.FAILED
                j.error = "任务异常终止（详见 worker 日志）"
                db.add(j)
                p = db.get(Project, j.project_id)
                if p and p.status in (ProjectStatus.STORYBOARDING, ProjectStatus.RENDERING):
                    p.status = ProjectStatus.FAILED
                    db.add(p)
                db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("mark_orphan_failed %s", job_id)


def render_task(job_id: str) -> None:
    _detach_inherited_pool()
    try:
        pipeline.run_render_job(job_id)
    except Exception:
        log.exception("render_task %s", job_id)
        _mark_orphan_failed(job_id)


QUEUE_NAME = "pipeline"
