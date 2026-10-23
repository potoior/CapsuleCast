"""批量中文 TTS：Kokoro-82M 单进程合成 + 内容哈希缓存。

一次模型加载合成全部场景语音（CLI 每行重载模型，6 行要 ~1 分钟，
批量模式 ~10 秒），是 3 分钟验收预算的关键优化。
缓存 key = sha256(voice + speed + text) → 同文案重渲不重算。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)

_MODEL_LOCK = threading.Lock()
_MODEL = None

KOKORO_VOICES = {
    "zf_xiaobei": "小北 · 女声，平稳解说",
    "zf_xiaoni": "小妮 · 女声，轻快亲和",
    "zf_xiaoxiao": "小晓 · 女声，温和叙事",
    "zf_xiaoyi": "小依 · 女声，知性柔和",
    "zm_yunjian": "云间 · 男声，沉稳大气",
    "zm_yunxi": "云希 · 男声，清晰讲解",
    "zm_yunxia": "云夏 · 男声，年轻活泼",
    "zm_yunyang": "云扬 · 男声，浑厚正式",
    "af_heart": "Heart · EN 女声",
    "af_bella": "Bella · EN 女声，柔和",
    "af_nicole": "Nicole · EN 女声，清亮",
    "af_sky": "Sky · EN 女声，明快",
    "am_michael": "Michael · EN 男声",
    "am_adam": "Adam · EN 男声，沉稳",
    "am_fenrir": "Fenrir · EN 男声，低沉",
    "am_puck": "Puck · EN 男声，活泼",
    "am_santa": "Santa · EN 男声，厚重",
    "bf_emma": "Emma · EN 女声（英音）",
    "bf_isabella": "Isabella · EN 女声（英音）",
    "bm_george": "George · EN 男声（英音）",
    "bm_lewis": "Lewis · EN 男声（英音）",
}
ZH_VOICES = [k for k in KOKORO_VOICES if k.startswith(("zf_", "zm_"))]

# Edge TTS 音色映射（标准普通话；zf_xiaobei/xiaoni 本身是方言音色，保留原样但默认不用）
EDGE_VOICE_MAP = {
    "zf_xiaobei": "zh-CN-liaoning-XiaobeiNeural",
    "zf_xiaoni": "zh-CN-shaanxi-XiaoniNeural",
    "zf_xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "zf_xiaoyi": "zh-CN-XiaoyiNeural",
    "zm_yunjian": "zh-CN-YunjianNeural",
    "zm_yunxi": "zh-CN-YunxiNeural",
    "zm_yunxia": "zh-CN-YunxiaNeural",
    "zm_yunyang": "zh-CN-YunyangNeural",
}
EDGE_LABELS = {
    "zf_xiaobei": "小北 · 女声（东北口音）",
    "zf_xiaoni": "小妮 · 女声（陕西口音）",
    "zf_xiaoxiao": "小晓 · 女声，标准普通话",
    "zf_xiaoyi": "小依 · 女声，标准普通话，知性",
    "zm_yunjian": "云间 · 男声，标准普通话，沉稳",
    "zm_yunxi": "云希 · 男声，标准普通话，清晰",
    "zm_yunxia": "云夏 · 男声，标准普通话，年轻",
    "zm_yunyang": "云扬 · 男声，标准普通话，浑厚",
}


@dataclass
class TTSResult:
    wav_path: Path
    duration_s: float
    cache_hit: bool


def _model_dir() -> Path:
    return Path.home() / ".cache" / "hyperframes" / "tts"


def _get_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        from kokoro_onnx import Kokoro  # 延迟导入，worker 进程内单例

        model = Kokoro(
            str(_model_dir() / "models" / "kokoro-v1.0.onnx"),
            str(_model_dir() / "voices" / "voices-v1.0.bin"),
        )
        _MODEL = model
        return model


def _is_voice_token_overflow(error: Exception) -> bool:
    return bool(re.search(
        r"index \d+ is out of bounds for axis 0 with size 510",
        str(error), re.IGNORECASE,
    ))


def _split_for_retry(value: str) -> tuple[str, str]:
    """与 hyperframes CLI 相同的溢出重试切分：按句读边界二分。"""
    midpoint = len(value) // 2
    boundaries = [
        i + 1 for i, ch in enumerate(value)
        if (ch in "。！？!?\n" or ch.isspace()) and 0 < i < len(value) - 1
    ]
    split_at = min(boundaries, key=lambda i: abs(i - midpoint)) if boundaries else midpoint
    return value[:split_at], value[split_at:]


def _edge_voice(voice: str) -> str:
    return EDGE_VOICE_MAP.get(voice, voice)


def _synthesize_edge(text: str, *, voice: str, speed: float, out_wav: Path) -> None:
    """微软 Edge TTS（标准普通话）。经 --proxy 走外网；产物统一转 24k 单声道 wav。"""
    import subprocess

    s = get_settings()
    proxy = (s.tts_proxy or s.https_proxy or s.http_proxy or "").strip()
    rate = f"{round((speed - 1) * 100):+d}%"
    cmd = [
        sys.executable, "-m", "edge_tts",
        "--voice", _edge_voice(voice),
        "--text", text,
        "--rate", rate,
        "--write-media", str(out_wav.with_suffix(".mp3")),
    ]
    if proxy:
        cmd += ["--proxy", proxy]
    mp3 = out_wav.with_suffix(".mp3")
    last_err = ""
    for attempt in range(3):
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=s.edge_tts_timeout_s)
        if p.returncode == 0 and mp3.exists() and mp3.stat().st_size > 1024:
            break
        last_err = (p.stderr or str(p.returncode))[-300:]
        mp3.unlink(missing_ok=True)
        time.sleep(0.9 * (attempt + 1))
    if not mp3.exists() or mp3.stat().st_size <= 1024:
        raise RuntimeError(f"edge-tts 失败: {last_err}")
    subprocess.run(["/home/nv5000/.local/bin/ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-y", "-i", str(mp3), "-ar", "24000", "-ac", "1",
                    "-c:a", "pcm_s16le", str(out_wav)], check=True, timeout=60)
    mp3.unlink(missing_ok=True)


def _synthesize_text(model, value: str, *, voice: str, speed: float, lang: str):
    try:
        return model.create(value, voice=voice, speed=speed, lang=lang)
    except IndexError as e:
        if not _is_voice_token_overflow(e) or len(value) < 2:
            raise
        left, right = _split_for_retry(value)
        l_samples, l_rate = _synthesize_text(model, left, voice=voice, speed=speed, lang=lang)
        r_samples, r_rate = _synthesize_text(model, right, voice=voice, speed=speed, lang=lang)
        if l_rate != r_rate:
            raise RuntimeError("Kokoro 两次合成采样率不一致") from None
        samples = list(l_samples) + list(r_samples)
        return samples, l_rate


# 声音前缀 → espeak 语言码（注意：中文是 cmn，不是 zh）
_VOICE_LANG = {
    "z": "cmn",   # 中文（zh_* 声音）
    "e": "es",    # 西班牙语
    "f": "fr-fr",  # 法语
    "j": "ja",    # 日语
    "h": "hi",    # 印地语
    "i": "it",    # 意大利语
    "p": "pt-br",  # 葡萄牙语
    "a": "en-us",  # 美音
    "b": "en-gb",  # 英音
}


def _lang_for_voice(voice: str) -> str:
    return _VOICE_LANG.get(voice[0].lower(), "en-us")


def synthesize_cached(
    text: str,
    *,
    voice: str,
    speed: float,
    cache_dir: Path,
    line_id: str = "01",
) -> TTSResult:
    """合成一行并落缓存；命中直接返回。"""
    s = get_settings()
    provider = (s.tts_provider or "kokoro").strip().lower()
    # Edge 的自然语速偏慢（实弹：16.5s 撞模板 16s 上限）→ 统一 +10%
    eff_speed = speed * 1.10 if provider == "edge" else speed
    key = hashlib.sha256(
        f"{provider}|{voice}|{eff_speed:.3f}|{text}".encode("utf-8")
    ).hexdigest()[:24]
    cache_dir.mkdir(parents=True, exist_ok=True)
    wav = cache_dir / f"{line_id}-{key}.wav"
    meta = cache_dir / f"{line_id}-{key}.json"

    if wav.exists() and meta.exists():
        try:
            dur = json.loads(meta.read_text())["duration_s"]
            return TTSResult(wav_path=wav, duration_s=dur, cache_hit=True)
        except Exception:
            pass

    used = provider
    if provider == "edge":
        try:
            _synthesize_edge(text.strip(), voice=voice, speed=eff_speed, out_wav=wav)
        except Exception as e:  # noqa: BLE001
            log.warning("Edge TTS 失败，回落 Kokoro: %s", str(e)[:200])
            used = "kokoro"

    if used == "kokoro":
        import soundfile as sf

        model = _get_model()
        lang = _lang_for_voice(voice)
        samples, rate = _synthesize_text(model, text.strip(), voice=voice, speed=speed, lang=lang)
        sf.write(str(wav), samples, rate)

    import soundfile as sf  # noqa: F811

    info = sf.info(str(wav))
    dur = info.frames / info.samplerate
    meta.write_text(json.dumps({"duration_s": round(dur, 3), "voice": voice,
                                "speed": speed, "provider": used, "text": text}),
                    encoding="utf-8")
    log.info("TTS %s: %.2fs via %s (cache %s)", line_id, dur, used, key[:8])
    return TTSResult(wav_path=wav, duration_s=round(dur, 3), cache_hit=False)
