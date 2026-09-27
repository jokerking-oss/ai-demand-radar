# -*- coding: utf-8 -*-
"""7x24 不定时守护。

不是固定整点 cron——那样容易被目标站点识别成机器人。
每轮之间的间隔 = 基准间隔 × (1 ± jitter_ratio)，且不小于 min_gap_minutes；
深夜 quiet_hours 自动降频。

用法：
    python daemon.py                 前台常驻（能看到实时日志）
    python daemon.py --background    后台模式，日志只写文件
"""
from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import g, load_config, path_of       # noqa: E402
from src.logutil import get_logger                    # noqa: E402
from src.runner import Runner                         # noqa: E402

_STOP = False


def _install_signal_handlers():
    def handler(signum, frame):
        global _STOP
        _STOP = True
        log_placeholder = globals().get("LOG")
        if log_placeholder:
            log_placeholder.info("收到停止信号，跑完当前这一轮就退出…")
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, handler)
        except Exception:
            pass


LOG = None


def in_quiet(now: datetime, windows) -> datetime | None:
    """返回所处静默窗口的结束时间；不在静默期返回 None。"""
    for w in windows or []:
        start_h, end_h = w[0], w[1]
        if start_h <= now.hour < end_h:
            end = now.replace(hour=end_h, minute=0, second=0, microsecond=0)
            return end
    return None


def next_gap_seconds(cfg: dict) -> int:
    rounds = max(1, int(g(cfg, "schedule.rounds_per_day", 24)))
    base = 24 * 3600 / rounds
    jitter = float(g(cfg, "schedule.jitter_ratio", 0.45))
    gap = base * (1 + random.uniform(-jitter, jitter))
    return int(max(gap, int(g(cfg, "schedule.min_gap_minutes", 12)) * 60))


def sleep_until(target: datetime) -> None:
    while not _STOP:
        delta = (target - datetime.now()).total_seconds()
        if delta <= 0:
            return
        time.sleep(min(5.0, delta))


def main() -> int:
    global LOG
    ap = argparse.ArgumentParser(description="AI 需求雷达 · 7x24 守护")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    ap.add_argument("--background", action="store_true", help="后台模式")
    args = ap.parse_args()

    cfg = load_config(args.config)
    LOG = get_logger("daemon", path_of(cfg, "project.log_dir", "logs"))
    _install_signal_handlers()

    runner = Runner(cfg, log=LOG)
    LOG.info("守护进程启动。每天约 %s 轮，间隔带 ±%s%% 抖动，静默时段 %s",
             g(cfg, "schedule.rounds_per_day", 24),
             int(float(g(cfg, "schedule.jitter_ratio", 0.45)) * 100),
             g(cfg, "schedule.quiet_hours", []))

    round_no = 0
    consecutive_fail = 0
    while not _STOP:
        now = datetime.now()
        quiet_end = in_quiet(now, g(cfg, "schedule.quiet_hours", []))
        if quiet_end and round_no > 0:
            LOG.info("深夜降频窗口，睡到 %s", quiet_end.strftime("%H:%M"))
            sleep_until(quiet_end + timedelta(minutes=random.randint(0, 25)))
            if _STOP:
                break
            continue

        round_no += 1
        try:
            do_consolidate = (round_no % max(4, int(g(cfg, "schedule.rounds_per_day", 24)) // 6) == 0)
            runner.run_round(do_consolidate=do_consolidate)
            consecutive_fail = 0
        except KeyboardInterrupt:
            break
        except Exception as e:
            consecutive_fail += 1
            LOG.exception("第 %s 轮执行异常: %s", round_no, e)
            backoff = min(1800, 120 * consecutive_fail)
            LOG.info("出错退避 %s 秒后重试", backoff)
            sleep_until(datetime.now() + timedelta(seconds=backoff))
            continue

        if args.once or _STOP:
            break

        gap = next_gap_seconds(cfg)
        nxt = datetime.now() + timedelta(seconds=gap)
        LOG.info("下一轮：%s（间隔 %.0f 分钟）", nxt.strftime("%H:%M:%S"), gap / 60)
        sleep_until(nxt)

    LOG.info("守护进程已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
