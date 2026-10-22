"""auto_feed：定时源 → 自动建项目 → 全自动直出视频。

用法：
  .venv/bin/python scripts/auto_feed.py            # 常驻轮询
  .venv/bin/python scripts/auto_feed.py --once     # 跑一轮退出（配合 cron）
  .venv/bin/python scripts/auto_feed.py --config my-feeds.json

配置（feeds.json，与脚本同目录或 --config 指定）：
{
  "interval_min": 30,
  "defaults": {"voice": "zm_yunxi", "theme": "dark", "bgm": "auto",
               "aspect": "16:9", "direct": true, "title_prefix": "自动搬运"},
  "rss": ["https://example.com/feed.xml"],
  "github_releases": ["vercel/next.js"]
}

状态（已见过的条目）存 work_dir/auto-feed-state.json，重跑不重复建。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from app.config import get_settings  # noqa: E402
from app.services.llm import proxy_for  # noqa: E402

API = "http://127.0.0.1:8300"


def _state_path() -> Path:
    p = get_settings().work_dir / "auto-feed-state.json"
    return p


def _load_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"seen": []}


def _save_state(st: dict) -> None:
    _state_path().write_text(json.dumps(st, ensure_ascii=False, indent=1))


def _fetch_rss(url: str) -> list[tuple[str, str, str]]:
    """返回 [(id, title, summary)]。"""
    with httpx.Client(timeout=30, proxy=proxy_for(url), follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
    out: list[tuple[str, str, str]] = []
    root = ET.fromstring(r.text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag == "item":  # RSS 2.0
            t = it.findtext("title") or ""
            link = it.findtext("link") or ""
            desc = it.findtext("description") or ""
            out.append((link or t, t.strip(), desc.strip()))
        elif tag == "entry":  # Atom
            t = it.findtext("title", namespaces=ns) or ""
            link = ""
            for le in it.findall("link", ns):
                link = le.get("href") or link
            desc = (it.findtext("summary", namespaces=ns)
                    or it.findtext("content", namespaces=ns) or "")
            out.append((link or t, t.strip(), desc.strip()))
    return out


def _fetch_releases(repo: str) -> list[tuple[str, str, str]]:
    url = f"https://api.github.com/repos/{repo}/releases?per_page=5"
    with httpx.Client(timeout=30, proxy=proxy_for(url), follow_redirects=True) as c:
        r = c.get(url, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
    out = []
    for rel in r.json():
        if rel.get("draft"):
            continue
        body = (rel.get("body") or "")[:300]
        tag = rel.get("tag_name") or ""
        out.append((f"{repo}@{tag}", f"{repo} 发布 {tag}", body))
    return out


def _strip_html(s: str) -> str:
    import re

    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def create_project(topic: str, defaults: dict) -> dict | None:
    payload = {
        "mode": "topic",
        "topic": topic[:180],
        "aspect": defaults.get("aspect", "16:9"),
        "voice": defaults.get("voice", "zm_yunxi"),
        "theme": defaults.get("theme", "dark"),
        "bgm": defaults.get("bgm", "auto"),
        "direct": bool(defaults.get("direct", True)),
    }
    prefix = defaults.get("title_prefix") or ""
    if prefix:
        payload["title"] = f"{prefix}｜{topic[:24]}"
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{API}/api/projects", json=payload)
        if r.status_code == 200:
            return r.json()
        print("  建项目失败:", r.status_code, r.text[:200])
    return None


def run_once(cfg: dict) -> None:
    st = _load_state()
    seen: set[str] = set(st["seen"])
    defaults = cfg.get("defaults") or {}

    entries: list[tuple[str, str, str]] = []
    for url in cfg.get("rss") or []:
        try:
            entries += _fetch_rss(url)
        except Exception as e:
            print(f"RSS {url} 拉取失败: {e}")
    for repo in cfg.get("github_releases") or []:
        try:
            entries += _fetch_releases(repo)
        except Exception as e:
            print(f"Releases {repo} 拉取失败: {e}")

    fresh = [(i, t, d) for i, t, d in entries if i and i not in seen]
    print(f"源条目 {len(entries)}，新条目 {len(fresh)}")
    for eid, title, desc in fresh[:5]:  # 单轮最多 5 条，避免队列洪峰
        topic = _strip_html(f"{title}。{desc}")[:180]
        print(f"  → 建项目: {title[:40]}")
        p = create_project(topic, defaults)
        if p:
            print(f"     项目 {p['id']}（直出已启动）")
            seen.add(eid)
            st["seen"] = list(seen)[-500:]
            _save_state(st)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--config", default=str(ROOT / "scripts" / "feeds.json"))
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps({
            "interval_min": 30,
            "defaults": {"voice": "zm_yunxi", "theme": "dark", "bgm": "auto",
                         "aspect": "16:9", "direct": True, "title_prefix": "自动搬运"},
            "rss": [],
            "github_releases": [],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已生成示例配置 {cfg_path}，请编辑后重跑")
        return
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    if args.once:
        run_once(cfg)
        return
    interval = max(5, int(cfg.get("interval_min", 30))) * 60
    print(f"常驻轮询：每 {interval // 60} 分钟")
    while True:
        try:
            run_once(cfg)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("轮询异常:", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
