"""本地端到端验证（不依赖 API/DB/队列）：

  python scripts/e2e_local.py [--skip-render]

驱动：storyboard → tts → compose → hyperframes check → render
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from app.config import get_settings  # noqa: E402
from app.services import hf  # noqa: E402
from app.services.compose import ScenePlan, compose_project  # noqa: E402
from app.services.storyboard import generate_storyboard  # noqa: E402
from app.services.template_engine import TemplateRegistry  # noqa: E402
from app.services.tts import synthesize_cached  # noqa: E402

SAMPLE = (
    "大模型每次调用都在为同样的提示词反复付费。"
    "其实第二次调用，输入价格可能只剩十分之一。"
    "秘密在 token 缓存：模型把算过的部分存成键值对。"
    "没命中，整段前缀都要重算；命中，只算新增的 token。"
    "读显存比算矩阵便宜得多，但首次写入要多付一点。"
    "记住一句话：把稳定的内容放前面，把变化的内容放后面。"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=SAMPLE)
    ap.add_argument("--aspect", default="16:9")
    ap.add_argument("--skip-render", action="store_true")
    ap.add_argument("--skip-check", action="store_true")
    args = ap.parse_args()

    s = get_settings()
    reg = TemplateRegistry()
    out = s.work_dir / "e2e-local" / "current"

    t0 = time.time()

    # 1) 分镜
    engine, scenes = generate_storyboard(args.text, duration_target_s=75)
    print(f"[1] 分镜 OK ({time.time()-t0:.1f}s, 引擎={engine})")
    for i, sc in enumerate(scenes):
        print(f"    镜{i+1}: {sc.template_id:16s} VO={sc.voice_text[:30]}…")

    # 2) TTS
    tts_cache = s.work_dir / "tts-cache"
    plans: list[ScenePlan] = []
    for i, sc in enumerate(scenes):
        r = synthesize_cached(sc.voice_text, voice="zf_xiaobei", speed=1.0,
                              cache_dir=tts_cache, line_id=f"{i+1:02d}")
        plans.append(ScenePlan(
            idx=i, scene_id=f"{i+1:02d}-{sc.template_id}", template_id=sc.template_id,
            fields=sc.fields, voice_text=sc.voice_text,
            voice_path=r.wav_path, voice_duration=r.duration_s,
        ))
        print(f"[2] TTS {i+1}: {r.duration_s:.2f}s ({'缓存' if r.cache_hit else '新合成'}) "
              f"累计 {time.time()-t0:.1f}s")

    # 3) 组装
    result = compose_project(project_id="e2e-local", title="E2E 本地验证",
                             aspect=args.aspect, scenes=plans, out_dir=out, registry=reg)
    print(f"[3] 组装 OK: 总时长 {result.total_duration:.1f}s, 字幕组 {result.groups_count} "
          f"({time.time()-t0:.1f}s) → {out}")

    # 4) check
    if not args.skip_check:
        check = hf.run_check(out)
        import json
        print(f"[4] check: {json.dumps(check, ensure_ascii=False)[:600]}")
        print(f"    check 累计 {time.time()-t0:.1f}s")

    # 5) render
    if not args.skip_render:
        outcome = hf.render_video(out, "renders/video.mp4")
        probe = hf.ffprobe(outcome.output)
        f = probe.get("format", {})
        print(f"[5] 渲染 OK: {outcome.output} · {f.get('duration')}s · "
              f"{outcome.output.stat().st_size/1e6:.1f}MB · GPU={outcome.used_gpu} · "
              f"总耗时 {time.time()-t0:.1f}s")

    print("E2E 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
