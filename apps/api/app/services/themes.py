"""视觉主题系统：令牌级换肤（模板已全走 var(--token)）。

三套主题：
  warm  —— 默认米白 + 珊瑚（参考项目设计语言）
  dark  —— 深色科技（夜航蓝黑 + 荧光青）
  brand —— 品牌色（暖中性底 + 自定义强调色，自动推导深浅变体）

实现：返回 {token名: 值}，由 template_engine 追加为 ROOT_SEL 覆盖块。
color-mix 派生令牌（hairline/ink-soft 等）自动跟随，无需单独覆盖。
"""

from __future__ import annotations

import colorsys

# 令牌默认值（与 base.css 保持一致；warm 主题返回空 = 不覆盖）
_DEFAULTS = {
    "ink": "#141413",
    "cream": "#FAF9F5",
    "tile": "#EFE9DE",
    "tile-strong": "#ECE3D4",
    "coral": "#CC785C",
    "coral-deep": "#A0523A",
    "navy": "#181715",
}

_DARK = {
    "ink": "#F2F4F8",
    "cream": "#0E1218",
    "tile": "#161C24",
    "tile-strong": "#1D2530",
    "coral": "#3ECFB2",
    "coral-deep": "#22A98D",
    "navy": "#1A222C",
}

THEME_CHOICES = {
    "warm": "暖光 · 米白珊瑚（默认）",
    "dark": "夜航 · 深色科技",
    "broadsheet": "社论 · 黑白报纸",
    "neon": "霓虹 · 赛博朋克",
    "journal": "手账 · 暖粉手写",
    "swiss": "瑞士 · 极简红黑",
    "inkwash": "水墨 · 宣纸朱砂",
    "vaporwave": "蒸汽波 · 紫粉渐变",
    "film": "默片 · 黑白胶片",
    "blueprint": "蓝图 · 工程制图",
    "chalkboard": "黑板 · 粉笔课堂",
    "brand": "品牌 · 自定义强调色",
}

# ── 风格包：颜色 + 字体 + 装饰令牌 + 专属 CSS 片段 ──────────
_STYLE_PACKS: dict[str, dict] = {
    "inkwash": {
        "tokens": {
            "ink": "#26221C", "cream": "#F4EFE4", "tile": "#EAE3D2",
            "tile-strong": "#E0D7C0", "coral": "#A63A2B", "coral-deep": "#7C2A1E",
            "navy": "#3A342B",
            "font-display": '"CJK Serif SC", "EB Garamond", serif',
            "font-body": '"CJK Serif SC", serif',
            "font-mono": '"JetBrains Mono", monospace',
            "spike-char": "\"◈\"", "tile-radius": "0.3cqmin", "pill-radius": "6px",
            "pill-bg": "#F0E9D8",
            "ground-image": ('radial-gradient(ellipse at 28% 18%, color-mix(in srgb, var(--ink) 5%, transparent), transparent 55%), '
                             'radial-gradient(ellipse at 78% 82%, color-mix(in srgb, var(--coral) 4%, transparent), transparent 50%)'),
            "ground-size": "100% 100%, 100% 100%",
        },
        "extra": (
            "ROOT_SEL .hf-display, ROOT_SEL .headline { letter-spacing: 0.03em; }\\n"
            "ROOT_SEL .hf-kicker { color: var(--coral-deep); }"
        ),
    },
    "vaporwave": {
        "tokens": {
            "ink": "#F5EDFF", "cream": "#1A1033", "tile": "#251A4A",
            "tile-strong": "#2D2070", "coral": "#FF6EC7", "coral-deep": "#35E8FF",
            "navy": "#12082A",
            "font-display": '"Inter", system-ui, sans-serif',
            "font-body": '"Inter", system-ui, sans-serif',
            "font-mono": '"JetBrains Mono", monospace',
            "spike-char": "\"▲\"", "tile-radius": "1.6cqmin", "pill-radius": "14px",
            "pill-bg": "#2D1B57",
            "ground-image": ('linear-gradient(160deg, #1A1033 0%, #2B1854 45%, #3A1D6E 70%, #562E9E 100%)'),
            "ground-size": "100% 100%",
        },
        "extra": (
            "ROOT_SEL .hf-display, ROOT_SEL .headline { background: "
            "linear-gradient(90deg, #35E8FF 0%, #FF6EC7 60%, #B98CFF 100%); "
            "-webkit-background-clip: text; background-clip: text; "
            "-webkit-text-fill-color: transparent; }\\n"
            "ROOT_SEL .hf-kicker { color: #35E8FF; }\\n"
            "ROOT_SEL .hf-card { border-color: color-mix(in srgb, #FF6EC7 40%, transparent); }"
        ),
    },
    "film": {
        "tokens": {
            "ink": "#EDEDED", "cream": "#121212", "tile": "#1C1C1C",
            "tile-strong": "#262626", "coral": "#F0F0F0", "coral-deep": "#9A9A9A",
            "navy": "#0A0A0A",
            "font-display": '"EB Garamond", "CJK Serif SC", serif',
            "font-body": '"EB Garamond", "CJK Serif SC", serif',
            "font-mono": '"JetBrains Mono", monospace',
            "spike-char": "\"✦\"", "tile-radius": "0cqmin", "pill-radius": "2px",
            "ground-image": ('radial-gradient(ellipse at center, transparent 52%, rgba(0,0,0,0.55) 100%), '
                             'repeating-linear-gradient(to bottom, rgba(255,255,255,0.018) 0 1px, transparent 1px 4px)'),
            "ground-size": "100% 100%, auto",
        },
        "extra": (
            "ROOT_SEL .hf-display, ROOT_SEL .headline { letter-spacing: 0.05em; color: #F2F2F2; }\\n"
            "ROOT_SEL .hf-kicker { color: #C9C9C9; letter-spacing: 0.28em; }"
        ),
    },
    "blueprint": {
        "tokens": {
            "ink": "#DCE8F5", "cream": "#0D2B4E", "tile": "#12365F",
            "tile-strong": "#174270", "coral": "#FFD23F", "coral-deep": "#6EC1FF",
            "navy": "#081C36",
            "font-display": '"JetBrains Mono", monospace',
            "font-body": '"Inter", system-ui, sans-serif',
            "font-mono": '"JetBrains Mono", monospace',
            "spike-char": "\"＋\"", "tile-radius": "0.2cqmin", "pill-radius": "4px",
            "ground-image": ('linear-gradient(to right, color-mix(in srgb, var(--ink) 5%, transparent) 1px, transparent 1px), '
                             'linear-gradient(to bottom, color-mix(in srgb, var(--ink) 5%, transparent) 1px, transparent 1px), '
                             'linear-gradient(to right, color-mix(in srgb, var(--coral-deep) 16%, transparent) 1.5px, transparent 1.5px), '
                             'linear-gradient(to bottom, color-mix(in srgb, var(--coral-deep) 16%, transparent) 1.5px, transparent 1.5px)'),
            "ground-size": "4cqw 4cqw, 4cqw 4cqw, 24cqw 24cqw, 24cqw 24cqw",
        },
        "extra": (
            "ROOT_SEL .hf-card { border-color: color-mix(in srgb, var(--coral-deep) 50%, transparent); }\\n"
            "ROOT_SEL .hf-kicker { color: var(--coral-deep); }\\n"
            "ROOT_SEL .hf-kicker .hf-spike { color: var(--coral); }"
        ),
    },
    "chalkboard": {
        "tokens": {
            "ink": "#F2EFE6", "cream": "#22392E", "tile": "#2B463A",
            "tile-strong": "#33523F", "coral": "#F7C948", "coral-deep": "#EF8A6B",
            "navy": "#1A2C24",
            "font-display": '"LXGW WenKai", serif',
            "font-body": '"LXGW WenKai", serif',
            "font-mono": '"JetBrains Mono", monospace',
            "spike-char": "\"✎\"", "tile-radius": "0.8cqmin", "pill-radius": "12px",
            "ground-image": ('radial-gradient(ellipse at 20% 25%, rgba(255,255,255,0.03), transparent 45%), '
                             'radial-gradient(ellipse at 80% 75%, rgba(0,0,0,0.22), transparent 55%)'),
            "ground-size": "100% 100%, 100% 100%",
        },
        "extra": (
            "ROOT_SEL .hf-card { border: 2px dashed color-mix(in srgb, var(--ink) 45%, transparent); }\\n"
            "ROOT_SEL .list .item + .item { border-top: 2px dashed color-mix(in srgb, var(--ink) 35%, transparent) !important; }\\n"
            "ROOT_SEL .hf-kicker { color: var(--coral); }"
        ),
    },
    "broadsheet": {
        "tokens": {
            "ink": "#17130E", "cream": "#F7F1E3", "tile": "#EAE1CB",
            "tile-strong": "#E0D4B6", "coral": "#B3211E", "coral-deep": "#7C1512",
            "navy": "#1A1611",
            "font-display": '"EB Garamond", "CJK Serif SC", Georgia, serif',
            "font-body": '"EB Garamond", "CJK Serif SC", Georgia, serif',
            "font-mono": '"JetBrains Mono", monospace',
            "spike-char": "\"❞\"", "tile-radius": "0cqmin", "pill-radius": "3px",
            "ground-image": "none",
        },
        "extra": (
            "ROOT_SEL .hf-kicker { border-bottom: 0.5cqmin solid var(--ink); "
            "padding-bottom: 1.4cqh; width: 100%; }\n"
            "ROOT_SEL .hf-display, ROOT_SEL .headline { font-weight: 700; }"
        ),
    },
    "neon": {
        "tokens": {
            "ink": "#E9F6FF", "cream": "#04060C", "tile": "#0A1120",
            "tile-strong": "#0E1A30", "coral": "#FF3DDB", "coral-deep": "#22D3EE",
            "navy": "#020409",
            "font-display": '"JetBrains Mono", monospace',
            "font-body": '"Inter", system-ui, sans-serif',
            "font-mono": '"JetBrains Mono", monospace',
            "spike-char": "\"▚\"", "tile-radius": "0.4cqmin", "pill-radius": "8px",
            "ground-image": ('repeating-linear-gradient(to bottom, '
                             'color-mix(in srgb, var(--coral) 4%, transparent) 0 1px, transparent 1px 5px), '
                             'linear-gradient(to right, color-mix(in srgb, var(--coral-deep) 5%, transparent) 1px, transparent 1px), '
                             'linear-gradient(to bottom, color-mix(in srgb, var(--coral-deep) 5%, transparent) 1px, transparent 1px)'),
            "ground-size": "auto, 10cqw 10cqw, 10cqw 10cqw",
        },
        "extra": (
            "ROOT_SEL .hf-display, ROOT_SEL .headline { text-shadow: "
            "0 0 2.2cqmin color-mix(in srgb, var(--coral) 55%, transparent); }\n"
            "ROOT_SEL .hf-kicker { color: var(--coral-deep); text-shadow: "
            "0 0 1.2cqmin color-mix(in srgb, var(--coral-deep) 60%, transparent); }\n"
            "ROOT_SEL .hf-card { border-color: color-mix(in srgb, var(--coral) 35%, transparent); }"
        ),
    },
    "journal": {
        "tokens": {
            "ink": "#4B3A34", "cream": "#FBEEE0", "tile": "#F5E0C9",
            "tile-strong": "#EFD3B4", "coral": "#E8695C", "coral-deep": "#C24B41",
            "navy": "#6E4F42",
            "font-display": '"LXGW WenKai", serif',
            "font-body": '"LXGW WenKai", serif',
            "font-mono": '"JetBrains Mono", monospace',
            "spike-char": "\"✿\"", "tile-radius": "2.4cqmin", "pill-radius": "20px",
            "pill-bg": "#F9DFC8",
            "ground-image": ('radial-gradient(color-mix(in srgb, var(--ink) 9%, transparent) 1.2px, transparent 1.2px)'),
            "ground-size": "2.6cqw 2.6cqw",
        },
        "extra": (
            "ROOT_SEL .hf-card { border: 2px dashed var(--hairline-strong); }\n"
            "ROOT_SEL .list .item + .item { border-top: 2px dashed var(--hairline-strong) !important; }\n"
            "ROOT_SEL .hf-kicker { letter-spacing: 0.08em; }"
        ),
    },
    "swiss": {
        "tokens": {
            "ink": "#0A0A0A", "cream": "#FFFFFF", "tile": "#F3F3F3",
            "tile-strong": "#E9E9E9", "coral": "#E4002B", "coral-deep": "#A60020",
            "navy": "#0A0A0A",
            "font-display": '"Inter", system-ui, sans-serif',
            "font-body": '"Inter", system-ui, sans-serif',
            "font-mono": '"JetBrains Mono", monospace',
            "spike-char": "\"■\"", "tile-radius": "0cqmin", "pill-radius": "0px",
            "ground-image": "none",
        },
        "extra": (
            "ROOT_SEL .hf-display, ROOT_SEL .headline { font-weight: 700; letter-spacing: -0.03em; }\n"
            "ROOT_SEL .hf-kicker { letter-spacing: 0.34em; }\n"
            "ROOT_SEL .hf-kicker .hf-spike { background: var(--coral); color: var(--coral); "
            "width: 2.6cqmin; height: 2.6cqmin; font-size: 0; display: inline-block; }"
        ),
    },
}

# ── D 层装饰词汇：右上角 plate mark + 纸纹颗粒 ──────────────
# (corner-mark 必须带引号——content: var() 要求字符串字面量；纸纹 0=关)
_PLATE_PACKS: dict[str, tuple[str, str, str]] = {
    "warm":       ('"知识胶囊 · CAPSULE"', "0.50", "0.13"),
    "dark":       ('"TECH BRIEF · 每日一份"', "0.45", "0"),
    "broadsheet": ('"观察 · THE OBSERVER"', "0.62", "0.18"),
    "neon":       ('"SYS.ONLINE ▸ LIVE"', "0.50", "0"),
    "journal":    ('"手账 · TODAY ★"', "0.55", "0.15"),
    "swiss":      ('"GRID · 1957"', "0.50", "0.06"),
    "inkwash":    ('"丙午 · 秋 · 手记"', "0.55", "0.14"),
    "vaporwave":  ('"AESTHETIC ▲ 1989"', "0.50", "0"),
    "film":       ('"REC ● 24 FPS"', "0.60", "0.22"),
    "blueprint":  ('"DWG NO. 2026-10"', "0.50", "0"),
    "chalkboard": ('"LESSON · 第一课"', "0.55", "0.12"),
    "brand":      ('"BRAND SERIES"', "0.50", "0.10"),
}
_PLATE_OVERRIDES = {t: {"corner-mark": m, "corner-opacity": o, "grain-alpha": g}
                    for t, (m, o, g) in _PLATE_PACKS.items()}
# 清掉此前误注入既有 pack 的三个键（统一由 overlay 层提供）
for _p in _STYLE_PACKS.values():
    _p.get("tokens", {}).pop("corner-mark", None)
    _p.get("tokens", {}).pop("corner-opacity", None)
    _p.get("tokens", {}).pop("grain-alpha", None)


def _hex_ok(v: str) -> bool:
    v = (v or "").strip()
    return len(v) == 7 and v[0] == "#" and all(
        c in "0123456789abcdefABCDEF" for c in v[1:])


def _shift(hex_color: str, factor: float, toward: str = "black") -> str:
    """向黑/白方向移动明度（factor 0-1）。"""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, lum, s = colorsys.rgb_to_hls(r, g, b)
    lum = max(0.0, min(1.0, lum * (1 - factor))) if toward == "black" \
        else max(0.0, min(1.0, lum + (1 - lum) * factor))
    r, g, b = colorsys.hls_to_rgb(h, lum, s)
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def theme_tokens(theme: str, brand_accent: str | None = None) -> dict[str, str]:
    """主题/风格包 → 令牌覆盖表。未知主题/暖色返回空（用默认）。"""
    theme = (theme or "warm").strip().lower()
    if theme == "dark":
        out = dict(_DARK)
    elif theme in _STYLE_PACKS:
        out = dict(_STYLE_PACKS[theme]["tokens"])
    elif theme == "brand" and _hex_ok(brand_accent):
        accent = brand_accent.strip().upper()
        out = {
            "coral": accent,
            "coral-deep": _shift(accent, 0.22, "black"),
            "navy": _shift(accent, 0.55, "black"),
        }
    else:
        out = {}
    out.update(_PLATE_OVERRIDES.get(theme, {}))
    # 深色主题：--ink-faint 若沿用「40% ink + 60% cream」会糊进底色
    # （实弹：blueprint 的下层标签几乎不可读）→ 按底色明度自动提亮。
    ink = out.get("ink", _DEFAULTS["ink"])
    if _hex_ok(ink):
        r, g, b = (int(ink[i:i + 2], 16) for i in (1, 3, 5))
        if (0.2126 * r + 0.7152 * g + 0.0722 * b) > 140:  # 亮字（深底主题）
            out.setdefault("ink-soft", "color-mix(in srgb, var(--ink) 80%, var(--cream))")
            out.setdefault("ink-faint", "color-mix(in srgb, var(--ink) 62%, var(--cream))")
    return out


def style_extra_css(theme: str) -> str:
    """风格包的专属 CSS 片段（ROOT_SEL 占位，由调用方替换为真实选择器）。"""
    theme = (theme or "warm").strip().lower()
    pack = _STYLE_PACKS.get(theme)
    return (pack or {}).get("extra", "")


def tokens_for_captions(tokens: dict[str, str]) -> dict[str, str]:
    """字幕胶囊令牌映射（captions.html 有自己的令牌名）。
    不完整的令牌表（如只有 plate 覆盖的默认风格）→ 返回空表用默认。"""
    if not {"ink", "cream", "coral"} <= set(tokens):
        return {}
    out = {
        "ink": tokens["ink"],
        "cream": tokens["cream"],
        "coral": tokens["coral"],
        "accent-2": tokens.get("tile-strong", tokens.get("cream", "#ECE3D4")),
    }
    if "font-body" in tokens:
        out["cap-font"] = tokens["font-body"]
    if "pill-radius" in tokens:
        out["pill-radius"] = tokens["pill-radius"]
    if "pill-bg" in tokens:
        out["pill-bg"] = tokens["pill-bg"]
    return out


def override_css(root_sel: str, tokens: dict[str, str]) -> str:
    """生成令牌覆盖 CSS（空表返回空串）。"""
    if not tokens:
        return ""
    body = "".join(f"--{k}: {v}; " for k, v in tokens.items())
    return f"\n{root_sel} {{ {body}}}"
