# -*- coding: utf-8 -*-
"""日志：同时打控制台和文件。"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path


class CallableLogger:
    """既能 log.info(...) 也能 log(...) 的薄壳，代码里两种写法混用都不报错。"""

    def __init__(self, logger: logging.Logger):
        self._log = logger

    def __call__(self, msg, *a, **kw):
        self._log.info(msg, *a, **kw)

    def info(self, msg, *a, **kw):
        self._log.info(msg, *a, **kw)

    def warning(self, msg, *a, **kw):
        self._log.warning(msg, *a, **kw)

    warn = warning

    def error(self, msg, *a, **kw):
        self._log.error(msg, *a, **kw)

    def debug(self, msg, *a, **kw):
        self._log.debug(msg, *a, **kw)

    def exception(self, msg, *a, **kw):
        self._log.exception(msg, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._log, name)


def get_logger(name: str = "radar", log_dir: str | Path = "logs"):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"demand_radar.{name}")
    if not log.handlers:
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)

        fh = logging.FileHandler(
            log_dir / f"{datetime.now():%Y%m%d}.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(fh)

        log.propagate = False
    return CallableLogger(log)
