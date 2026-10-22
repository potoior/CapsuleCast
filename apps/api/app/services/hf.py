"""HyperFrames CLI 封装：check / render / snapshot / ffprobe。

统一的子进程环境：
  · PATH 注入 ~/.local/bin(ffmpeg) 与 venv(kokoro 依赖)
  · HYPERFRAMES_BROWSER_PATH 指向本地 Chrome Headless Shell
  · 代理变量只用于外部资源（本地服务在 NO_PROXY 里）
渲染策略：GPU 捕获优先（--browser-gpu 自动探测），失败自动重试并
最终降级 --no-browser-gpu（SwiftShader 软件渲染）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)


class HFCommandError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1, tail: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.tail = tail


@dataclass
class HFEnv:
    """hyperframes 子进程环境。"""

    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls) -> "HFEnv":
        s = get_settings()
        env = dict(os.environ)
        path_parts = [
            str(s.app_root / ".venv" / "bin"),
            str(Path.home() / ".local" / "bin"),
            env.get("PATH", ""),
        ]
        env["PATH"] = ":".join(p for p in path_parts if p)
        if s.hyperframes_browser_path:
            env["HYPERFRAMES_BROWSER_PATH"] = s.hyperframes_browser_path
        # 代理：外部 CDN/字体走代理，本地服务直连
        if s.http_proxy:
            env.setdefault("http_proxy", s.http_proxy)
            env.setdefault("https_proxy", s.https_proxy or s.http_proxy)
            env["no_proxy"] = s.no_proxy
            env["NO_PROXY"] = s.no_proxy
        # npx 每次校验版本会联网，直接跳过
        env.setdefault("NPM_CONFIG_UPDATE_NOTIFIER", "false")
        return cls(env=env)


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    env: dict[str, str],
    log_sink=None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    e = dict(env)
    if extra_env:
        e.update(extra_env)
    p = subprocess.run(
        args, cwd=str(cwd), env=e, timeout=timeout_s,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
    )
    if log_sink and p.stdout:
        for line in p.stdout.splitlines():
            log_sink(line)
    return p


def _npx(s) -> list[str]:
    return ["npx", "--yes", f"hyperframes@{s.hf_cli_version}"]


# ── check ────────────────────────────────────────────────────

def run_check(project_dir: Path, *, log_sink=None) -> dict:
    """hyperframes check --json → 结构化结果。errors 非空 → raise。"""
    s = get_settings()
    env = HFEnv.build().env
    p = _run(
        _npx(s) + ["check", "--json"],
        cwd=project_dir, timeout_s=s.hf_check_timeout_s, env=env, log_sink=log_sink,
    )
    # --json 输出是尾部一段美化 JSON；从每个候选 "{" 行尝试解析到结尾
    data: dict | None = None
    lines = p.stdout.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("{"):
            try:
                cand = json.loads("\n".join(lines[i:]))
                if isinstance(cand, dict):
                    data = cand
                    break
            except json.JSONDecodeError:
                continue
    if p.returncode != 0 and data is None:
        raise HFCommandError(
            f"hyperframes check 退出码 {p.returncode}",
            exit_code=p.returncode,
            tail="\n".join(lines[-30:]),
        )
    return data or {"ok": p.returncode == 0, "raw_tail": p.stdout[-2000:]}


# ── render ───────────────────────────────────────────────────

@dataclass
class RenderOutcome:
    output: Path
    used_gpu: bool
    attempts: int
    log_tail: str = ""


def render_video(
    project_dir: Path,
    output_rel: str = "renders/video.mp4",
    *,
    log_sink=None,
    max_attempts: int = 3,
    workers: int | None = None,
    quality: str | None = None,
) -> RenderOutcome:
    """渲染 MP4：GPU 优先 → 重试 → 软件降级。分段捕获 + 断点续渲。"""
    s = get_settings()
    env = HFEnv.build().env
    workers = workers or s.hf_render_workers
    quality = quality or s.hf_render_quality
    out_path = project_dir / output_rel
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 尝试序列：GPU（自动探测）×2 → 软件（--no-browser-gpu）
    attempts = [
        {"gpu": True, "force_soft": False},
        {"gpu": True, "force_soft": False},
        {"gpu": False, "force_soft": True},
    ][:max_attempts]

    last_tail = ""
    for i, cfg in enumerate(attempts, 1):
        extra = {"HF_SEGMENTED_CAPTURE": "true"}  # 分段捕获，配合 --resume 断点续渲
        args = _npx(s) + [
            "render",
            "--output", output_rel,
            "--quality", quality,
            "--workers", str(workers),
            "--resume",           # 复用上次已完成的分段
        ]
        if cfg["force_soft"]:
            args.append("--no-browser-gpu")
        else:
            args.append("--browser-gpu")
        try:
            p = _run(args, cwd=project_dir, timeout_s=s.hf_render_timeout_s,
                     env=env, log_sink=log_sink, extra_env=extra)
        except subprocess.TimeoutExpired:
            last_tail = f"render 超时（{s.hf_render_timeout_s}s）"
            log.warning("渲染超时（尝试 %d/%d）", i, len(attempts))
            continue
        last_tail = "\n".join(p.stdout.splitlines()[-40:])
        if p.returncode == 0 and out_path.exists():
            used_gpu = not cfg["force_soft"]
            log.info("渲染成功（第 %d 次，GPU=%s）", i, used_gpu)
            return RenderOutcome(output=out_path, used_gpu=used_gpu, attempts=i,
                                 log_tail=last_tail)
        log.warning("渲染失败（第 %d/%d 次，GPU=%s）rc=%s",
                    i, len(attempts), cfg["force_soft"] is False, p.returncode)
        if log_sink:
            log_sink(f"[render] attempt {i} failed rc={p.returncode}, retrying…")

    raise HFCommandError("渲染失败（GPU + 软件降级均未成功）", tail=last_tail)


# ── snapshot（分镜预览图）─────────────────────────────────────

def snapshot_frames(
    project_dir: Path,
    at_seconds: list[float],
    out_dir: Path,
    *,
    log_sink=None,
    timeout_s: int = 180,
) -> list[Path]:
    """按时间点截帧（一个 Chrome 实例批量截）。返回按序 PNG 路径列表。"""
    s = get_settings()
    env = HFEnv.build().env
    out_dir.mkdir(parents=True, exist_ok=True)
    args = _npx(s) + [
        "snapshot",
        "--at", ",".join(f"{t:.2f}" for t in at_seconds),
        "--no-end", "--keep",
        "-o", str(out_dir),
    ]
    p = _run(args, cwd=project_dir, timeout_s=timeout_s, env=env, log_sink=log_sink)
    if p.returncode != 0:
        raise HFCommandError(f"snapshot 失败 rc={p.returncode}",
                             tail="\n".join(p.stdout.splitlines()[-20:]))
    # --keep 命名：at-<seconds>s.png
    pngs = []
    for t in at_seconds:
        f"at-{t:.2f}s.png".rstrip("0").rstrip(".") if t % 1 else f"at-{int(t)}s.png"
        # 实际命名规则：at-0s / at-1.5s（去尾零）
        cand = None
        for fmt in (f"at-{t:g}s.png", f"at-{t:.2f}s.png"):
            c = out_dir / fmt
            if c.exists():
                cand = c
                break
        if cand is None:
            cands = sorted(out_dir.glob("at-*.png"))
            cand = cands[len(pngs) - 1] if len(cands) >= len(pngs) else None
        if cand:
            pngs.append(cand)
    return pngs


# ── ffprobe ──────────────────────────────────────────────────

def ffprobe(path: Path) -> dict:
    get_settings()
    ffprobe_bin = shutil.which("ffprobe", path=HFEnv.build().env["PATH"])
    p = subprocess.run(
        [ffprobe_bin, "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if p.returncode != 0:
        return {}
    return json.loads(p.stdout or "{}")


def extract_poster(video: Path, out_png: Path, at_s: float = 1.0) -> Path:
    get_settings()
    env_path = HFEnv.build().env["PATH"]
    ffmpeg_bin = shutil.which("ffmpeg", path=env_path)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg_bin, "-y", "-ss", str(at_s), "-i", str(video),
         "-frames:v", "1", str(out_png)],
        capture_output=True, timeout=120,
    )
    return out_png if out_png.exists() else out_png
