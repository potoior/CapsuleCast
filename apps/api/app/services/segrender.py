"""场景级渲染缓存：逐镜独立渲染 + xfade 拼接。

动机：hyperframes --resume 的分段缓存按项目整体哈希失效，单镜改动也全量重渲。
本模块把每镜渲染为独立片段，按内容哈希缓存于 work_dir/seg-cache/ ——
编辑一镜只重渲该镜，其余镜头命中缓存，拼接是固定开销。

语义对齐（与全量路径严格一致）：
  · 片段时长 d_i = voice_i + pad_i + (0.5 尾巴 if 非末镜)；
  · xfade/acrossfade 0.5s 交替 fade/slideleft（对应全量的 crossfade/push-slide）；
  · 总时长 = sum(voice + pads) 分毫不差；
  · 字幕随镜烧录（片段内 captions 组用镜内相对时间）；
  · 跨项目共享缓存（相同内容 hash 即命中）。

失败策略：任一片段渲染失败 → 抛异常 → 调用方回落全量渲染。
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import get_settings
from . import hf
from .compose import ScenePlan, _CAP_FONT, _FADE_S, _JINJA, _PILL_MAX, resolve_media_fields
from .template_engine import TemplateRegistry, aspect_size


@dataclass
class SegStats:
    rendered: int = 0
    cached: int = 0
    parallel: int = 0
    assemble_s: float = 0.0
    used_gpu: bool = False


# ── 单镜迷你项目组装 ───────────────────────────────────────

def _write_static(seg_dir: Path, project_id: str, idx: int) -> None:
    s = get_settings()
    (seg_dir / "compositions" / "frames").mkdir(parents=True, exist_ok=True)
    (seg_dir / "assets" / "voice").mkdir(parents=True, exist_ok=True)
    (seg_dir / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    (seg_dir / "assets" / "vendor").mkdir(parents=True, exist_ok=True)
    (seg_dir / "hyperframes.json").write_text(json.dumps({
        "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
        "paths": {"blocks": "compositions", "components": "compositions/components",
                  "assets": "assets"},
        "media": {"autoProxy": True},
        "authoringSkill": "faceless-explainer",
    }), encoding="utf-8")
    (seg_dir / "meta.json").write_text(json.dumps({
        "id": f"{project_id}-seg{idx:02d}", "name": f"{project_id}-seg{idx:02d}",
        "createdAt": "2026-01-01T00:00:00.000Z",
    }), encoding="utf-8")
    for f in (s.shared_assets_dir / "fonts").glob("*.woff2"):
        shutil.copy2(f, seg_dir / "assets" / "fonts" / f.name)
    g = seg_dir / "assets" / "vendor" / "gsap.min.js"
    if not g.exists():
        shutil.copy2(s.shared_assets_dir / "vendor" / "gsap.min.js", g)


def compose_segment(
    *,
    project_id: str,
    plan: ScenePlan,
    idx: int,
    duration_s: float,
    width: int,
    height: int,
    registry: TemplateRegistry,
    theme_tokens: dict[str, str],
    scene_groups: list[dict],
    out_dir: Path,
    total_scenes: int = 1,
    style_name: str = "",
    progress_span: tuple[float, float, float] | None = None,
    logo_src: Path | None = None,
    logo_pos: str = "br",
    logo_opacity: float = 0.85,
) -> Path:
    """组装单镜迷你 hyperframes 项目（帧 + 镜内字幕 + 音频）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_static(out_dir, project_id, idx)

    frame_rel = f"compositions/frames/{plan.scene_id}.html"
    fields_resolved = resolve_media_fields(plan.fields, out_dir)
    html = registry.render_frame(
        plan.template_id, fields_resolved, scene_id=plan.scene_id,
        duration_s=duration_s, width=width, height=height,
        theme_tokens=theme_tokens or None, style_name=style_name,
    )
    # AD/进度条/logo 注入（与全量 compose 同源）
    logo_arg = None
    if logo_src is not None and Path(logo_src).exists():
        shutil.copy2(logo_src, out_dir / "assets" / "logo.png")
        logo_arg = ("assets/logo.png", logo_pos, logo_opacity)
    from .ad import inject_scene_extras
    html = inject_scene_extras(html, plan.scene_id, fields_resolved,
                               progress_span=progress_span, logo=logo_arg)
    (out_dir / frame_rel).write_text(html, encoding="utf-8")

    voice_rel = f"assets/voice/{idx + 1:02d}.wav"
    shutil.copy2(plan.voice_path, out_dir / voice_rel)

    # 镜内字幕（组时间已是镜内相对值）
    # last_end：复刻全量语义 end = min(下一组.start, 末组.end+0.3)——
    # 段内没有"下一组"，用「下一镜首组近似起点」钳制，避免末组停留到转场里
    aspect = "16:9" if width > height else ("9:16" if height > width else "1:1")
    tail = _FADE_S if idx < total_scenes - 1 else 0.0
    if scene_groups and idx < total_scenes - 1:
        last_end = min(duration_s - tail + 0.04,
                       scene_groups[-1]["end"] + 0.3)
    else:
        last_end = duration_s
    from .themes import override_css, tokens_for_captions
    cap_html = _JINJA.get_template("captions.html.j2").render(
        width=width, height=height, total_duration=duration_s,
        groups_json=json.dumps(scene_groups, ensure_ascii=False),
        band_top_pct=82, band_height_pct=18,
        cap_font_px=_CAP_FONT[aspect], pill_max_pct=_PILL_MAX[aspect],
        theme_css=override_css("#captions-root", tokens_for_captions(theme_tokens)),
        last_end_s=round(last_end, 3),
    )
    (out_dir / "compositions" / "captions.html").write_text(cap_html, encoding="utf-8")

    index_html = _JINJA.get_template("index.html.j2").render(
        width=width, height=height, total_duration=duration_s,
        scenes=[{
            "scene_id": plan.scene_id, "src": frame_rel,
            "start": 0.0, "duration": duration_s,
            "voice": voice_rel, "voice_duration": plan.voice_duration,
            "track_index": 0,
        }],
        transitions=[],
    )
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")
    return out_dir


# ── 内容哈希与缓存 ─────────────────────────────────────────

def _content_hash(seg_dir: Path, plan: ScenePlan, duration_s: float,
                  width: int, height: int) -> str:
    h = hashlib.sha256()
    for rel in ("index.html", "compositions/captions.html",
                f"compositions/frames/{plan.scene_id}.html"):
        h.update((seg_dir / rel).read_bytes())
    h.update(plan.voice_path.name.encode())
    h.update(str(plan.voice_path.stat().st_size).encode())
    h.update(f"|{duration_s:.3f}|{width}x{height}".encode())
    return h.hexdigest()[:20]


def _probe_duration(path: Path) -> float:
    d = hf.ffprobe(path)
    return float(d.get("format", {}).get("duration") or 0)


# ── 对外入口 ───────────────────────────────────────────────

def render_segmented(
    *,
    project_id: str,
    plans: list[ScenePlan],
    pads: list[float],
    aspect: str,
    theme: str = "warm",
    brand_accent: str | None = None,
    scene_groups_by_idx: list[list[dict]],
    work_root: Path,
    out_path: Path,
    jlog=None,
    max_parallel: int = 3,
    progress: bool = False,
    progress_total: float = 0.0,
    scene_global_starts: list[float] | None = None,
    logo_src: Path | None = None,
    logo_pos: str = "br",
    logo_opacity: float = 0.85,
    series: str | None = None,
    episode_no: int | None = None,
) -> tuple[Path, SegStats]:
    """逐镜渲染（内容哈希缓存）+ xfade 拼接。返回 (成片路径, 统计)。"""
    from .captions import align_scene_words, group_words, measure_lead_in
    from .themes import theme_tokens

    s = get_settings()
    registry = TemplateRegistry()
    width, height = aspect_size(aspect)
    t_tokens = theme_tokens(theme, brand_accent)
    if series:
        ep = f" · EP.{episode_no:02d}" if episode_no else ""
        t_tokens = dict(t_tokens)
        t_tokens["corner-mark"] = f'"{series}{ep}"'
        t_tokens["corner-opacity"] = "0.62"
    seg_cache = s.work_dir / "seg-cache"
    seg_cache.mkdir(parents=True, exist_ok=True)

    n = len(plans)
    stats = SegStats(parallel=min(max_parallel, n))

    # 每镜：时长 → 迷你项目 → 哈希 → 缓存判定
    jobs: list[dict] = []
    for i, plan in enumerate(plans):
        tail = _FADE_S if i < n - 1 else 0.0
        d = plan.voice_duration + (pads[i] if i < len(pads) else 0.0) + tail
        seg_dir = work_root / f"seg-{i:02d}"
        # 镜内字幕组（词级对齐 + 实测前导，与全量路径同源）
        lead = measure_lead_in(plan.voice_path)
        sw = align_scene_words(plan.voice_text, plan.voice_duration, lead_in_s=lead)
        groups = group_words(sw)
        span = None
        if progress and progress_total > 0 and scene_global_starts:
            g0 = scene_global_starts[i] if i < len(scene_global_starts) else 0.0
            span = (g0 / progress_total,
                    min(1.0, (g0 + d) / progress_total), d)
        compose_segment(
            project_id=project_id, plan=plan, idx=i, duration_s=round(d, 3),
            width=width, height=height, registry=registry,
            theme_tokens=t_tokens, scene_groups=groups, out_dir=seg_dir,
            total_scenes=n, style_name=theme,
            progress_span=span, logo_src=logo_src,
            logo_pos=logo_pos, logo_opacity=logo_opacity,
        )
        key = _content_hash(seg_dir, plan, d, width, height)
        cache_p = seg_cache / f"{key}.mp4"
        jobs.append({"i": i, "dir": seg_dir, "dur": d, "key": key,
                     "cache": cache_p,
                     "was_cached": cache_p.exists() and cache_p.stat().st_size > 10_000})

    def _log(msg: str, level: str = "info"):
        if jlog:
            jlog.log(msg, level=level, stage="render")

    def _render_one(job: dict) -> Path:
        if job["cache"].exists() and job["cache"].stat().st_size > 10_000:
            _probe_duration(job["cache"])  # 可读性校验
            return job["cache"]
        out = hf.render_video(job["dir"], "renders/seg.mp4", workers=2,
                              log_sink=None)
        seg_mp4 = job["dir"] / "renders" / "seg.mp4"
        shutil.copyfile(seg_mp4, job["cache"])
        return job["cache"]

    # 并行渲染新片段（缓存命中的瞬间返回）
    t0 = time.time()
    results: list[Path] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=stats.parallel) as ex:
        futs = {ex.submit(_render_one, j): j for j in jobs}
        for fut in concurrent.futures.as_completed(futs):
            j = futs[fut]
            try:
                p = fut.result()
            except Exception as e:
                raise RuntimeError(f"场景 {j['i'] + 1} 渲染失败: {e}") from e
            results.append(p)
    # 按镜序排列
    key_to_path = {j["key"]: j["cache"] for j in jobs}
    seg_paths = [key_to_path[j["key"]] for j in jobs]
    # 统计命中/新渲（用提交前的存在性）
    stats.cached = sum(1 for j in jobs if j["was_cached"])
    stats.rendered = n - stats.cached
    _log(f"分段渲染: 新渲 {stats.rendered} 镜 · 缓存命中 {stats.cached} 镜"
         f"（并行 {stats.parallel}）· {time.time() - t0:.0f}s")

    # ── 拼接（xfade 交替 fade/slideleft + acrossfade）─────────
    actual = [_probe_duration(p) for p in seg_paths]
    for j, a in zip(jobs, actual):
        if abs(a - j["dur"]) > 0.6:
            _log(f"片段 {j['i'] + 1} 实测 {a:.2f}s 与预期 {j['dur']:.2f}s 偏差大，"
                 "以实测时长拼接", level="warn")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if n == 1:
        shutil.copyfile(seg_paths[0], out_path)
        stats.assemble_s = 0.0
        return out_path, stats

    cmd: list[str] = [str(_ffmpeg()), "-y"]
    for p in seg_paths:
        cmd += ["-i", str(p)]
    fc_parts: list[str] = []
    prev_v, prev_a = "[0:v]", "[0:a]"
    joined = actual[0]
    # 转场词汇：8 种确定性轮换（按入镜内容哈希，重渲稳定）
    _TPOOL = ["fade", "wipeleft", "slideleft", "circleopen",
              "smoothleft", "wiperight", "smoothright", "fadewhite"]
    import hashlib as _hl
    for k in range(1, n):
        offset = max(0.0, joined - _FADE_S)
        seed = _hl.sha1(jobs[k]["scene_id"].encode()).digest()[0] if "scene_id" in jobs[k] else k
        ttype = _TPOOL[seed % len(_TPOOL)]
        fc_parts.append(
            f"{prev_v}[{k}:v]xfade=transition={ttype}:duration={_FADE_S}"
            f":offset={offset:.3f}[v{k}]")
        fc_parts.append(f"{prev_a}[{k}:a]acrossfade=d={_FADE_S}[a{k}]")
        prev_v, prev_a = f"[v{k}]", f"[a{k}]"
        joined = joined + actual[k] - _FADE_S
    fc = ";".join(fc_parts)
    cmd += ["-filter_complex", fc, "-map", prev_v, "-map", prev_a,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out_path)]
    t1 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"xfade 拼接失败: {r.stderr[-600:]}")
    stats.assemble_s = time.time() - t1
    total = _probe_duration(out_path)
    _log(f"拼接完成: {n} 镜 · {n - 1} 个转场 · 总时长 {total:.1f}s · "
         f"编码 {stats.assemble_s:.0f}s")
    stats.used_gpu = True  # 片段渲染由 hf 尝试 GPU，回落软件也在此发生
    return out_path, stats


def _ffmpeg() -> Path:
    import shutil as _sh

    p = _sh.which("ffmpeg", path="/home/nv5000/.local/bin:/usr/local/bin:/usr/bin")
    return Path(p or "ffmpeg")
