#!/usr/bin/env python3
"""为模板库生成可视化预览：每套模板渲染一帧稳定画面 → apps/web/public/tpl/<id>.webp

用法：
  .venv/bin/python scripts/render_template_previews.py            # 全部模板（warm 主题）
  .venv/bin/python scripts/render_template_previews.py --theme blueprint
  .venv/bin/python scripts/render_template_previews.py diagram-flow chart-bars
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from app.services.compose import ScenePlan, compose_project  # noqa: E402
from app.services.hf import snapshot_frames  # noqa: E402
from app.services.template_engine import TemplateRegistry  # noqa: E402

OUT_DIR = ROOT / "apps" / "web" / "public" / "tpl"
FFMPEG = "/home/nv5000/.local/bin/ffmpeg"

CATEGORY = {
    "图解": ["diagram-flow","diagram-layers","diagram-cycle","diagram-quadrant","diagram-hub",
             "diagram-pyramid","diagram-funnel","diagram-venn","diagram-venn-3","diagram-tree",
             "diagram-radar","diagram-gauge","diagram-stairs","diagram-spectrum","diagram-balance",
             "diagram-fishbone","diagram-journey","diagram-iceberg","diagram-blocks","diagram-domino",
             "diagram-onion","diagram-tug","diagram-swimlane","diagram-loop-duo","diagram-daywheel",
             "diagram-callouts","diagram-bottleneck","diagram-mindmap","diagram-orbit",
             "diagram-triangle","diagram-orgchart","diagram-sequence","diagram-gantt"],
    "数据": ["chart-bars","chart-line","chart-donut","chart-table","chart-kpi","chart-scatter",
             "chart-waterfall","chart-histogram","chart-poll","chart-splitbar","chart-bump",
             "chart-bubble","chart-area","chart-sankey","chart-heatmap","chart-stackedbar",
             "stat-clash","stat-equivalent","stat-dotmatrix","stat-thermometer","stat-row",
             "big-stat","habit-grid"],
    "叙事": ["myth-buster","qa-card","checklist","rank-list","dialogue-quotes","bio-card","news-card",
             "formula-card","price-compare","before-after","pros-cons","facts-trio","tier-list",
             "experiment-log","bio-timeline","compare-trio","progress-bundle","sort-bins",
             "chain-distort","quiz-reveal","chat-bubbles","versus-table","headline-barrage",
             "concept-compare","free-blocks","word-cloud"],
    "装置": ["word-slot","receipt-strip","ticket-stub","nutrition-label","terminal-window",
             "split-flap","game-quest","comic-panel","sticky-notes","trading-card",
             "search-results","email-client"],
    "单据": ["finance-candle","medical-report","legal-clause","grade-card","resume-card",
             "itinerary-card","waybill-track","monitor-dash","menu-board","floor-plan",
             "payslip-stub","invoice-sheet","boarding-pass","certificate-award","pricing-table"],
    "钩子": ["title-hook","bullet-list","quote-cta","image-focus","chapter-card","process-steps",
             "timeline-events","term-demo","term-explainer"],
}
CAT_OF = {tid: cat for cat, ids in CATEGORY.items() for tid in ids}


def silent_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--theme", default="warm")
    ap.add_argument("--at", type=float, default=7.6, help="每镜截帧秒数（场景 9s）")
    args = ap.parse_args()

    reg = TemplateRegistry()
    tpls = [reg.get(t) for t in args.ids] if args.ids else sorted(reg.list(), key=lambda t: t.id)
    missing = [t.id for t in tpls if t.id not in CAT_OF]
    if missing:
        print(f"⚠️ 未归类模板（请在 CATEGORY 里补）: {missing}", file=sys.stderr)

    work = Path(tempfile.mkdtemp(prefix="tplprev-"))
    scenes = []
    for i, t in enumerate(tpls):
        sid = f"{i + 1:03d}-{t.id}"
        vp = work / "assets" / "voice" / f"{sid}.wav"
        silent_wav(vp)
        scenes.append(ScenePlan(idx=i, scene_id=sid, template_id=t.id, fields=t.example,
                                voice_text="", voice_path=vp, voice_duration=9.0))

    dur = 9.0
    res = compose_project(project_id="tplprev", title="模板预览", aspect="16:9",
                          scenes=scenes, out_dir=work, theme=args.theme)
    ats = [dur * i + args.at for i in range(len(tpls))]
    pngs = snapshot_frames(res.project_dir, ats, work / "shots", timeout_s=1800)
    if len(pngs) != len(tpls):
        print(f"⚠️ 截帧数量不符: {len(pngs)} != {len(tpls)}", file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []
    for t, png in zip(tpls, pngs):
        out = OUT_DIR / f"{t.id}.webp"
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(png),
                        "-vf", "scale=720:405", "-quality", "82", str(out)], check=True)
        manifest.append({
            "id": t.id, "name": t.name, "category": CAT_OF.get(t.id, "其他"),
            "motion": t.motion, "role": t.role, "description": t.description,
            "aspects": list(t.aspects), "preview": f"/tpl/{t.id}.webp",
            "theme": args.theme,
        })
        print(f"  ✓ {t.id}")

    mp = OUT_DIR / "index.json"
    existing = {}
    if mp.exists():
        try:
            existing = {x["id"]: x for x in json.loads(mp.read_text(encoding="utf-8"))}
        except Exception:  # noqa: BLE001
            existing = {}
    for m in manifest:
        existing[m["id"]] = m
    mp.write_text(json.dumps(sorted(existing.values(), key=lambda x: x["id"]),
                             ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✓ {len(manifest)} 张预览 → {OUT_DIR}")
    print(f"✓ 清单 → {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
