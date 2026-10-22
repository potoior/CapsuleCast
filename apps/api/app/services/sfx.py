"""程序化音效层（SFX）：转场 whoosh、要点 pop、数字 ding、章节 chime、开场 tick。

全部 numpy 合成（无素材依赖），缓存到 work_dir/sfx/。
build_sfx_track() 按事件时间线把音效铺满成与成片等长的立体声整轨，
由 bgm.amix_video 三路混入（旁白 + BGM 床 + SFX 轨）。
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from .bgm import SR, _write_wav
from ..config import get_settings

_CACHE: dict[str, np.ndarray] = {}


def _env_exp(n: int, tau: float) -> np.ndarray:
    return np.exp(-np.arange(n) / (tau * SR))


def _sweep(f0: float, f1: float, dur: float) -> np.ndarray:
    """频率指数滑动正弦（相位连续）。"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    k = np.log(f1 / f0) / max(dur, 1e-6)
    phase = 2 * np.pi * f0 * (np.exp(k * t) - 1) / k
    return np.sin(phase)


def _synth(name: str) -> np.ndarray:
    if name == "whoosh":
        dur = 0.5
        n = int(dur * SR)
        rng = np.random.default_rng(7)
        noise = rng.standard_normal(n)
        # 简易低通：块均值（分块变宽 → 听感下沉）
        out = np.empty(n)
        widths = np.linspace(4, 90, n).astype(int)
        cs = np.cumsum(np.concatenate(([0.0], noise)))
        for i in range(n):
            w = widths[i]
            out[i] = (cs[i + 1] - cs[max(0, i + 1 - w)]) / w
        out /= max(1e-9, np.abs(out).max())
        body = out * np.sin(np.linspace(0, np.pi, n)) ** 1.5
        return 0.75 * body + 0.25 * _sweep(620, 150, dur) * np.sin(np.linspace(0, np.pi, n))
    if name == "pop":
        dur = 0.16
        body = _sweep(520, 170, dur) * _env_exp(int(dur * SR), 0.05)
        click = np.zeros(int(dur * SR))
        click[: int(0.004 * SR)] = 0.6
        return body + click
    if name == "ding":
        dur = 0.65
        n = int(dur * SR)
        t = np.arange(n) / SR
        x = (np.sin(2 * np.pi * 1318.5 * t)
             + 0.45 * np.sin(2 * np.pi * 2637.0 * t)
             + 0.22 * np.sin(2 * np.pi * 659.3 * t)) * _env_exp(n, 0.16)
        return x * 0.9
    if name == "tick":
        dur = 0.07
        n = int(dur * SR)
        t = np.arange(n) / SR
        rng = np.random.default_rng(3)
        x = np.sin(2 * np.pi * 1900 * t) * _env_exp(n, 0.012)
        x[: int(0.003 * SR)] += 0.4 * rng.standard_normal(int(0.003 * SR))
        return x
    if name == "chime":
        dur = 0.9
        n = int(dur * SR)
        t = np.arange(n) / SR
        x = np.zeros(n)
        for i, (f, at, g) in enumerate([(1046.5, 0.0, 0.8), (1568.0, 0.09, 0.6)]):
            s = int(at * SR)
            seg = (np.sin(2 * np.pi * f * t[: n - s])
                   + 0.3 * np.sin(2 * np.pi * 2 * f * t[: n - s])) * _env_exp(n - s, 0.22)
            x[s:] += g * seg
        return x * 0.8
    raise ValueError(f"未知音效 {name}")


def _load(name: str) -> np.ndarray:
    if name in _CACHE:
        return _CACHE[name]
    s = get_settings()
    cache_dir = Path(s.work_dir) / "sfx"
    f = cache_dir / f"{name}.wav"
    if not f.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        mono = _synth(name)
        peak = max(1e-9, np.abs(mono).max())
        _write_wav(f, np.stack([mono / peak, mono / peak], axis=1) * 0.9)
    with wave.open(str(f)) as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    mono = raw[0::2].astype(np.float32) / 32768.0  # 解交织取左声道
    _CACHE[name] = mono
    return _CACHE[name]


# 音效默认增益（相对满幅；amix normalize=0 下直接叠加）
_GAINS = {"whoosh": 0.20, "pop": 0.13, "ding": 0.16, "tick": 0.10, "chime": 0.15}


def build_sfx_track(
    events: list[tuple[str, float]],   # (音效名, 绝对时间 s)
    total_s: float,
    out: Path,
) -> Path | None:
    """事件时间线 → 与成片等长的立体声 SFX 整轨。无事件返回 None。"""
    events = [(n, max(0.0, t)) for n, t in events if t < total_s]
    if not events:
        return None
    n = int(total_s * SR)
    track = np.zeros((n, 2), dtype=np.float32)
    for name, at in events:
        snd = _load(name)
        s = int(at * SR)
        e = min(n, s + len(snd))
        if e <= s:
            continue
        g = float(_GAINS.get(name, 0.15))
        track[s:e, 0] += snd[: e - s] * g
        track[s:e, 1] += snd[: e - s] * g
    # 全轨限幅
    track = np.tanh(track * 1.2) * 0.85
    _write_wav(out, track)
    return out
