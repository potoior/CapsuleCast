"""渲染流水线编排：分镜预览任务 / 渲染任务的阶段机。

阶段与产物全部落盘（work_dir/<project>/…），阶段完成写 marker（含输入哈希）。
任务失败重试时：输入未变的阶段直接跳过（断点续渲）；hyperframes render
本身启用分段捕获 + --resume，同一组合的已完成分段也会被复用。

日志：每条写 job_logs 表（level/stage/message），前端轮询分页拉取。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from ..config import get_settings
from ..db import SessionLocal
from ..models import Artifact, Job, JobLog, JobStatus, Project, ProjectStatus, Scene
from . import hf, storage
from .compose import ScenePlan, compose_project
from .template_engine import TemplateRegistry
from .tts import synthesize_cached

log = logging.getLogger(__name__)

# 预估语速（字/秒，zh · speed 1.0 实测约 3.4）——仅用于 TTS 前的排期预估
_ZH_CHARS_PER_SEC = 3.4

STORYBOARD_STAGES = ["generate", "tts", "compose", "snapshot"]
RENDER_STAGES = ["tts", "compose", "check", "render", "publish"]


class JobLogger:
    """双写日志：job_logs 表 + 内存缓冲。"""

    def __init__(self, db, job_id: str):
        self.db = db
        self.job_id = job_id
        self._buf: list[JobLog] = []

    def log(self, message: str, *, level: str = "info", stage: str | None = None) -> None:
        entry = JobLog(job_id=self.job_id, ts=datetime.now(timezone.utc),
                       level=level, stage=stage, message=str(message)[:4000])
        self._buf.append(entry)
        # 批量刷写，避免每行一个事务
        if len(self._buf) >= 20:
            self.flush()

    def flush(self) -> None:
        if self._buf:
            self.db.add_all(self._buf)
            self.db.commit()
            self._buf.clear()


def _project_dir(project_id: str) -> Path:
    s = get_settings()
    d = s.work_dir / f"project-{project_id}" / "current"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker_path(project_id: str, stage: str) -> Path:
    s = get_settings()
    d = s.work_dir / f"project-{project_id}" / "markers"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stage}.done.json"


def _scenes_hash(scenes: list[Scene]) -> str:
    payload = [
        {"t": sc.template_id, "v": sc.voice_text, "f": sc.fields, "i": sc.idx}
        for sc in scenes
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()[:20]


def _set_job(db, job: Job, *, stage: str | None = None, progress: int | None = None,
             status: JobStatus | None = None, error: str | None = None) -> None:
    if stage is not None:
        job.stage = stage
    if progress is not None:
        job.progress = max(job.progress or 0, min(100, progress))
    if status is not None:
        job.status = status
    if error is not None:
        job.error = error[:4000]
    job.updated_at = datetime.now(timezone.utc)
    db.add(job)
    db.commit()


def _tts_all(
    db, job: Job, project: Project, scenes: list[Scene], jlog: JobLogger,
    progress_base: int, progress_span: int, speed: float = 1.0,
    voice_override: str | None = None,
) -> list[tuple[Scene, Path, float]]:
    """合成全部场景语音（缓存命中跳过）。返回 [(scene, wav_path, duration)]。"""
    s = get_settings()
    cache_dir = s.work_dir / "tts-cache"
    results: list[tuple[Scene, Path, float]] = []
    total = len(scenes)
    for i, sc in enumerate(scenes):
        t0 = time.time()
        r = synthesize_cached(
            sc.voice_text,
            voice=voice_override or project.voice,
            speed=speed,
            cache_dir=cache_dir,
            line_id=f"{sc.idx + 1:02d}",
        )
        jlog.log(f"TTS 场景{sc.idx + 1}: {r.duration_s:.2f}s"
                 f"（{'缓存' if r.cache_hit else f'{time.time() - t0:.1f}s 合成'}）",
                 stage="tts")
        results.append((sc, r.wav_path, r.duration_s))
        _set_job(db, job, progress=progress_base + int(progress_span * (i + 1) / total))
    return results


def _compose(
    db, job: Job, project: Project, scenes: list[Scene],
    tts_results: list[tuple[Scene, Path, float]], jlog: JobLogger,
    pads: list[float] | None = None,
) -> Path:
    """组装 hyperframes 项目目录。pads=None 时为标准排布（无停留）。"""
    reg = TemplateRegistry()
    plans: list[ScenePlan] = []
    for i, (sc, wav, dur) in enumerate(tts_results):
        plans.append(ScenePlan(
            idx=i,
            scene_id=f"{i + 1:02d}-{sc.template_id}",
            template_id=sc.template_id,
            fields=sc.fields,
            voice_text=sc.voice_text,
            voice_path=wav,
            voice_duration=dur,
        ))
    out = _project_dir(project.id)
    # 清掉上次产物，避免 stale 文件；renders/ 只删旧 mp4（保留 .hf-segments 分段缓存供 --resume 复用）
    import shutil
    for p in out.iterdir():
        if p.name == "renders":
            (p / "video.mp4").unlink(missing_ok=True)
            (p / "poster.png").unlink(missing_ok=True)
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)

    from ..config import get_settings as _gs
    _logo = None
    if getattr(project, "logo_path", None):
        _lp = Path(_gs().work_dir) / "logos" / project.logo_path
        if _lp.exists():
            _logo = _lp
    result = compose_project(
        project_id=project.id,
        title=project.title,
        aspect=project.aspect.value if hasattr(project.aspect, "value") else str(project.aspect),
        scenes=plans,
        out_dir=out,
        registry=reg,
        pads=pads,
        theme=project.theme or "warm",
        brand_accent=project.brand_accent,
        progress=bool(getattr(project, "progress_bar", True)),
        logo_src=_logo,
        logo_pos=getattr(project, "logo_pos", "br") or "br",
        logo_opacity=float(getattr(project, "logo_opacity", 0.85) or 0.85),
        cap_style=getattr(project, "cap_style", "standard") or "standard",
        series=getattr(project, "series", None),
        episode_no=getattr(project, "episode_no", None),
    )
    # 回写场景时间到 DB
    for i, (sc, _, _) in enumerate(tts_results):
        sc.duration_s = round(result.scene_durations[i], 3)
        sc.start_s = round(result.scene_starts[i], 3)
        db.add(sc)
    db.commit()

    # 数据保真提示：旁白含具体统计数字时提醒人工核对（LLM 有编数风险）
    import re as _re_stats
    _stat_hits = []
    for sc, _, _ in tts_results:
        for m in _re_stats.finditer(
                r"(\d+(?:\.\d+)?)\s*(?:%|％|万|亿|倍|元|人|次|分钟|小时)", sc.voice_text or ""):
            _stat_hits.append(m.group(0))
    if _stat_hits:
        jlog.log(f"⚠ 旁白含 {len(_stat_hits)} 处具体数字（{'、'.join(_stat_hits[:6])}…），"
                 "发布前请人工核对数据来源", level="warn", stage="compose")

    drifted = [i for i, ln in enumerate(result.leads or []) if abs(ln - 0.04) > 0.06]
    jlog.log(
        f"组装完成: {len(plans)} 镜 · 总时长 {result.total_duration:.1f}s · "
        f"字幕组 {result.groups_count} · 目录 {out}",
        stage="compose",
    )
    if drifted:
        jlog.log(f"字幕校准: 第 {[i + 1 for i in drifted]} 镜前导静音实测 "
                 f"{[round(result.leads[i], 2) for i in drifted]}s（默认假设 0.04s），已按实测平移",
                 stage="compose")
    return out


def _hf_progress_bridge(db, job: Job, jlog: JobLogger,
                        base: int, span: int) -> Callable[[str], None]:
    """解析 @hf-progress 行 → 更新 job.progress。"""
    rx = re.compile(r'@hf-progress\s+(\{.*\})')
    state = {"last_flush": 0.0}

    def sink(line: str) -> None:
        stripped = str(line)
        # 每行日志同时进 jlog（节流：进度行除外，避免刷屏）
        m = rx.search(stripped)
        if m:
            try:
                obj = json.loads(m.group(1))
                pct = int(obj.get("pct", 0))
                _set_job(db, job, progress=base + int(span * pct / 100))
            except Exception:
                pass
            return
        # 关键行才记录，避免几千行 trace 淹没日志
        if any(k in stripped for k in ("ERROR", "WARN", "Browser", "Rendering",
                                       "render", "Render", "✓", "✗", "failed")):
            jlog.log(stripped[:2000], stage="render")
            now = time.time()
            if now - state["last_flush"] > 2.0:
                jlog.flush()
                state["last_flush"] = now

    return sink


# ── 任务入口 ─────────────────────────────────────────────────

def run_outline_job(job_id: str) -> None:
    """大纲任务：主题 → 扩写 → 结构化大纲（轻任务，无 TTS/Chrome）。"""
    db = SessionLocal()
    jlog = JobLogger(db, job_id)
    job = db.get(Job, job_id)
    project = db.get(Project, job.project_id)
    try:
        _set_job(db, job, status=JobStatus.RUNNING, stage="outline", progress=20)
        from .outline import generate_outline
        jlog.log(f"开始扩写主题：{project.topic}", stage="outline")
        engine, outline = generate_outline(
            project.topic or project.title,
            duration_target_s=project.duration_target_s or 75,
        )
        project.outline = outline
        project.title = outline.get("title") or project.title
        project.engine = engine
        project.status = ProjectStatus.OUTLINE_READY
        project.error = None
        db.add(project)
        _set_job(db, job, status=JobStatus.SUCCEEDED, stage="outline", progress=100)
        jlog.log(
            f"大纲就绪（引擎 {engine}）：{len(outline.get('beats') or [])} 个层次 + "
            f"钩子 + 收尾。请编辑确认，或选择直接生成视频。",
            stage="outline",
        )
        jlog.flush()

        # ── 全自动直出：大纲就绪即链式发起分镜（分镜完成再自动渲染）──
        if project.direct_to_video:
            from redis import Redis
            from rq import Queue

            from ..jobs.tasks import QUEUE_NAME, storyboard_task
            s = get_settings()
            q = Queue(QUEUE_NAME, connection=Redis.from_url(s.redis_url))
            rjob = Job(id=uuid.uuid4().hex, project_id=project.id, kind="storyboard")
            db.add(rjob)
            project.status = ProjectStatus.STORYBOARDING
            db.add(project)
            db.commit()
            q.enqueue(storyboard_task, rjob.id, job_id=rjob.id, job_timeout=3600, ttl=7200)
            jlog.log("直出模式：大纲通过，自动生成分镜", stage="outline")
            jlog.flush()
    except Exception as e:
        db.rollback()
        log.exception("outline job %s 失败", job_id)
        project = db.get(Project, job.project_id)
        project.status = ProjectStatus.FAILED
        project.error = str(e)[:2000]
        db.add(project)
        _set_job(db, job, status=JobStatus.FAILED, error=str(e))
        jlog.log(f"任务失败: {e}", level="error")
        jlog.flush()
    finally:
        db.close()


def run_storyboard_job(job_id: str) -> None:
    """分镜任务：大纲/文案 → 场景 → TTS → 组装 → 快照 → 上传预览图。
    若 project.direct_to_video，完成后链式发起渲染。"""
    db = SessionLocal()
    jlog = JobLogger(db, job_id)
    job = db.get(Job, job_id)
    project = db.get(Project, job.project_id)
    try:
        _set_job(db, job, status=JobStatus.RUNNING, stage="generate", progress=5)

        reg = TemplateRegistry()

        # 场景来源：大纲（结构化）优先，其次规则引擎切文案
        if project.outline:
            from .outline import scenes_from_outline
            raw_scenes = scenes_from_outline(project.outline, reg)
            used = "outline"
            # 逐镜校验；失败回退 bullet-list 抽取
            from .storyboard import _EXTRACTORS
            fixed = []
            for sc in raw_scenes:
                try:
                    reg.validate(sc["template_id"], sc["fields"])
                    fixed.append(sc)
                except Exception:
                    sc2 = dict(sc)
                    sc2["template_id"] = "bullet-list"
                    sc2["fields"] = _EXTRACTORS["bullet-list"](sc["voice_text"])
                    fixed.append(sc2)
            raw_scenes = fixed
            from collections import namedtuple
            SbScene = namedtuple("SbScene", ["template_id", "voice_text", "fields"])
            scenes_sb = [SbScene(s["template_id"], s["voice_text"], s["fields"])
                         for s in raw_scenes]
        else:
            from .storyboard import generate_storyboard
            used, scenes_sb = generate_storyboard(
                project.script_text,
                duration_target_s=project.duration_target_s or 75,
                engine=project.engine,
            )
        project.engine = used
        db.add(project)

        # 覆盖 scenes（清内存集合，避免级联 flush 踩到已删实例）
        for sc in list(project.scenes):
            db.delete(sc)
        project.scenes.clear()
        db.commit()
        for i, sc in enumerate(scenes_sb):
            db.add(Scene(project_id=project.id, idx=i, template_id=sc.template_id,
                         voice_text=sc.voice_text, fields=sc.fields))
        db.commit()
        jlog.log(f"分镜生成完成（来源 {used}，{len(scenes_sb)} 镜）", stage="generate")
        _set_job(db, job, stage="tts", progress=10)

        scenes = list(db.scalars(select(Scene).where(Scene.project_id == project.id)
                                 .order_by(Scene.idx)).all())
        tts_results = _tts_all(db, job, project, scenes, jlog, 10, 30)
        # 记录自然语速(1.0)时长 —— 之后选时长（pacing）的基准
        for sc, _, dur in tts_results:
            sc.voice_s = round(dur, 3)
            db.add(sc)
        db.commit()
        jlog.flush()

        _set_job(db, job, stage="compose", progress=45)
        out_dir = _compose(db, job, project, scenes, tts_results, jlog)
        jlog.flush()

        # 快照：每镜中段取一帧
        _set_job(db, job, stage="snapshot", progress=70)
        ats: list[float] = []
        for i, (sc, _, _) in enumerate(tts_results):
            start = tts_results[i][0].start_s or 0
            dur = sc.duration_s or 8
            ats.append(min(start + dur * 0.6, start + dur - 0.4))
        snap_dir = get_settings().work_dir / f"project-{project.id}" / "preview"
        import shutil as _sh
        _sh.rmtree(snap_dir, ignore_errors=True)
        pngs = hf.snapshot_frames(out_dir, ats, snap_dir)
        for i, png in enumerate(pngs):
            if i < len(scenes) and png.exists():
                key = f"projects/{project.id}/scenes/{scenes[i].idx}.png"
                storage.upload(png, key, "image/png")
                scenes[i].snapshot_url = storage.presigned_get(key)
                db.add(scenes[i])
        # 每镜语音也上传——预览播放器试听用
        for _i, (sc, wav_p, _d) in enumerate(tts_results):
            try:
                wav_p = Path(wav_p)
                if wav_p.exists():
                    key = f"projects/{project.id}/scenes/{sc.idx}.wav"
                    storage.upload(wav_p, key, "audio/wav")
                    sc.voice_url = storage.presigned_get(key)
                    db.add(sc)
            except Exception:  # noqa: BLE001
                pass
        db.commit()
        jlog.log(f"预览快照完成（{len(pngs)} 帧 + {len(tts_results)} 段语音）", stage="snapshot")

        project.status = ProjectStatus.STORYBOARD_READY
        project.error = None
        db.add(project)
        _set_job(db, job, status=JobStatus.SUCCEEDED, stage="snapshot", progress=100)
        jlog.log("分镜就绪：请确认或编辑场景后发起渲染", stage="snapshot")
        jlog.flush()

        # ── 直出模式：跳过用户确认，链式发起渲染 ────────────────
        if project.direct_to_video:
            from redis import Redis
            from rq import Queue

            from ..jobs.tasks import QUEUE_NAME, render_task
            s = get_settings()
            q = Queue(QUEUE_NAME, connection=Redis.from_url(s.redis_url))
            rjob = Job(id=uuid.uuid4().hex, project_id=project.id, kind="render")
            db.add(rjob)
            project.status = ProjectStatus.RENDERING
            db.add(project)
            db.commit()
            q.enqueue(render_task, rjob.id, job_id=rjob.id, job_timeout=1800, ttl=7200)
            jlog.log("直出模式：已自动发起渲染", stage="snapshot")
            jlog.flush()
    except Exception as e:
        db.rollback()
        log.exception("storyboard job %s 失败", job_id)
        project = db.get(Project, job.project_id)
        project.status = ProjectStatus.FAILED
        project.error = str(e)[:2000]
        db.add(project)
        _set_job(db, job, status=JobStatus.FAILED, error=str(e))
        jlog.log(f"任务失败: {e}", level="error")
        jlog.flush()
    finally:
        db.close()


def run_render_job(job_id: str) -> None:
    """渲染任务：TTS → 组装 → check → render(GPU→软件) → 上传发布。"""
    db = SessionLocal()
    jlog = JobLogger(db, job_id)
    try:
        job = db.get(Job, job_id)
        project = db.get(Project, job.project_id)
        _set_job(db, job, status=JobStatus.RUNNING, stage="tts", progress=5)
        scenes = list(db.scalars(select(Scene).where(Scene.project_id == project.id)
                                 .order_by(Scene.idx)).all())
        if not scenes:
            raise ValueError("项目没有场景，请先生成分镜")

        # ── 时长计划（用户已看剧本选过时长）─────────────────────
        from .pacing import choose_speed, plan_pads
        from .template_engine import TemplateRegistry
        reg = TemplateRegistry()
        natural = sum(sc.voice_s or 0.0 for sc in scenes)
        # 直出/未选时长：目标显著大于自然时长 → 自动升为「舒缓」让停留拉伸生效
        mode = project.pacing_mode
        if mode == "standard" and project.duration_target_s and \
                project.duration_target_s > natural * 1.1:
            mode = "relaxed"
            jlog.log(f"目标 {project.duration_target_s}s > 自然 {natural:.0f}s，"
                     f"自动切为「舒缓」模式拉伸停留", stage="tts")
        speed = choose_speed(mode, natural, project.duration_target_s)
        jlog.log(
            f"渲染开始：{len(scenes)} 镜 · {project.aspect} · "
            f"时长模式 {mode}（目标 {project.duration_target_s or '自然'}s · "
            f"自然 {natural:.0f}s · 语速 {speed:.2f}） · 声音 {project.voice}",
            stage="tts",
        )

        scenes_h = _scenes_hash(scenes)
        marker = _marker_path(project.id, "tts")
        if marker.exists() and json.loads(marker.read_text()).get("hash") == scenes_h:
            jlog.log("场景未变化，复用已有语音与组合", stage="tts")
            tts_results = _tts_all(db, job, project, scenes, jlog, 5, 5,
                                   speed=speed)  # 全部缓存命中
        else:
            tts_results = _tts_all(db, job, project, scenes, jlog, 5, 10,
                                   speed=speed)
        jlog.flush()

        # 停留分配（用实际 TTS 时长 + 模板上限）
        plan = plan_pads(
            voice_durations=[d for _, _, d in tts_results],
            template_maxes=[reg.get(sc.template_id).max_duration_s for sc in scenes],
            target_s=project.duration_target_s,
            mode=mode,
        )
        if plan.pads and any(p > 0 for p in plan.pads):
            jlog.log(f"停留计划: {[round(p, 1) for p in plan.pads]}s（{plan.note}）",
                     stage="compose")

        _set_job(db, job, stage="compose", progress=20)
        out_dir = _compose(db, job, project, scenes, tts_results, jlog,
                           pads=plan.pads)
        marker.write_text(json.dumps({"hash": scenes_h,
                                      "at": datetime.now(timezone.utc).isoformat()}))
        jlog.flush()

        # ── check（布局/对比度/空白帧/字幕带质检）───────────────
        _set_job(db, job, stage="check", progress=28)
        check_marker = _marker_path(project.id, "check")
        check = None
        if check_marker.exists() and json.loads(check_marker.read_text()).get("hash") == scenes_h:
            try:
                check = json.loads(check_marker.read_text()).get("report")
                jlog.log("场景未变化，复用上次 check 结论", stage="check")
            except Exception:
                check = None
        if check is None:
            jlog.log("运行 hyperframes check（lint + 运行时 + 布局 + 对比度）…", stage="check")
            check = hf.run_check(out_dir, log_sink=lambda ln: jlog.log(str(ln)[:600], stage="check"))
            # 只有通过的结论才值得记忆；失败必须重跑
            _ok = check.get("ok", check.get("status") == "pass")
            _errs = check.get("errors") or [i for i in (check.get("issues") or [])
                                            if str(i.get("severity", "")).lower() == "error"]
            if _ok and not _errs:
                try:
                    check_marker.write_text(json.dumps(
                        {"hash": scenes_h, "report": check,
                         "at": datetime.now(timezone.utc).isoformat()}))
                except Exception:
                    pass
            else:
                check_marker.unlink(missing_ok=True)
        jlog.flush()
        ok = check.get("ok", check.get("status") == "pass")
        errors = check.get("errors") or []
        warnings = check.get("warnings") or []
        issues = check.get("issues") or []
        err_list = [i for i in issues if str(i.get("severity", "")).lower() == "error"]
        if not ok or errors or err_list:
            detail = json.dumps(check, ensure_ascii=False)[:3000]
            jlog.log(f"check 未通过: {detail}", level="error", stage="check")
            raise HFCheckFailed(f"hyperframes check 未通过（{len(errors or err_list)} 个错误）",
                                report=check)
        jlog.log(f"check 通过（警告 {len(warnings)}）", stage="check")
        _set_job(db, job, progress=38)
        jlog.flush()

        # ── render：分段缓存优先 → 回落全量 ────────────────────
        _set_job(db, job, stage="render", progress=40)
        from .compose import ScenePlan as _SP2
        seg_plans = [
            _SP2(idx=i, scene_id=f"{i + 1:02d}-{sc.template_id}",
                 template_id=sc.template_id, fields=sc.fields,
                 voice_text=sc.voice_text, voice_path=wav, voice_duration=dur)
            for i, (sc, wav, dur) in enumerate(tts_results)
        ]
        video_path = None
        seg_stats = None
        try:
            from . import segrender
            seg_root = out_dir / "segments"
            assembled = out_dir / "renders" / "video-seg.mp4"
            from ..config import get_settings as _gs
            _rlogo = None
            if getattr(project, "logo_path", None):
                _rlp = Path(_gs().work_dir) / "logos" / project.logo_path
                if _rlp.exists():
                    _rlogo = _rlp
            _gstarts = [sc.start_s or 0.0 for sc in scenes]
            _gtotal = (sum(d for _, _, d in tts_results)
                       + sum(p for p in (plan.pads or [])))
            video_path, seg_stats = segrender.render_segmented(
                project_id=project.id, plans=seg_plans, pads=plan.pads,
                aspect=(project.aspect.value if hasattr(project.aspect, "value") else str(project.aspect)),
                theme=project.theme or "warm", brand_accent=project.brand_accent,
                scene_groups_by_idx=[],  # 字幕组在 segrender 内部生成（与全量同源）
                work_root=seg_root, out_path=assembled, jlog=jlog,
                progress=bool(getattr(project, "progress_bar", True)),
                progress_total=_gtotal,
                scene_global_starts=_gstarts,
                logo_src=_rlogo,
                logo_pos=getattr(project, "logo_pos", "br") or "br",
                logo_opacity=float(getattr(project, "logo_opacity", 0.85) or 0.85),
                series=getattr(project, "series", None),
                episode_no=getattr(project, "episode_no", None),
            )
            jlog.log(f"渲染完成（分段路径）: 新渲 {seg_stats.rendered} · "
                     f"缓存 {seg_stats.cached} · 拼接 {seg_stats.assemble_s:.0f}s · "
                     f"GPU捕获={'是' if seg_stats.used_gpu else '否'}", stage="render")
            render_used_gpu = seg_stats.used_gpu
            render_attempts = 1
        except Exception as e:
            log.warning("分段渲染失败，回落全量: %s", e)
            jlog.log(f"分段渲染失败，回落全量渲染: {str(e)[:300]}", level="warn", stage="render")
            video_path = None
        if video_path is None:
            sink = _hf_progress_bridge(db, job, jlog, 40, 45)
            outcome = hf.render_video(out_dir, "renders/video.mp4", log_sink=sink)
            video_path = outcome.output
            render_used_gpu = outcome.used_gpu
            render_attempts = outcome.attempts
            jlog.log(f"渲染完成（全量路径）: GPU捕获={'是' if render_used_gpu else '否'} · "
                     f"尝试 {render_attempts} 次", stage="render")
        jlog.flush()

        # ── BGM 床混音（确定性闪避：包络来自真实旁白时间线，视频流直拷）──
        track = (project.bgm or "auto").strip()
        if track == "auto":
            from .bgm import pick_for
            track = pick_for(project.id)
        # ── SFX 音效轨（转场 whoosh / 要点 pop / 数字 ding / 章节 chime）──
        _sfx_track = None
        if bool(getattr(project, "sfx_enabled", True)) and tts_results:
            try:
                from . import sfx as sfx_svc
                ev = [("chime", 0.12)]
                _scs = [t[0] for t in tts_results]
                for _i, _sc in enumerate(_scs):
                    _st = _sc.start_s or 0.0
                    _d = _sc.duration_s or 5.0
                    if _i + 1 < len(_scs):
                        _d = max(0.5, (_scs[_i + 1].start_s or _st) - _st)
                    if _i > 0:
                        ev.append(("whoosh", max(0.0, _st - 0.5)))
                    _tid = _sc.template_id or ""
                    if _tid == "big-stat":
                        ev.append(("ding", _st + 0.55 * _d))
                    elif _tid == "chapter-card":
                        ev.append(("chime", _st + 0.15))
                    elif _tid.startswith(("diagram-", "chart-")) or _tid in (
                            "bullet-list", "free-blocks", "process-steps",
                            "concept-compare", "term-explainer", "term-demo",
                            "myth-buster", "qa-card", "checklist",
                            "formula-card", "rank-list", "dialogue-quotes",
                            "news-card", "price-compare", "bio-card"):
                        ev.append(("pop", _st + 0.30 * _d))
                video_dur0 = float(hf.ffprobe(video_path).get("format", {}).get("duration") or 0) \
                    or sum(d for _, _, d in tts_results)
                _sfx_track = sfx_svc.build_sfx_track(
                    ev, video_dur0, out_dir / "renders" / "sfx-track.wav")
                if _sfx_track is not None:
                    jlog.log(f"音效层: {len(ev)} 个事件（转场 whoosh · 要点 pop · 数据 ding）",
                             stage="publish")
            except Exception as e:  # noqa: BLE001
                jlog.log(f"音效层跳过: {e}", level="warn", stage="publish")
                _sfx_track = None

        if track != "none":
            from . import bgm as bgm_svc
            probe = hf.ffprobe(video_path)
            video_dur = float(probe.get("format", {}).get("duration") or 0) or \
                sum(d for _, _, d in tts_results)
            narration = [
                (sc.start_s or 0.0, (sc.start_s or 0.0) + dur)
                for (sc, _, dur) in tts_results
            ]
            bed = out_dir / "renders" / "bgm-bed.wav"
            bgm_svc.make_ducked_bed(track, narration, video_dur, bed)
            mixed = out_dir / "renders" / "video-bgm.mp4"
            bgm_svc.amix_video(video_path, bed, mixed, sfx_wav=_sfx_track)
            video_path = mixed
            jlog.log(f"BGM 混音: {bgm_svc.TRACKS.get(track, track)}"
                     f"（语音下自动压低 ~10dB · 空档 -27dB）+ 响度归一 -14 LUFS", stage="publish")
            jlog.flush()
        else:
            norm = out_dir / "renders" / "video-loudnorm.mp4"
            if _sfx_track is not None:
                bgm_svc.amix_video(video_path, _sfx_track, norm)
            else:
                bgm_svc.loudnorm_video(video_path, norm)
            video_path = norm
            jlog.log("响度归一: -14 LUFS / 峰值 -1.5dB（平台标准）", stage="publish")

        # ── publish ─────────────────────────────────────────────
        _set_job(db, job, stage="publish", progress=90)
        probe = hf.ffprobe(video_path)
        poster_png = out_dir / "renders" / "poster.png"
        hf.extract_poster(video_path, poster_png, at_s=1.0)

        mp4_key = f"projects/{project.id}/renders/{job.id}.mp4"
        storage.upload(video_path, mp4_key, "video/mp4")
        url = storage.presigned_get(mp4_key)

        # 字幕 SRT 也作为产物发布
        srt_real = out_dir / "captions.srt"
        src_srt = srt_real if srt_real.exists() else None
        if src_srt:
            try:
                storage.upload(src_srt, f"projects/{project.id}/renders/{job.id}.srt",
                               "application/x-subrip")
            except Exception:
                pass

        poster_url = ""
        if poster_png.exists():
            pkey = f"projects/{project.id}/renders/{job.id}-poster.png"
            storage.upload(poster_png, pkey, "image/png")
            poster_url = storage.presigned_get(pkey)

        db.add(Artifact(project_id=project.id, job_id=job.id, kind="mp4",
                        object_key=mp4_key, url=url,
                        bytes=video_path.stat().st_size,
                        meta={"duration_s": float(probe.get("format", {}).get("duration", 0)),
                              "gpu_capture": render_used_gpu,
                              "attempts": render_attempts,
                              "bgm": track,
                              "aspect": str(project.aspect)}))
        if poster_url:
            db.add(Artifact(project_id=project.id, job_id=job.id, kind="poster",
                            object_key=pkey, url=poster_url, bytes=poster_png.stat().st_size,
                            meta={}))
        db.add(Artifact(project_id=project.id, job_id=job.id, kind="checks",
                        object_key=f"projects/{project.id}/checks/{job.id}.json",
                        url="", bytes=0,
                        meta={"summary": check}))
        # checks 报告也落一份对象
        try:
            checks_path = out_dir / "renders" / "checks.json"
            checks_path.write_text(json.dumps(check, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            storage.upload(checks_path, f"projects/{project.id}/checks/{job.id}.json",
                           "application/json")
        except Exception:
            pass

        # ── 发布物料：封面 ×3 + 平台文案 ─────────────────────
        try:
            from .covers import generate_covers
            from .themes import theme_tokens
            covers_dir = out_dir / "renders" / "covers"
            cover_pngs = generate_covers(
                project_id=project.id, title=project.title,
                summary=(project.outline or {}).get("summary", ""),
                hook=((project.outline or {}).get("hook") or {}).get("narration", ""),
                scenes_fields=[sc.fields for sc in scenes],
                theme_tokens=theme_tokens(project.theme or "warm", project.brand_accent),
                out_dir=covers_dir,
                brand=project.cover_brand or "AI 视频工坊",
            )
            for cp in cover_pngs:
                ckey = f"projects/{project.id}/covers/{job.id}-{cp.stem}.png"
                storage.upload(cp, ckey, "image/png")
                db.add(Artifact(
                    project_id=project.id, job_id=job.id, kind="cover",
                    object_key=ckey, url=storage.presigned_get(ckey),
                    bytes=cp.stat().st_size,
                    meta={"variant": cp.stem.replace("cover-", "")}))
            if cover_pngs:
                jlog.log(f"封面就绪: {','.join(p.stem.replace('cover-', '') for p in cover_pngs)}",
                         stage="publish")
        except Exception as e:
            jlog.log(f"封面生成跳过: {e}", level="warn", stage="publish")

        if project.outline:
            try:
                from .publish_copy import generate_publish_copy
                copy_data = generate_publish_copy(
                    title=project.title,
                    summary=(project.outline or {}).get("summary", ""),
                    hook=((project.outline or {}).get("hook") or {}).get("narration", ""),
                    beats=(project.outline or {}).get("beats") or [],
                )
                if copy_data:
                    db.add(Artifact(
                        project_id=project.id, job_id=job.id, kind="copy",
                        object_key=f"projects/{project.id}/copy/{job.id}.json",
                        url="", bytes=0, meta={"platforms": copy_data}))
                    jlog.log("平台发布文案就绪: 抖音 / B站 / 视频号", stage="publish")
            except Exception as e:
                jlog.log(f"发布文案跳过: {e}", level="warn", stage="publish")

        # ── 附加画幅（可选：一次渲染同时出竖版/方版）────────────
        from . import bgm as bgm_svc
        from .compose import ScenePlan as _SP
        plans_for_extra = [
            _SP(idx=i, scene_id=f"{i + 1:02d}-{sc.template_id}",
                template_id=sc.template_id, fields=sc.fields,
                voice_text=sc.voice_text, voice_path=wav,
                voice_duration=dur)
            for i, (sc, wav, dur) in enumerate(tts_results)
        ]
        narration_saved = [
            (sc.start_s or 0.0, (sc.start_s or 0.0) + dur)
            for (sc, _, dur) in tts_results
        ]
        # ── A/B 对照版：换音色/换风格再渲一版（最多 2 组）────────
        ab_variants = ((job.payload or {}).get("ab_variants") or [])[:2]
        for k_ab, v_ab in enumerate(ab_variants):
            try:
                from . import bgm as bgm_svc
                ab_track = track
                ab_voice = v_ab.get("voice")
                ab_theme = v_ab.get("theme")
                if ab_voice == project.voice:
                    ab_voice = None
                if ab_theme == (project.theme or "warm"):
                    ab_theme = None
                if not ab_voice and not ab_theme:
                    continue
                tag = f"ab{k_ab + 1}-" + "-".join(
                    x for x in (ab_voice, ab_theme) if x)[:40]
                jlog.log(f"A/B 版 {k_ab + 1}: 音色={ab_voice or '同'} 风格={ab_theme or '同'}",
                         stage="publish")
                ab_dir = _project_dir(project.id).parent / f"project-{project.id}" / f"current-{tag}"
                if ab_voice:
                    tts_ab = _tts_all(db, job, project, scenes, jlog, 85, 4,
                                      voice_override=ab_voice)
                else:
                    tts_ab = tts_results
                plans_ab = [
                    ScenePlan(idx=i, scene_id=f"{i + 1:02d}-{sc.template_id}",
                              template_id=sc.template_id,
                              fields=dict(sc.fields), voice_text=sc.voice_text,
                              voice_path=wav, voice_duration=dur)
                    for i, (sc, wav, dur) in enumerate(tts_ab)
                ]
                compose_project(
                    project_id=project.id, title=project.title,
                    aspect=(project.aspect.value if hasattr(project.aspect, "value") else str(project.aspect)),
                    scenes=plans_ab, out_dir=ab_dir, registry=reg,
                    pads=plan.pads, theme=ab_theme or (project.theme or "warm"),
                    brand_accent=project.brand_accent, bgm=ab_track,
                    progress=bool(getattr(project, "progress_bar", True)),
                    logo_src=(_rlp2 if getattr(project, "logo_path", None)
                              and (_rlp2 := (Path(get_settings().work_dir) / "logos" / project.logo_path)).exists() else None),
                    logo_pos=getattr(project, "logo_pos", "br") or "br",
                    cap_style=getattr(project, "cap_style", "standard") or "standard",
                    series=getattr(project, "series", None),
                    episode_no=getattr(project, "episode_no", None),
                )
                ab_out = hf.render_video(ab_dir, "renders/video.mp4", log_sink=lambda ln: None)
                ab_video = ab_out.output
                if ab_track and ab_track != "none":
                    bed_ab = ab_dir / "renders" / "bgm-bed.wav"
                    narration_ab = [(sc.start_s or 0.0, (sc.start_s or 0.0) + d)
                                    for (sc, _, d) in tts_ab]
                    bgm_svc.make_ducked_bed(
                        ab_track, narration_ab,
                        float(hf.ffprobe(ab_video).get("format", {}).get("duration") or 1),
                        bed_ab)
                    mixed_ab = ab_dir / "renders" / "video-bgm.mp4"
                    bgm_svc.amix_video(ab_video, bed_ab, mixed_ab, sfx_wav=_sfx_track)
                    ab_video = mixed_ab
                else:
                    norm_ab = ab_dir / "renders" / "video-loudnorm.mp4"
                    if _sfx_track is not None:
                        bgm_svc.amix_video(ab_video, _sfx_track, norm_ab)
                    else:
                        bgm_svc.loudnorm_video(ab_video, norm_ab)
                    ab_video = norm_ab
                ab_key = f"projects/{project.id}/renders/{job.id}-{tag}.mp4"
                storage.upload(ab_video, ab_key, "video/mp4")
                db.add(Artifact(project_id=project.id, job_id=job.id, kind="mp4",
                                object_key=ab_key, url=storage.presigned_get(ab_key),
                                bytes=ab_video.stat().st_size,
                                meta={"duration_s": round(float(
                                    hf.ffprobe(ab_video).get("format", {}).get("duration") or 0), 1),
                                    "ab_variant": v_ab}))
                db.commit()
                jlog.log(f"A/B 版 {k_ab + 1} 完成: {tag}", stage="publish")
            except Exception as e:  # noqa: BLE001
                jlog.log(f"A/B 版 {k_ab + 1} 失败（跳过）: {str(e)[:160]}", level="warn",
                         stage="publish")

        track_saved = track
        extra = [a for a in ((job.payload or {}).get("extra_aspects") or [])
                 if a and a != (project.aspect.value if hasattr(project.aspect, "value")
                                else str(project.aspect))]
        for k, asp in enumerate(extra):
            try:
                alt_dir = _project_dir(project.id).parent / f"project-{project.id}" / f"current-{asp.replace(':', 'x')}"
                compose_project(
                    project_id=project.id, title=project.title, aspect=asp,
                    scenes=plans_for_extra, out_dir=alt_dir, registry=reg,
                    pads=plan.pads, theme=project.theme or "warm",
                    brand_accent=project.brand_accent, bgm=track_saved,
                    progress=bool(getattr(project, "progress_bar", True)),
                    logo_src=(_rlp if getattr(project, "logo_path", None)
                              and (_rlp := (Path(get_settings().work_dir) / "logos" / project.logo_path)).exists() else None),
                    logo_pos=getattr(project, "logo_pos", "br") or "br",
                    series=getattr(project, "series", None),
                    episode_no=getattr(project, "episode_no", None),
                    logo_opacity=float(getattr(project, "logo_opacity", 0.85) or 0.85),
                )
                alt_check = hf.run_check(alt_dir, log_sink=lambda ln: None)
                ok2 = alt_check.get("ok", alt_check.get("status") == "pass")
                errs2 = [i for i in (alt_check.get("issues") or [])
                         if str(i.get("severity", "")).lower() == "error"]
                if not ok2 or errs2:
                    jlog.log(f"{asp} check 未通过，跳过该画幅", level="warn", stage="publish")
                    continue
                alt_out = hf.render_video(alt_dir, "renders/video.mp4", log_sink=lambda ln: None)
                alt_video = alt_out.output
                # BGM 床复用（时间线一致）
                if track_saved and track_saved != "none":
                    bed2 = alt_dir / "renders" / "bgm-bed.wav"
                    bgm_svc.make_ducked_bed(track_saved, narration_saved,
                                            float(hf.ffprobe(alt_video).get("format", {}).get("duration") or 1),
                                            bed2)
                    mixed2 = alt_dir / "renders" / "video-bgm.mp4"
                    bgm_svc.amix_video(alt_video, bed2, mixed2, sfx_wav=_sfx_track)
                    alt_video = mixed2
                else:
                    norm2 = alt_dir / "renders" / "video-loudnorm.mp4"
                    bgm_svc.loudnorm_video(alt_video, norm2)
                    alt_video = norm2
                akey = f"projects/{project.id}/renders/{job.id}-{asp.replace(':', '')}.mp4"
                storage.upload(alt_video, akey, "video/mp4")
                db.add(Artifact(
                    project_id=project.id, job_id=job.id, kind="mp4",
                    object_key=akey, url=storage.presigned_get(akey),
                    bytes=alt_video.stat().st_size,
                    meta={"duration_s": float(hf.ffprobe(alt_video).get("format", {}).get("duration", 0)),
                          "gpu_capture": alt_out.used_gpu, "attempts": alt_out.attempts,
                          "bgm": track_saved or "none", "aspect": asp}))
                jlog.log(f"附加画幅 {asp} 完成", stage="publish")
                _set_job(db, job, progress=min(97, 88 + 9 * (k + 1) // max(1, len(extra))))
            except Exception as e:
                jlog.log(f"附加画幅 {asp} 失败（不影响主画幅）: {e}", level="warn", stage="publish")

        project.status = ProjectStatus.DONE
        project.error = None
        db.add(project)
        _set_job(db, job, status=JobStatus.SUCCEEDED, stage="publish", progress=100)
        dur_s = float(probe.get("format", {}).get("duration", 0))
        jlog.log(f"发布完成: MP4 {video_path.stat().st_size / 1e6:.1f}MB · "
                 f"时长 {dur_s:.1f}s · poster={'有' if poster_url else '无'}", stage="publish")
        jlog.flush()
    except Exception as e:
        db.rollback()
        log.exception("render job %s 失败", job_id)
        project = db.get(Project, job.project_id)
        project.status = ProjectStatus.FAILED
        project.error = str(e)[:2000]
        db.add(project)
        _set_job(db, job, status=JobStatus.FAILED, error=str(e))
        jlog.log(f"任务失败: {e}", level="error")
        jlog.flush()
        raise
    finally:
        db.close()


class HFCheckFailed(RuntimeError):
    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report or {}
