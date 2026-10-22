"""RQ worker 启动：python -m apps.api.worker 或 make worker。

附带看门狗线程：
  · 僵死任务回收：RUNNING 但心跳（updated_at）超时 → 标 FAILED，用户可重渲
    （防 worker 被杀后任务永远卡在 rendering）
  · 僵尸浏览器清理：无渲染任务进行时，清掉挂了 30 分钟以上的捕获浏览器进程
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.jobs.tasks import QUEUE_NAME
from redis import Redis
from rq import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

log = logging.getLogger("worker.watchdog")

_HEARTBEAT_STALE_MIN = 15   # RUNNING 任务心跳超时（progress 更新最久 5s 一轮，留足余量）
_CHROME_STALE_MIN = 30      # 捕获浏览器进程僵死判定


_WD_SESSION = None


def _wd_session():
    """看门狗专用会话：NullPool（用完真断，不进池）。

    关键约束：RQ 父进程不允许持有池化常活连接——fork 出的子进程会原样
    继承连接池，撞上 psycopg3 的服务端预编译语句名（DuplicatePreparedStatement）。
    """
    global _WD_SESSION
    if _WD_SESSION is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import NullPool

        s = get_settings()
        eng = create_engine(s.database_url, poolclass=NullPool)
        _WD_SESSION = sessionmaker(bind=eng)
    return _WD_SESSION()


def _watchdog_once() -> None:
    """单轮巡检：回收僵死任务 + 清理僵尸浏览器。可安全失败。"""
    try:
        from sqlalchemy import text as _text

        db = _wd_session()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=_HEARTBEAT_STALE_MIN)
            stale = db.execute(_text(
                "SELECT id, project_id FROM jobs WHERE status = 'RUNNING' "
                "AND updated_at < :cutoff"),
                {"cutoff": cutoff}).fetchall()
            for jid, pid in stale:
                db.execute(_text(
                    "UPDATE jobs SET status = 'FAILED', "
                    "error = '心跳超时（疑似僵死），看门狗自动回收，可重新渲染' "
                    "WHERE id = :j"), {"j": jid})
                db.execute(_text(
                    "UPDATE projects SET status = 'error', "
                    "error = '渲染任务僵死已自动回收，可重新渲染' WHERE id = :p "
                    "AND status = 'RENDERING'"), {"p": pid})
                db.commit()
                log.warning("看门狗: 回收僵死任务 %s (项目 %s)", jid, pid)

            # 没有进行中的渲染 → 清僵尸浏览器
            running = db.execute(_text(
                "SELECT count(*) FROM jobs WHERE status = 'RUNNING' "
                "AND kind IN ('render', 'storyboard')")).scalar()
        finally:
            db.close()

        if not running:
            try:
                out = subprocess.run(
                    ["ps", "-eo", "pid,etime,cmd"], capture_output=True, text=True,
                    timeout=10)
                for line in out.stdout.splitlines():
                    if "chrome-headless-shell" not in line:
                        continue
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    etime = parts[1]
                    if "-" in etime or int(etime.split(":")[0]) >= _CHROME_STALE_MIN:
                        subprocess.run(["kill", "-9", parts[0]], timeout=5)
                        log.warning("看门狗: 清理僵尸浏览器 %s (%s)", parts[0], etime)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        log.exception("看门狗巡检失败（下一轮重试）")


def _watchdog_loop(stop: threading.Event) -> None:
    while not stop.wait(60):
        _watchdog_once()


def main() -> int:
    s = get_settings()
    conn = Redis.from_url(s.redis_url)
    queues = [QUEUE_NAME]

    stop = threading.Event()
    t = threading.Thread(target=_watchdog_loop, args=(stop,), daemon=True,
                         name="watchdog")
    t.start()
    log.info("看门狗已启动：僵死任务 %d 分钟回收 · 僵尸浏览器 %d 分钟清理",
             _HEARTBEAT_STALE_MIN, _CHROME_STALE_MIN)

    w = Worker(queues, connection=conn, name="llm2video-worker-1")
    try:
        w.work(burst=False)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
