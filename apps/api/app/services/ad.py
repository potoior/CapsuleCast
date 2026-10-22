"""艺术指导层（AD）：每镜确定性的版式变化。

同一条视频里，每一镜的排版轴（左/中/右/强调框）、进场方向、节奏窗
由场景内容哈希决定 —— 内容不变则变体不变（分段缓存友好），
内容一改则版式重新洗牌。全局 CSS 只动共享类（.hf-kicker/.headline/.note），
23 套模板零改动继承；`window.__AD` 供模板 JS 读取进场方向/节奏。
"""
from __future__ import annotations

import hashlib
import json


def ad_for_scene(scene_id: str, fields: dict) -> dict:
    seed_src = f"{scene_id}|{json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)}"
    b = hashlib.sha1(seed_src.encode()).digest()[0]
    return {
        "variant": b % 4,                                # 排版轴: 0左(默认) 1中 2右 3强调框
        "flip": -1 if (b >> 2) & 1 else 1,               # 进场方向镜像
        "pace": (0.85, 0.95, 1.05, 1.15)[(b >> 3) & 3],  # 节奏窗系数
    }


_VARIANT_CSS = {
    0: "",
    1: (
        "ROOT .hf-kicker { justify-content: center; }\n"
        "ROOT .headline { text-align: center; }\n"
        "ROOT .note { text-align: center; margin-left: auto; margin-right: auto; }"
    ),
    2: (
        "ROOT .hf-kicker { justify-content: flex-end; }\n"
        "ROOT .headline { text-align: right; }\n"
        "ROOT .note { text-align: right; margin-left: auto; }"
    ),
    3: (
        "ROOT .hf-kicker { border: 1px solid var(--hairline-strong);"
        " padding: 0.9cqh 1.5cqw; border-radius: var(--pill-radius, 8px);"
        " background: var(--pill-bg, color-mix(in srgb, var(--ink) 4%, transparent));"
        " width: fit-content; }\n"
        "ROOT .headline { font-weight: 700; letter-spacing: 0.01em; }"
    ),
}


def ad_snippet(scene_id: str, ad: dict) -> str:
    """注入到 frame 的 </template> 前：<style> 变体规则。
    （JS 参数改由 inject_scene_extras 缝进主脚本——HF 只执行模板首个 <script>）"""
    css = _VARIANT_CSS[ad["variant"]].replace("ROOT", f"#f-{scene_id}")
    return f"<style>{css}</style>"


# ── 场景附加元素注入：AD + 顶部进度条 + 品牌水印 ──────────────
# 全量 compose 与分段 segrender 共用；内容哈希种子保证重渲一致。

_LOGO_POS_CSS = {
    "tr": "top: 4.4cqh; right: 3.6cqw;",
    "br": "bottom: calc(var(--cap-band-height) * 1cqh + 2.2cqh); right: 3.6cqw;",
    "bl": "bottom: calc(var(--cap-band-height) * 1cqh + 2.2cqh); left: 3.6cqw;",
}


_AM_SRC = None  # AM 动画词汇表源码缓存（帧内缝入）


def inject_scene_extras(
    html: str,
    scene_id: str,
    fields: dict,
    *,
    progress_span: tuple[float, float, float] | None = None,
    logo: tuple[str, str, float] | None = None,   # (assets 相对路径, 位置, 透明度)
) -> str:
    """向已渲染帧注入：AD 变体 CSS/__AD、顶部全片进度条、logo 水印。

    progress_span = (起始百分比, 结束百分比, 场景时长)。
    元素 HTML 注入到合成根 div 开标签后；样式/脚本注入到 </template> 前。
    """
    root = f"#f-{scene_id}"
    inner_html = ""
    tail_css = ""
    tail_js = ""

    # ── 进度条 ──
    if progress_span is not None:
        a, b, d = progress_span
        inner_html += (f'<div class="hf-progress"><i id="s-{scene_id}-pb">'
                       f"</i></div>")
        tail_css += (
            f"{root} .hf-progress {{ position: absolute; top: 0; left: 0; right: 0;"
            f" height: 0.3cqmin; background: var(--tile); z-index: 58; }}\n"
            f"{root} .hf-progress > i {{ display: block; height: 0.3cqmin;"
            f" background: var(--coral); opacity: 0.8; }}\n"
        )
        tail_js += (
            f'(function(){{var t=window.__timelines && window.__timelines["{scene_id}"];'
            f'if(t){{t.fromTo("#s-{scene_id}-pb",{{width:"{a * 100:.2f}%"}},'
            f'{{width:"{b * 100:.2f}%",duration:{max(d, 0.1):.3f},ease:"none"}},0);}}}})();'
        )

    # ── logo 水印 ──
    if logo is not None:
        src, pos, opacity = logo
        pos_css = _LOGO_POS_CSS.get(pos, _LOGO_POS_CSS["br"])
        inner_html += f'<img class="hf-logo" src="{src}" alt="">'
        tail_css += (
            f"{root} .hf-logo {{ position: absolute; {pos_css}"
            " height: 4.6cqmin; width: auto; z-index: 59;"
            f" opacity: {opacity:.2f}; }}\n"
        )
        if pos == "tr":  # 右上被 logo 占用 → 关掉 plate mark 角标
            tail_css += f"{root}::after {{ opacity: 0 !important; }}\n"

    ad = ad_for_scene(scene_id, fields)
    snippet_css = ad_snippet(scene_id, ad)
    snippet_css = snippet_css.replace("<style>", "").replace("</style>", "")
    if tail_css:
        snippet_css += tail_css

    if inner_html:
        m = html.find(f'<div id="f-{scene_id}"')
        if m >= 0:
            g = html.find(">", m)
            if g >= 0:
                html = html[: g + 1] + inner_html + html[g + 1:]

    # ── JS 缝进主脚本（HF 只执行模板首个 <script>，新标签不会跑）──
    # ⓪ AM 动画词汇表源码：帧上下文拿不到宿主 <script>，必须整段缝进帧内
    #    （纯函数定义，无 gsap 依赖；一次性模块级缓存）
    global _AM_SRC
    if _AM_SRC is None:
        from ..config import get_settings as _gs
        _AM_SRC = (_gs().shared_assets_dir / "vendor" / "anim.js").read_text("utf-8")
    if "window.AM" not in html:
        iife0 = html.find("(function () {")
        if iife0 >= 0:
            html = html[:iife0] + _AM_SRC + "\n" + html[iife0:]
    # ① __AD 参数：插在 IIFE 开括号后（模板读取 window.__AD）
    iife = html.find("(function () {")
    if iife >= 0:
        ins = iife + len("(function () {")
        html = (html[:ins]
                + f"\n          window.__AD = {{ flip: {ad['flip']}, pace: {ad['pace']} }};"
                + html[ins:])
    # ② 进度条 tween：插在最后一个 IIFE 收括号前（tl/D 都在作用域内）
    if tail_js and progress_span is not None:
        closer = html.rfind("})();")
        if closer >= 0:
            a_pct = f"{progress_span[0] * 100:.2f}"
            b_pct = f"{progress_span[1] * 100:.2f}"
            d_s = f"{max(progress_span[2], 0.1):.3f}"
            js = (
                'try { if (window.__timelines && window.__timelines["' + scene_id + '"]) '
                '{ window.__timelines["' + scene_id + '"].fromTo("#s-' + scene_id + '-pb",'
                '{width:"' + a_pct + '%"},'
                '{width:"' + b_pct + '%",duration:' + d_s + ',ease:"none"},0); } } catch (e) {}'
            )
            html = html[:closer] + "\n          " + js + "\n        " + html[closer:]
    # CSS 缝进主 style 块（HF 的 CSS 管线对额外 <style> 标签解析不稳）
    if snippet_css:
        html = html.replace("</style>", "\n" + snippet_css + "\n</style>", 1)
    return html
