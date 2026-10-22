"""组装 HyperFrames 项目目录：index.html + frames + captions + assets。

产出结构与参考项目 token-cache-explained 完全一致：
  hyperframes.json / meta.json / index.html
  compositions/frames/NN-<template>.html
  compositions/captions.html
  assets/voice/NN.wav  assets/fonts/*  assets/vendor/gsap.min.js
  audio_meta.json（词级时间戳）
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import get_settings
from .captions import SceneWords, align_scene_words, group_words, measure_lead_in
from .template_engine import TemplateRegistry, aspect_size

_JINJA = Environment(
    autoescape=select_autoescape(default=False),  # HTML 内容由模板引擎已转义/受控
    trim_blocks=False,
    lstrip_blocks=False,
    keep_trailing_newline=True,
)
_TPL_DIR = Path(__file__).parent / "compose_templates"
_JINJA.loader = FileSystemLoader(str(_TPL_DIR))

# 转场 crossfade 固定 0.5s（不占片长）；pads 是停留（占片长）
_FADE_S = 0.5
# 竖屏字幕字号更大（参照参考项目 @media 分支）
_CAP_FONT = {"16:9": 52, "1:1": 48, "9:16": 44}
_PILL_MAX = {"16:9": 78, "1:1": 84, "9:16": 90}


@dataclass
class ScenePlan:
    idx: int            # 0 起
    scene_id: str       # 01-title-hook
    template_id: str
    fields: dict
    voice_text: str
    voice_path: Path    # 绝对路径
    voice_duration: float


@dataclass
class ComposeResult:
    project_dir: Path
    total_duration: float
    scene_starts: list[float]
    scene_durations: list[float]
    groups_count: int
    leads: list[float]          # 每镜实测前导静音（字幕校准用）


def resolve_media_fields(fields: dict, out_dir: Path) -> dict:
    """把 media:<object_key> 引用落地为项目内资产，改写为相对路径。

    图片从 storage 拷到 out_dir/assets/media/，模板里直接相对引用。
    """
    import re as _re

    out = dict(fields)

    def _materialize(key: str) -> str:
        from . import storage

        src_path = storage.local_path(key)
        if src_path and src_path.exists():
            media_dir = out_dir / "assets" / "media"
            media_dir.mkdir(parents=True, exist_ok=True)
            dst = media_dir / src_path.name
            if not dst.exists():
                shutil.copy2(src_path, dst)
            return f"assets/media/{src_path.name}"
        return f"media:{key}"

    for k, v in out.items():
        if isinstance(v, str) and "media:" in v:
            out[k] = _re.sub(r"media:([A-Za-z0-9/_\-.]+)",
                             lambda m: _materialize(m.group(1)), v)
    return out


def compose_project(
    *,
    project_id: str,
    title: str,
    aspect: str,
    scenes: list[ScenePlan],
    out_dir: Path,
    registry: TemplateRegistry | None = None,
    pads: list[float] | None = None,   # 每镜尾部停留（pacing 计划）；缺省 0.5/末镜 0
    bgm: str | None = None,            # BGM 轨道名（写入 audio_meta，混音在渲染后做）
    theme: str = "warm",               # 视觉主题 warm|dark|brand
    brand_accent: str | None = None,   # brand 主题的强调色 #RRGGBB
    progress: bool = False,            # 顶部全片进度条
    logo_src: Path | None = None,      # logo 源文件（自动拷入 assets/）
    logo_pos: str = "br",              # tr|br|bl
    logo_opacity: float = 0.85,
    cap_style: str = "standard",       # 字幕样式 standard|large|high
    series: str | None = None,         # 栏目名（角标变「栏目 · EP.NN」）
    episode_no: int | None = None,
) -> ComposeResult:
    s = get_settings()
    reg = registry or TemplateRegistry()
    width, height = aspect_size(aspect)

    from .themes import override_css, theme_tokens, tokens_for_captions
    t_tokens = theme_tokens(theme, brand_accent)
    if series:
        ep = f" · EP.{episode_no:02d}" if episode_no else ""
        t_tokens = dict(t_tokens)
        t_tokens["corner-mark"] = f'"{series}{ep}"'
        t_tokens["corner-opacity"] = "0.62"

    # ── 布局：语音时长驱动时间线 ────────────────────────────────
    # 语义：pads[i] = 第 i 镜的"停留"（hold，下一镜推迟开始，占真实片长）；
    #       每个转场另有固定 0.5s crossfade 重叠（不占片长，纯视觉过渡）。
    #       pads 全 0 时与参考项目排布完全一致（total = sum(voices)）。
    if pads is None:
        pads = [0.0] * max(0, len(scenes))
    pads = list(pads[: len(scenes)]) + [0.0] * max(0, len(scenes) - len(pads))

    # 最短停留补齐：短旁白不能让场景跌破模板 min_duration（占真实片长，非重叠）
    for i, sc in enumerate(scenes):
        try:
            tmin = reg.get(sc.template_id).min_duration_s
        except Exception:
            tmin = 3.5
        fade = 0.0 if i == len(scenes) - 1 else _FADE_S
        need = tmin - (sc.voice_duration + fade)
        if need > 0 and pads[i] < need:
            pads[i] = round(need, 3)

    starts: list[float] = []
    durations: list[float] = []
    t = 0.0
    for i, sc in enumerate(scenes):
        starts.append(t)
        is_last = i == len(scenes) - 1
        dur = sc.voice_duration + pads[i] + (0.0 if is_last else _FADE_S)
        durations.append(dur)
        t += sc.voice_duration + pads[i]
    total = t if scenes else 0.0
    # 末镜时长补齐到 total
    if scenes:
        durations[-1] = max(durations[-1], total - starts[-1])

    # ── 目录与静态资产 ──────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    logo_rel = None
    if logo_src is not None and Path(logo_src).exists():
        shutil.copy2(logo_src, out_dir / "assets" / "logo.png")
        logo_rel = "assets/logo.png"
    (out_dir / "compositions" / "frames").mkdir(parents=True, exist_ok=True)
    (out_dir / "assets" / "voice").mkdir(parents=True, exist_ok=True)
    (out_dir / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    (out_dir / "assets" / "vendor").mkdir(parents=True, exist_ok=True)

    (out_dir / "hyperframes.json").write_text(json.dumps({
        "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
        "paths": {
            "blocks": "compositions",
            "components": "compositions/components",
            "assets": "assets",
        },
        "media": {"autoProxy": True},
        "authoringSkill": "faceless-explainer",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    (out_dir / "meta.json").write_text(json.dumps({
        "id": project_id,
        "name": project_id,
        "createdAt": "2026-01-01T00:00:00.000Z",
    }, ensure_ascii=False), encoding="utf-8")

    # 字体 / gsap（幂等复制）
    for f in (s.shared_assets_dir / "fonts").glob("*.woff2"):
        shutil.copy2(f, out_dir / "assets" / "fonts" / f.name)
    gsap_dst = out_dir / "assets" / "vendor" / "gsap.min.js"
    if not gsap_dst.exists():
        shutil.copy2(s.shared_assets_dir / "vendor" / "gsap.min.js", gsap_dst)
    anim_dst = out_dir / "assets" / "vendor" / "anim.js"
    if not anim_dst.exists():
        shutil.copy2(s.shared_assets_dir / "vendor" / "anim.js", anim_dst)

    # ── 逐镜：帧 HTML + 语音 + 词级对齐 ─────────────────────────
    all_groups: list[dict] = []
    scene_leads: list[float] = []
    audio_meta_voices: list[dict] = []
    frame_files: list[str] = []

    for i, sc in enumerate(scenes):
        scene_id = sc.scene_id
        frame_rel = f"compositions/frames/{scene_id}.html"
        frame_files.append(frame_rel)

        fields_i = resolve_media_fields(sc.fields, out_dir)
        html = reg.render_frame(
            sc.template_id,
            fields_i,
            scene_id=scene_id,
            duration_s=durations[i],
            width=width,
            height=height,
            theme_tokens=t_tokens,
            style_name=theme,
            is_last=(i == len(scenes) - 1),
        )
        # AD 层 + 进度条 + logo（内容哈希种子，全量/分段共用注入器）
        from .ad import inject_scene_extras
        span = None
        if progress and total > 0:
            s0 = starts[i]
            span = (s0 / total, min(1.0, (s0 + durations[i]) / total),
                    durations[i])
        logo_arg = None
        if logo_rel is not None:
            logo_arg = (logo_rel, logo_pos, logo_opacity)
        html = inject_scene_extras(html, scene_id, sc.fields,
                                   progress_span=span, logo=logo_arg)
        (out_dir / frame_rel).write_text(html, encoding="utf-8")

        # 语音：拷贝到 assets/voice/
        voice_rel = f"assets/voice/{i + 1:02d}.wav"
        shutil.copy2(sc.voice_path, out_dir / voice_rel)

        # 词级对齐（比例分配 + 实测前导静音校准）→ 绝对时间（加场景偏移）
        lead = measure_lead_in(sc.voice_path)
        scene_leads.append(lead)
        sw: SceneWords = align_scene_words(sc.voice_text, sc.voice_duration, lead_in_s=lead)
        words_meta = []
        for wi, w in enumerate(sw.visible()):
            words_meta.append({
                "id": f"w{wi}",
                "text": w.text,
                "start": round(w.start, 3),
                "end": round(w.end, 3),
            })
        audio_meta_voices.append({
            "frame": i + 1,
            "path": voice_rel,
            "duration_s": round(sc.voice_duration, 3),
            "words": words_meta,
        })

        for g in group_words(sw):
            all_groups.append({
                "frame": i + 1,
                "start": round(g["start"] + starts[i], 3),
                "end": round(g["end"] + starts[i], 3),
                "text": g["text"],
                "words": [
                    {
                        "text": w["text"],
                        "start": round(w["start"] + starts[i], 3),
                        "end": round(w["end"] + starts[i], 3),
                    }
                    for w in g["words"]
                ],
            })

    # 字幕按时间排序（重叠时前组让位）
    all_groups.sort(key=lambda g: g["start"])

    # ── SRT 导出（一张卡 = 一条字幕）────────────────────────
    def _srt_ts(t: float) -> str:
        ms_total = int(round(max(0.0, t) * 1000))
        h, rem = divmod(ms_total, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    srt_lines: list[str] = []
    for gi, g in enumerate(all_groups, start=1):
        srt_lines.append(str(gi))
        srt_lines.append(f"{_srt_ts(g['start'])} --> {_srt_ts(g['end'])}")
        srt_lines.append(g["text"].replace(" ", ""))  # 中文字幕去词间空格
        srt_lines.append("")
    (out_dir / "captions.srt").write_text("\n".join(srt_lines), encoding="utf-8")

    (out_dir / "audio_meta.json").write_text(json.dumps({
        "tts_provider": "kokoro",
        "voice_id": scenes[0].voice_path.name if scenes else "",
        "bgm": bgm,
        "voices": audio_meta_voices,
        "total_duration_s": round(total, 3),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── captions.html ──────────────────────────────────────────
    _cap_font = _CAP_FONT[aspect] * (1.28 if cap_style == "large" else 1.0)
    _band_top = 76 if cap_style == "high" else 82
    cap_html = _JINJA.get_template("captions.html.j2").render(
        width=width,
        height=height,
        total_duration=total,
        groups_json=json.dumps(all_groups, ensure_ascii=False),
        band_top_pct=_band_top,
        band_height_pct=(100 - _band_top),
        cap_font_px=_cap_font,
        pill_max_pct=_PILL_MAX[aspect],
        theme_css=override_css("#captions-root", tokens_for_captions(t_tokens)),
        last_end_s=total,
    )
    (out_dir / "compositions" / "captions.html").write_text(cap_html, encoding="utf-8")

    # ── index.html（根时间线 + 转场）─────────────────────────────
    scene_ctx = []
    for i, sc in enumerate(scenes):
        scene_ctx.append({
            "scene_id": sc.scene_id,
            "src": frame_files[i],
            "start": starts[i],
            "duration": durations[i],
            "voice": f"assets/voice/{i + 1:02d}.wav",
            "voice_duration": sc.voice_duration,
            "track_index": 0,
        })

    transitions = []
    for i in range(len(scenes) - 1):
        at = starts[i + 1]  # 转场点 = 下一镜起点（含上一镜停留）
        kind = "crossfade" if i % 2 == 0 else "push-slide"
        tr = {
            "from": scenes[i].scene_id,
            "to": scenes[i + 1].scene_id,
            "at": at,
            "kind": kind,
        }
        if kind == "push-slide":
            tr["slide_from"] = -width
            tr["slide_to"] = width
        transitions.append(tr)

    index_html = _JINJA.get_template("index.html.j2").render(
        width=width,
        height=height,
        total_duration=total,
        scenes=scene_ctx,
        transitions=transitions,
    )
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")

    return ComposeResult(
        project_dir=out_dir,
        total_duration=total,
        scene_starts=starts,
        scene_durations=durations,
        groups_count=len(all_groups),
        leads=scene_leads,
    )
