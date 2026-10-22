"""程序化环境乐 + 人声闪避混音。

不引入任何第三方音频资产：三条氛围轨全部由 numpy 确定性合成（固定种子），
首用时生成到 work_dir/bgm/ 缓存复用。混音走 ffmpeg sidechaincompress：
旁白出现时 BGM 自动压低（ducking），视频流直拷不重编码（秒级完成）。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from ..config import get_settings

log = logging.getLogger(__name__)

SR = 44100
DUR_S = 56  # 每轨长度（ffmpeg -stream_loop 循环拼接）

TRACKS: dict[str, str] = {
    "warm": "暖光 · 轻柔钢琴垫",
    "pulse": "夜航 · 电子脉冲",
    "ambient": "薄雾 · 空灵氛围",
}
BGM_CHOICES = {"auto": "自动（按项目随机）", "none": "无音乐", **TRACKS}


# ── 合成基元（纯 numpy，无 scipy 依赖）──────────────────────

def _t(dur: float = DUR_S) -> np.ndarray:
    return np.arange(int(SR * dur)) / SR


def _lowpass(x: np.ndarray, cutoff_hz: float) -> np.ndarray:
    """FFT 单级低通（软斜率）。"""
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    # 软滚降：cutoff 处 -3dB，4 倍频处趋零
    gain = 1.0 / np.sqrt(1.0 + (freqs / max(cutoff_hz, 1.0)) ** 4)
    return np.fft.irfft(spec * gain, n=len(x))


def _bandpass(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    keep = (freqs >= lo) & (freqs <= hi)
    return np.fft.irfft(spec * keep, n=len(x))


def _delay(x: np.ndarray, sec: float, feedback: float = 0.35) -> np.ndarray:
    d = int(sec * SR)
    y = x.copy()
    for k in range(1, 5):
        amp = feedback ** k
        y[d * k:] += amp * x[:-d * k] if d * k < len(x) else 0
    return y


def _adsr(n: int, a: float, r: float) -> np.ndarray:
    """简化包络：attack/release 秒，中间平。"""
    env = np.ones(n)
    na, nr = min(int(a * SR), n // 2), min(int(r * SR), n // 2)
    if na:
        env[:na] = np.linspace(0, 1, na) ** 2
    if nr:
        env[-nr:] *= np.linspace(1, 0, nr) ** 1.5
    return env


def _sine(freq: float, t: np.ndarray, harmonics: tuple[float, ...] = (1.0,)) -> np.ndarray:
    out = np.zeros_like(t)
    for h, amp in enumerate(harmonics, start=1):
        out += amp * np.sin(2 * np.pi * freq * h * t)
    return out / sum(harmonics)


def _pedal_tone(root: float, t: np.ndarray) -> np.ndarray:
    """软钢琴感：基音 + 衰减谐波 + 指数衰减。"""
    parts = [(1, 1.0), (2, 0.35), (3, 0.12), (4, 0.05), (5, 0.02)]
    x = np.zeros_like(t)
    for h, amp in parts:
        x += amp * np.sin(2 * np.pi * root * h * t) * np.exp(-t * (2.2 + 0.9 * h))
    return x * 0.8


def _normalize(x: np.ndarray, rms: float = 0.05) -> np.ndarray:
    cur = float(np.sqrt(np.mean(x**2))) or 1.0
    return x * (rms / cur)


def _loopable(x: np.ndarray, xf: int = SR * 4) -> np.ndarray:
    """首尾交叉淡化，可无缝循环。"""
    if len(x) <= xf * 2:
        return x
    fade = np.linspace(0, 1, xf) ** 2
    y = x.copy()
    y[:xf] = y[:xf] * fade + x[-xf:] * (1 - fade)
    return y[:-xf]


def _write_wav(path: Path, stereo: np.ndarray) -> None:
    pcm = np.clip(stereo, -1, 1)
    pcm16 = (pcm * 32767).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16.reshape(-1).tobytes())  # (n,2) C序展平 = L,R,L,R 交织


def _stereo(mono: np.ndarray, detune_cents: float = 3.0) -> np.ndarray:
    """加宽立体声：右声道轻微失谐 + 微延迟。"""
    r = np.roll(mono, int(0.012 * SR))
    r = _lowpass(r, 3800)
    return np.stack([_lowpass(mono, 4500), r * 0.96], axis=1)


# ── 三条轨道 ───────────────────────────────────────────────

def _gen_warm() -> np.ndarray:
    """暖光：Cmaj9 钢琴垫 + 五声音阶零星钟音，慢呼吸包络。"""
    rng = np.random.default_rng(7)
    t = _t()
    x = np.zeros_like(t)
    # 和弦垫（C3 E3 G3 B3 D4）：逐音淡入交替呼吸
    for f, ph in zip((130.81, 164.81, 196.0, 246.94, 293.66), (0.0, 1.1, 2.3, 3.4, 4.6)):
        breathe = 0.62 + 0.38 * np.sin(2 * np.pi * t / 16.0 + ph)
        x += 0.32 * _sine(f, t, (1.0, 0.28, 0.08)) * breathe
    # 零星钟音（C 大调五声）：每 3.5-5.5s 一颗
    notes = (523.25, 587.33, 659.25, 783.99, 880.0)
    pos = 2.0
    while pos < DUR_S - 6:
        f = notes[rng.integers(len(notes))]
        seg_t = t - pos
        hit = (seg_t >= 0) & (seg_t < 6)
        bell = np.zeros_like(t)
        bell[hit] = _pedal_tone(f, seg_t[hit])
        x += 0.30 * bell
        pos += rng.uniform(3.2, 5.4)
    x = _delay(x, 0.42, 0.34)
    x = _lowpass(x, 2600)
    x *= (0.6 + 0.4 * np.sin(2 * np.pi * t / 28.0))  # 全局慢呼吸
    return x


def _gen_pulse() -> np.ndarray:
    """夜航：A 小调电子脉冲，100 BPM，暗色低音 + 柔和琶音 + 气声 hat。"""
    rng = np.random.default_rng(23)
    t = _t()
    beat = 0.6  # 100 BPM
    x = np.zeros_like(t)
    # 低音脉冲（A2，每拍一次，指数衰减）
    for b in np.arange(0, DUR_S, beat):
        seg_t = t - b
        hit = (seg_t >= 0) & (seg_t < beat * 1.8)
        x[hit] += 0.5 * _pedal_tone(110.0, seg_t[hit] - 0.0) * np.exp(-seg_t[hit] * 5.0)
    # 八分音符琶音（A 小调五声 A3 C4 D4 E4 G4）
    arp = (220.0, 261.63, 293.66, 329.63, 392.0)
    for k, b in enumerate(np.arange(0, DUR_S, beat / 2)):
        if rng.random() < 0.22:  # 留白
            continue
        f = arp[(k + int(k / 8)) % len(arp)]
        seg_t = t - b
        hit = (seg_t >= 0) & (seg_t < 0.5)
        x[hit] += 0.16 * _sine(f, seg_t[hit], (1.0, 0.22)) * np.exp(-seg_t[hit] * 7.0)
    # 气声 hat：反拍上的带通噪声
    noise = rng.standard_normal(len(t)) * 0.5
    for b in np.arange(beat / 2, DUR_S, beat):
        seg_t = t - b
        hit = (seg_t >= 0) & (seg_t < 0.08)
        tick = np.zeros_like(t)
        tick[hit] = _bandpass(rng.standard_normal(int(hit.sum())) * 0.4, 6000, 11000)
        x += tick * 0.5
    x = _delay(x, 0.30, 0.28)
    x = _lowpass(x, 3400)
    # 呼吸：每 8 拍一个渐强渐弱循环
    x *= 0.65 + 0.35 * np.sin(2 * np.pi * t / (beat * 16) - np.pi / 2)
    return x


def _gen_ambient() -> np.ndarray:
    """薄雾：D 调失谐持续音 + 缓慢噪声涌动 + 稀疏高频星光。"""
    rng = np.random.default_rng(41)
    t = _t()
    x = np.zeros_like(t)
    # 失谐持续音（D2 A2 D3，±0.6Hz 拍频）
    for f in (73.42, 110.0, 146.83):
        x += 0.30 * np.sin(2 * np.pi * f * t + 0.5 * np.sin(2 * np.pi * 0.07 * t))
        x += 0.22 * np.sin(2 * np.pi * (f + 0.6) * t)
    # 缓慢涌动的带通噪声（30s 周期）
    swell = 0.5 + 0.5 * np.sin(2 * np.pi * t / 30.0 - np.pi / 2)
    noise = _bandpass(rng.standard_normal(len(t)), 300, 900)
    x += 0.35 * noise * swell
    # 高频星光（D6 / A5 / E6），每 6-9s 一颗，长衰减
    stars = (1174.66, 880.0, 1318.51)
    pos = 4.0
    while pos < DUR_S - 8:
        f = stars[rng.integers(len(stars))]
        seg_t = t - pos
        hit = (seg_t >= 0) & (seg_t < 9)
        tone = np.zeros_like(t)
        tone[hit] = np.sin(2 * np.pi * f * (seg_t[hit])) * np.exp(-seg_t[hit] * 0.9)
        x += 0.10 * tone
        pos += rng.uniform(6.0, 9.0)
    x = _delay(x, 0.55, 0.42)
    x = _lowpass(x, 5200)
    return x


_GENS = {"warm": _gen_warm, "pulse": _gen_pulse, "ambient": _gen_ambient}


# ── 对外接口 ───────────────────────────────────────────────

def ensure_track(name: str) -> Path:
    """轨道 WAV 就绪（惰性生成 + 缓存）。"""
    if name not in _GENS:
        raise ValueError(f"未知 BGM 轨道: {name}")
    s = get_settings()
    path = s.work_dir / "bgm" / f"{name}.wav"
    if path.exists() and path.stat().st_size > 100_000:
        return path
    log.info("生成 BGM 轨道 %s → %s", name, path)
    mono = _normalize(_loopable(_GENS[name]()))
    # 首尾整体淡入淡出，避免循环点爆音
    n_fade = SR * 2
    mono[:n_fade] *= np.linspace(0, 1, n_fade) ** 2
    mono[-n_fade:] *= np.linspace(1, 0, n_fade) ** 2
    _write_wav(path, _stereo(mono))
    return path


def pick_for(project_id: str) -> str:
    """auto：按项目 ID 确定性挑一条（同一项目稳定）。"""
    h = int(hashlib.sha256(project_id.encode()).hexdigest()[:8], 16)
    return list(_GENS)[h % len(_GENS)]


# ── 确定性闪避：按旁白时间线生成 duck 包络（不用 sidechain 黑盒） ──

def _load_track_mono(name: str) -> np.ndarray:
    path = ensure_track(name)
    with wave.open(str(path)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        data = data.reshape(-1, 2).astype(np.float64) / 32768
    return data.mean(axis=1)


def _agc(x: np.ndarray, target_rms: float, *, block_s: float = 1.0,
         smooth_s: float = 0.6, max_gain: float = 3.0) -> np.ndarray:
    """自动增益控制：逐块压向目标响度并平滑，压平音乐动态起伏。"""
    n = len(x)
    b = max(1, int(block_s * SR))
    nb = int(np.ceil(n / b))
    gains = np.empty(nb)
    for i in range(nb):
        seg = x[i * b:(i + 1) * b]
        r = float(np.sqrt(np.mean(seg**2))) or 1e-9
        gains[i] = float(np.clip(target_rms / r, 1.0 / max_gain, max_gain))
    g = np.repeat(gains, b)[:n]
    k = max(1, int(smooth_s * SR))
    c = np.cumsum(np.concatenate(([0.0], g)))
    sm = (c[k:] - c[:-k]) / k
    pad_l, pad_r = k // 2, k - k // 2
    g = np.concatenate((np.full(pad_l, sm[0]), sm,
                        np.full(pad_r, sm[-1])))[:n]
    return x * g


def make_ducked_bed(
    track: str,
    narration: list[tuple[float, float]],
    total_s: float,
    out: Path,
    *,
    bed_rms: float = 0.05,     # 空档时床响度（-26 dBFS）
    duck_gain: float = 0.32,   # 语音期间增益（≈ -10 dB 闪避）
    lead_s: float = 0.12,
    tail_s: float = 0.35,
    smooth_s: float = 0.30,
) -> Path:
    """按旁白区间把 BGM 床预混好（含包络+淡入淡出+响度归一）。"""
    n = int(total_s * SR)
    t = np.arange(n) / SR

    # 语音掩码
    env = np.ones(n)
    for s, e in narration:
        s, e = max(0.0, s - lead_s), min(total_s, e + tail_s)
        env[(t >= s) & (t <= e)] = duck_gain
    # 平滑（近似 attack/release 斜坡）——cumsum 滑窗均值，O(n)
    k = max(1, int(smooth_s * SR))
    c = np.cumsum(np.concatenate(([0.0], env)))
    sm = (c[k:] - c[:-k]) / k
    pad_l, pad_r = k // 2, k - k // 2
    env = np.concatenate((
        np.full(pad_l, sm[0]),
        sm,
        np.full(pad_r, sm[-1]),
    ))[:n]

    # BGM 循环铺满 + AGC 压平动态（稳态床，不让响乐句顶出来）
    mono = _load_track_mono(track)
    reps = int(np.ceil(n / len(mono)))
    bed = np.tile(mono, reps)[:n]
    bed = _agc(bed, bed_rms)

    bed *= env

    # 全局淡入淡出（最后做：只是形状，不参与响度）
    nf = int(1.5 * SR)
    fo = int(2.5 * SR)
    bed[:nf] *= np.linspace(0, 1, nf) ** 2
    if n > fo:
        bed[-fo:] *= np.linspace(1, 0, fo) ** 1.5
    _write_wav(out, _stereo(bed))
    return out


def amix_video(video: Path, bed_wav: Path, out: Path,
               sfx_wav: Path | None = None) -> Path:
    """旁白视频 + 已闪避 BGM 床（+ 可选 SFX 轨）混音 + LUFS 归一；视频流直拷。"""
    if sfx_wav is not None:
        fc = (
            "[0:a][1:a][2:a]amix=inputs=3:duration=first:normalize=0,"
            "loudnorm=I=-14:TP=-1.5:LRA=11,"
            "alimiter=limit=0.92,"
            "aresample=48000[a]"
        )
        cmd = [
            str(_ffmpeg_bin()), "-y",
            "-i", str(video),
            "-i", str(bed_wav),
            "-i", str(sfx_wav),
            "-filter_complex", fc,
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out),
        ]
    else:
        fc = (
            "[0:a][1:a]amix=inputs=2:duration=first:normalize=0,"
            "loudnorm=I=-14:TP=-1.5:LRA=11,"
            "alimiter=limit=0.92,"
            "aresample=48000[a]"
        )
        cmd = [
            str(_ffmpeg_bin()), "-y",
            "-i", str(video),
            "-i", str(bed_wav),
            "-filter_complex", fc,
            "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out),
        ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"BGM 混音失败: {r.stderr[-800:]}")
    return out


def loudnorm_video(video: Path, out: Path) -> Path:
    """无 BGM 时的响度归一（-14 LUFS 平台标准）；视频流直拷。"""
    cmd = [
        str(_ffmpeg_bin()), "-y", "-i", str(video),
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000",
        "-map", "0:v", "-map", "0:a",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"响度归一失败: {r.stderr[-500:]}")
    return out


def _ffmpeg_bin() -> Path:
    p = shutil.which("ffmpeg", path="/home/nv5000/.local/bin:/usr/local/bin:/usr/bin")
    return Path(p or "ffmpeg")


def _ffprobe_bin() -> Path:
    p = shutil.which("ffprobe", path="/home/nv5000/.local/bin:/usr/local/bin:/usr/bin")
    return Path(p or "ffprobe")


def warm_cache() -> None:
    for name in _GENS:
        ensure_track(name)
