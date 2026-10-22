"""AD 注入器金测：确定性种子、JS/CSS 缝进主块、闭合标签完好、语法可过 node。"""
import subprocess
from pathlib import Path as _Path

from app.services.ad import ad_for_scene, ad_snippet, inject_scene_extras

_FRAME = """<template><style>
        .base { color: red; }
      </style>
      <div id="f-01-x" data-composition-id="01-x" data-duration="10"><div class="clip"></div></div>
      <script>
        (function () {
          var SID = "s-01-x";
          var D = 10;
          var tl = gsap.timeline({ paused: true });
          tl.to({}, { duration: D }, 0);
          window.__timelines = window.__timelines || {};
          window.__timelines["01-x"] = tl;
        })();
      </script></template>"""


def test_ad_seed_deterministic():
    f = {"kicker": "k", "headline": "h"}
    a1 = ad_for_scene("01-x", f)
    a2 = ad_for_scene("01-x", dict(f))
    assert a1 == a2, "同内容必须同种子（分段缓存依赖）"
    f2 = dict(f, headline="别的标题")
    # 不同内容大概率落到不同变体（4 轴 + flip + pace 的组合空间大）
    assert ad_for_scene("01-x", f2) != a1 or ad_for_scene("01-y", f) != a1


def test_variant_in_range():
    for i in range(50):
        ad = ad_for_scene(f"s{i}", {"x": i})
        assert ad["variant"] in (0, 1, 2, 3)
        assert ad["flip"] in (1, -1)
        assert 0.85 <= ad["pace"] <= 1.15


def test_snippet_has_no_script_tag():
    """HF 只执行首个 <script>——snippet 只允许 <style>。"""
    ad = ad_for_scene("01-x", {})
    assert "<script" not in ad_snippet("01-x", ad)


def test_inject_sews_css_into_main_style():
    out = inject_scene_extras(_FRAME, "01-x", {}, progress_span=(0.0, 0.5, 10.0))
    # 不允许出现第二个 <style>（HF CSS 管线对额外标签解析不稳）
    assert out.count("<style>") == 1
    assert out.count("</style>") == 1, "主 style 闭合标签必须保留"
    assert ".hf-progress" in out
    assert "#f-01-x .hf-progress" in out


def test_inject_sews_js_into_main_script(tmp_path):
    out = inject_scene_extras(_FRAME, "01-x", {}, progress_span=(0.1, 0.4, 10.0))
    # 不允许出现第二个 <script> 标签（HF 只跑首个）
    assert out.count("<script>") == 1
    assert 'window.__AD = ' in out, "AD 参数须缝进主脚本"
    assert '__timelines["01-x"].fromTo' in out
    # node 语法校验（缝合不允许破坏主脚本）
    import re
    js = re.search(r"<script>(.*?)</script>", out, re.S).group(1)
    jf = tmp_path / "probe.js"
    jf.write_text(js)
    p = subprocess.run(["node", "--check", str(jf)], capture_output=True, text=True)
    assert p.returncode == 0, f"缝合后 JS 语法错误:\n{p.stderr[:300]}"


def test_inject_progress_element_inside_root():
    out = inject_scene_extras(_FRAME, "01-x", {}, progress_span=(0.0, 1.0, 10.0))
    root_at = out.find('<div id="f-01-x"')
    prog_at = out.find('<div class="hf-progress"')
    ground_at = out.find('<div class="clip"')
    assert root_at < prog_at < ground_at, "进度条 div 必须在合成根内部"


def test_inject_logo_suppresses_tr_corner():
    out = inject_scene_extras(_FRAME, "01-x", {}, logo=("assets/logo.png", "tr", 0.9))
    assert ".hf-logo" in out
    assert "opacity: 0 !important" in out, "tr 位 logo 必须关掉 plate 角标"
    out2 = inject_scene_extras(_FRAME, "01-x", {}, logo=("assets/logo.png", "br", 0.9))
    assert "opacity: 0 !important" not in out2


def test_no_progress_no_element():
    out = inject_scene_extras(_FRAME, "01-x", {})
    assert "hf-progress" not in out
    assert "hf-logo" not in out


# ── AM 动画词汇表缝入（帧上下文拿不到宿主 <script>，不缝即全帧冻结）──

def test_am_source_sewn_into_frame_before_iife(tmp_path):
    """compose 后帧 HTML 必须含 window.AM 定义，且在 IIFE 之前。"""
    import glob as _glob
    import shutil as _shutil
    import subprocess as _sp
    import sys as _sys
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from app.services.compose import ScenePlan, compose_project
    from app.services.template_engine import TemplateRegistry
    from app.config import get_settings

    _wav_dir = get_settings().work_dir / "tts-cache"
    wav = sorted(_glob.glob(str(_wav_dir / "*.wav")))
    if not wav:  # 无 TTS 缓存时跳过（CI 环境）
        pytest.skip("无 TTS 样本")
    reg = TemplateRegistry()
    out = tmp_path / "amtest"
    scenes = [ScenePlan(idx=0, scene_id="01-bullet-list", template_id="bullet-list",
                        fields=dict(reg.get("bullet-list").example), voice_text="测",
                        voice_path=_Path(wav[0]), voice_duration=7.0)]
    res = compose_project(project_id="amtest", title="t", aspect="16:9",
                          scenes=scenes, out_dir=out, theme="warm")
    h = (res.project_dir / "compositions/frames/01-bullet-list.html").read_text()
    assert "window.AM" in h, "AM 源码未缝入帧内"
    assert h.index("window.AM") < h.index("(function () {"), "AM 必须在帧 IIFE 之前"


def test_all_template_scripts_parse_after_jinja_strip():
    """HF check 不查帧 JS 语法 → 套件自补：剥掉 Jinja 后全部模板脚本过 node --check。

    背景：补丁工具曾把引号拼接搞坏（"[id^='…"），tpl-check 全绿但帧整段冻结。
    """
    import re as _re
    import subprocess as _subprocess
    root = _Path(__file__).resolve().parents[3] / "templates"
    bad = []
    for d in sorted(root.iterdir()):
        f = d / "template.html"
        if not (d.is_dir() and f.exists()) or d.name.startswith("_"):
            continue
        src = f.read_text()
        if "<script>" not in src:
            continue
        s, e = src.index("<script>") + 8, src.index("</script>")
        js = _re.sub(r"\{\{.*?\}\}", "1", src[s:e])
        js = _re.sub(r"\{%.*?%\}", "", js)
        p = "/tmp/opencode/_tpl_js_check.js"
        _Path(p).write_text(js)
        r = _subprocess.run(["node", "--check", p], capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(d.name)
    assert not bad, f"模板帧 JS 语法错误（HF check 查不出来，帧会整段冻结）: {bad}"
