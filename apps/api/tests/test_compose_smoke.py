"""compose 冒烟：不渲染，只验证组装产物与注入痕迹（快，~1s）。"""
import glob
import wave
from pathlib import Path

import pytest

from app.services.compose import compose_project, ScenePlan

WAV = Path(sorted(glob.glob(
    "/home/nv5000/data/llm2video-work/tts-cache/01-*.wav"))[0])


def _dur() -> float:
    with wave.open(str(WAV)) as w:
        return w.getnframes() / w.getframerate()


@pytest.mark.skipif(not WAV.exists(), reason="无 TTS 缓存样本")
def test_compose_two_scenes_with_extras(tmp_path):
    d = _dur()
    plans = [
        ScenePlan(idx=0, scene_id="01-bullet-list", template_id="bullet-list",
                  fields={"kicker": "要点", "headline": "两条要点",
                          "bullets": ["第一条", "第二条", "第三条"],
                          "highlight_index": 0, "variant": 0},
                  voice_text="测试。", voice_path=WAV, voice_duration=d),
        ScenePlan(idx=1, scene_id="02-myth-buster", template_id="myth-buster",
                  fields={"kicker": "辟谣", "headline": "试一试",
                          "myth": "这个说法是错的", "truth": "真相其实是这样的"},
                  voice_text="测试。", voice_path=WAV, voice_duration=d),
    ]
    out = tmp_path / "proj"
    res = compose_project(project_id="smoke", title="t", aspect="16:9",
                          scenes=plans, out_dir=out, pads=[1.5, 0.0],
                          theme="warm", progress=True)
    # 结构产物
    assert (out / "index.html").exists()
    for sid in ("01-bullet-list", "02-myth-buster"):
        f = out / "compositions" / "frames" / f"{sid}.html"
        assert f.exists(), f"缺帧 {sid}"
        html = f.read_text()
        # 注入痕迹：单 style/script、AD 参数、进度条
        assert html.count("<style>") == 1 and html.count("</style>") == 1
        assert html.count("<script>") == 1
        assert "window.__AD" in html
        assert ".hf-progress" in html
        assert '__timelines["' + sid + '"].fromTo' in html
    # 音频资产
    assert (out / "assets" / "voice" / "01.wav").exists()
    assert res.total_duration > 0
    assert len(res.scene_starts) == 2
    # 进度条百分比正确性：首镜从 0 开始
    html1 = (out / "compositions" / "frames" / "01-bullet-list.html").read_text()
    assert 'width:"0.00%"' in html1 or "width:\"0%" in html1
    html2 = (out / "compositions" / "frames" / "02-myth-buster.html").read_text()
    assert 'width:"0.00%"' not in html2  # 次镜起点 >0


@pytest.mark.skipif(not WAV.exists(), reason="无 TTS 缓存样本")
def test_media_ref_resolution_inside_block(tmp_path):
    """free-blocks 的 img 块：media: 引用须在段内被解析（whole-string 之外的嵌套）。"""
    from app.services.compose import resolve_media_fields

    fields = {"blocks": ["img|media:projects/x/media/nonexist.png",
                         "list|要点A；要点B"]}
    out = resolve_media_fields(fields, tmp_path)
    # 不存在的对象保持原样（不炸、不吞）
    assert "media:projects/x/media/nonexist.png" in out["blocks"][0]


def test_scene_durations_last_scene_padded_to_total(tmp_path):
    d = _dur()
    plans = [ScenePlan(idx=0, scene_id="01-a", template_id="bullet-list",
                       fields={"kicker": "开头要点", "headline": "三条要点",
                               "bullets": ["第一条要点", "第二条要点", "第三条要点"],
                               "highlight_index": 0},
                       voice_text="x", voice_path=WAV, voice_duration=d),
             ScenePlan(idx=1, scene_id="02-b", template_id="quote-cta",
                       fields={"kicker": "点题收束", "quote": "把观众当聪明人",
                               "cta": "关注我看下期", "footnote": "下期见"},
                       voice_text="x", voice_path=WAV, voice_duration=d)]
    out = tmp_path / "p2"
    res = compose_project(project_id="smoke2", title="t", aspect="16:9",
                          scenes=plans, out_dir=out, pads=[1.5, 0.0], theme="warm")
    assert abs(sum(res.scene_durations) - res.total_duration) < 0.35 + 0.5
