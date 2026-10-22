"""系统状态：控制台状态栏（服务/队列/GPU/磁盘）。"""

from __future__ import annotations

import shutil
import subprocess

from fastapi import APIRouter

from ..config import get_settings
from ..services.template_engine import get_registry

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/voices")
def voices():
    """全部可用旁白音色（中文优先）。"""
    from ..services.tts import KOKORO_VOICES, ZH_VOICES
    zh = [{"id": v, "label": KOKORO_VOICES[v]} for v in ZH_VOICES]
    en = [{"id": v, "label": KOKORO_VOICES[v]}
          for v in KOKORO_VOICES if v not in ZH_VOICES]
    return {"zh": zh, "en": en}


@router.post("/voices/preview")
def voice_preview(body: dict):
    """音色试听：合成一句固定样例，返回 wav。"""
    from fastapi.responses import FileResponse

    from ..services.tts import KOKORO_VOICES, synthesize_cached
    voice = (body or {}).get("voice", "zf_xiaobei")
    if voice not in KOKORO_VOICES:
        from fastapi import HTTPException

        raise HTTPException(422, f"未知音色: {voice}")
    s = get_settings()
    text = ("Hello, I am the narrator for this video. "
            "Tech, knowledge and stories — I can tell them all."
            if voice[0] in "ab" else
            "你好，我是这段视频的旁白声音。科技、知识、故事，都可以交给我来讲。")
    r = synthesize_cached(text, voice=voice, speed=1.0,
                          cache_dir=s.work_dir / "tts-cache",
                          line_id=f"preview-{voice}")
    return FileResponse(r.wav_path, media_type="audio/wav",
                        filename=f"preview-{voice}.wav")


@router.get("/status")
def status():
    s = get_settings()

    # Redis / 队列
    queue_depth = None
    workers = 0
    try:
        from redis import Redis
        from rq import Queue, Worker

        conn = Redis.from_url(s.redis_url)
        q = Queue("pipeline", connection=conn)
        queue_depth = q.count
        workers = len(Worker.all(connection=conn))
    except Exception:
        pass

    # GPU
    gpu = None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            gpu = out.stdout.strip().splitlines()[0]
    except Exception:
        pass

    # 磁盘
    try:
        du = shutil.disk_usage(str(s.work_dir))
        disk_free_gb = round(du.free / 1e9, 1)
    except Exception:
        disk_free_gb = None

    # LLM 是否配置
    return {
        "ok": True,
        "workers": workers,
        "queue_depth": queue_depth,
        "gpu": gpu,
        "disk_free_gb": disk_free_gb,
        "templates": len(get_registry().list()),
        "llm_configured": bool(s.llm_base_url and s.llm_model),
        "hf_cli": s.hf_cli_version,
    }
