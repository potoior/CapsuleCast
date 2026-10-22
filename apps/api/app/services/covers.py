"""成片封面生成：三个版式，继承项目主题令牌，chrome-headless-shell 截图。

版式：
  title    —— 中央超大标题 + 副题（B 站/YouTube 通用）
  stat     —— 巨型数字/短语 + 悬念问句（抖音信息流风）
  question —— 标题转问句 + 大「？」装饰（悬念点击版）

全部 1920×1080；用系统 Noto CJK + 内置 woff2 字体，零新依赖。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..config import get_settings

_CHROME = (Path.home() / ".cache" / "ms-playwright" /
           "chromium_headless_shell-1243" / "chrome-headless-shell-linux-arm64" /
           "chrome-headless-shell")

_FONT_DIR = None  # 运行时由 _fonts_css() 解析


def _fonts_css() -> str:
    s = get_settings()
    d = s.shared_assets_dir / "fonts"
    return f"""
@font-face {{ font-family: "Inter"; src: url("file://{d}/Inter-700.woff2") format("woff2"); font-weight: 700; }}
@font-face {{ font-family: "Inter"; src: url("file://{d}/Inter-400.woff2") format("woff2"); font-weight: 400; }}
"""


def _page_css(tokens: dict[str, str]) -> str:
    """主题令牌 → 封面 CSS 变量。"""
    t = {
        "ink": tokens.get("ink", "#141413"),
        "cream": tokens.get("cream", "#FAF9F5"),
        "tile": tokens.get("tile", "#EFE9DE"),
        "coral": tokens.get("coral", "#CC785C"),
        "coral-deep": tokens.get("coral-deep", "#A0523A"),
        "navy": tokens.get("navy", "#181715"),
    }
    body = "".join(f"--{k}: {v};" for k, v in t.items())
    return f":root {{ {body} }}"


_BASE_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: 1920px; height: 1080px; overflow: hidden; }
body {
  background: var(--cream); color: var(--ink);
  font-family: "Inter", "Noto Sans SC", "Noto Sans CJK SC", sans-serif;
  position: relative;
}
.serif { font-family: "Noto Serif SC", "Noto Serif CJK SC", Georgia, serif; }
.mono { font-family: "JetBrains Mono", "Noto Sans Mono CJK SC", monospace; }
.grain {
  position: absolute; inset: 0; pointer-events: none;
  background-image: radial-gradient(color-mix(in srgb, var(--ink) 10%, transparent) 1px, transparent 1px);
  background-size: 26px 26px; opacity: 0.5;
}
.kicker {
  position: absolute; top: 88px; left: 96px;
  font-size: 30px; font-weight: 700; letter-spacing: 0.3em;
  color: var(--coral);
}
.kicker::before { content: "✱ "; }
.badge {
  position: absolute; bottom: 88px; left: 96px;
  font-size: 26px; color: color-mix(in srgb, var(--ink) 55%, var(--cream));
  letter-spacing: 0.12em;
}
"""

_LAYOUTS = {
    "title": """
<div class="grain"></div>
<div class="kicker">__KICKER__</div>
<div style="position:absolute; inset:0; display:flex; flex-direction:column;
     align-items:center; justify-content:center; padding: 0 140px; text-align:center;">
  <div class="serif" style="font-size: 128px; font-weight: 700; line-height: 1.22; max-width: 1500px;
       letter-spacing: -0.02em;">__TITLE__</div>
  <div style="margin-top: 48px; font-size: 38px; font-weight: 400;
       color: color-mix(in srgb, var(--ink) 62%, var(--cream));">__SUB__</div>
  <div style="margin-top: 64px; width: 120px; height: 10px; background: var(--coral);"></div>
</div>
<div class="badge">__BRAND__</div>
""",
    "stat": """
<div class="grain"></div>
<div style="position:absolute; inset:0; display:flex; flex-direction:column;
     align-items:center; justify-content:center; text-align:center;">
  <div style="font-size: 44px; font-weight: 400;
       color: color-mix(in srgb, var(--ink) 60%, var(--cream));
       margin-bottom: 44px;">__QUESTION__</div>
  <div class="serif" style="font-size: 240px; font-weight: 700; line-height: 1;
       color: var(--coral);">__STAT__</div>
  <div style="margin-top: 56px; font-size: 54px; font-weight: 700;">__TITLE__</div>
</div>
<div class="badge">__BRAND__</div>
""",
    "question": """
<div style="position:absolute; inset:0; background: var(--navy);"></div>
<div style="position:absolute; inset:0; display:flex; flex-direction:column;
     align-items:center; justify-content:center; text-align:center; padding: 0 160px;">
  <div class="serif" style="font-size: 48px; color: color-mix(in srgb, var(--cream) 62%, var(--navy));
       margin-bottom: 48px;">一个多数人没想过的问题</div>
  <div class="serif" style="font-size: 112px; font-weight: 700; line-height: 1.36;
       color: var(--cream);">__QUESTION__</div>
  <div style="margin-top: 72px; font-size: 34px; letter-spacing: 0.2em;
       color: var(--coral);">▶ __BRAND__</div>
</div>
""",
}


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _find_stat(scenes_fields: list[dict]) -> tuple[str, str] | None:
    """从场景里找封面可用的核心数字/短语。"""
    for f in scenes_fields:
        st = f.get("stat") or f.get("stat_label")
        if st and len(str(st)) <= 14:
            return str(st), str(f.get("headline") or "")
    for f in scenes_fields:
        bars = f.get("bars") or []
        for b in bars:
            if "|" in b:
                lab, val = b.split("|", 1)
                m = re.search(r"\d", val)
                if m:
                    return val.strip(), lab.strip()
    return None


def generate_covers(
    *,
    project_id: str,
    title: str,
    summary: str,
    hook: str,
    scenes_fields: list[dict],
    theme_tokens: dict[str, str],
    out_dir: Path,
    brand: str = "AI 视频工坊",
) -> list[Path]:
    """生成 3 张封面 PNG，返回路径列表。"""
    get_settings()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 素材加工
    t = _esc(title or "未命名视频")
    question = _esc(hook or summary or title)
    if len(question) > 42:
        question = question[:40] + "…"
    if not question.endswith("？"):
        question = question.rstrip("。！？") + "？"
    stat_pair = _find_stat(scenes_fields)
    stat, stat_label = stat_pair if stat_pair else ("为什么", t)
    kicker = _esc((summary or title)[:12]) if (summary or title) else "本期话题"

    outs: list[Path] = []
    for name, layout in _LAYOUTS.items():
        html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{_fonts_css()}{_page_css(theme_tokens)}{_BASE_CSS}</style></head>
<body>{layout}</body></html>"""
        html = (html.replace("__KICKER__", kicker)
                    .replace("__TITLE__", t)
                    .replace("__SUB__", _esc((summary or "")[:30] or hook[:30] or ""))
                    .replace("__QUESTION__", question)
                    .replace("__STAT__", _esc(stat[:10]))
                    .replace("__BRAND__", _esc(brand)))
        hp = out_dir / f"cover-{name}.html"
        hp.write_text(html, encoding="utf-8")
        png = out_dir / f"cover-{name}.png"
        subprocess.run(
            [str(_CHROME), "--no-sandbox", "--disable-gpu",
             f"--screenshot={png}", "--window-size=1920,1080",
             f"file://{hp}"],
            capture_output=True, text=True, timeout=60,
        )
        if png.exists() and png.stat().st_size > 10_000:
            outs.append(png)
    return outs
