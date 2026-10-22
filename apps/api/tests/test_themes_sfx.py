"""主题令牌 + SFX 合成回归。"""
import numpy as np
from app.services.themes import style_extra_css, theme_tokens, tokens_for_captions


class TestThemeTokens:
    def test_full_pack_has_ink_cream_coral(self):
        for t in ("dark", "broadsheet", "inkwash", "vaporwave", "film",
                  "blueprint", "chalkboard", "neon"):
            tok = theme_tokens(t)
            assert {"ink", "cream", "coral"} <= set(tok), f"{t} 缺基础色令牌"

    def test_plate_overlay_on_every_theme(self):
        """12 风格都要有角标文案——曾因 pack 劫持导致 warm/brand 丢角标。"""
        from app.services.themes import THEME_CHOICES
        for t in THEME_CHOICES:
            tok = theme_tokens(t, "#3366FF" if t == "brand" else None)
            assert tok.get("corner-mark"), f"{t} 缺 plate mark"
            assert float(tok.get("corner-opacity", 0)) > 0
            assert "grain-alpha" in tok

    def test_warm_is_default_tokens_plus_plate(self):
        """warm 无完整 pack：只有 plate overlay，其余走 base.css 默认。"""
        tok = theme_tokens("warm")
        assert "ink" not in tok, "warm 不应自带 ink（base.css 默认）"
        assert tok["corner-mark"] == '"知识胶囊 · CAPSULE"'

    def test_brand_with_accent(self):
        tok = theme_tokens("brand", "#3366FF")
        assert tok["coral"] == "#3366FF"
        assert tok["corner-mark"] == '"BRAND SERIES"'

    def test_captions_incomplete_tokens_early_exit(self):
        """不完整令牌表（只有 plate 的 warm）必须返回空表走默认字幕。"""
        assert tokens_for_captions(theme_tokens("warm")) == {}
        cap = tokens_for_captions(theme_tokens("dark"))
        assert cap["ink"] and cap["cream"]

    def test_style_extra_css_tolerates_plate_only_packs(self):
        """warm/brand 只有 plate overlay 没有 extra——曾在此 KeyError。"""
        assert style_extra_css("warm") == ""
        assert "border-bottom" in style_extra_css("broadsheet")


class TestSFX:
    def test_sound_durations(self):
        from app.services.sfx import _load
        expect = {"whoosh": 0.5, "pop": 0.16, "ding": 0.65, "tick": 0.07, "chime": 0.9}
        for name, want in expect.items():
            got = len(_load(name)) / 44100
            assert abs(got - want) < 0.01, f"{name}: {got:.2f}s ≠ {want}s"

    def test_load_is_mono_not_interleaved(self):
        """曾把立体声交织当单声道读导致时长×2。"""
        from app.services.sfx import _load
        s = _load("pop")
        assert abs(np.abs(s).max() - 0.9) < 0.05

    def test_build_track_events_and_clipping(self, iso_settings, tmp_path):
        from app.services.sfx import build_sfx_track
        out = tmp_path / "sfx.wav"
        p = build_sfx_track([("chime", 0.1), ("whoosh", 5.0), ("pop", 20.0)], 8.0, out)
        assert p is not None and out.exists()
        # 超出总长的事件必须被丢弃
        import wave
        with wave.open(str(out)) as w:
            assert abs(w.getnframes() / 44100 - 8.0) < 0.01

    def test_build_track_no_events_returns_none(self, iso_settings, tmp_path):
        from app.services.sfx import build_sfx_track
        assert build_sfx_track([], 5.0, tmp_path / "x.wav") is None
        assert build_sfx_track([("pop", 99.0)], 5.0, tmp_path / "x.wav") is None
