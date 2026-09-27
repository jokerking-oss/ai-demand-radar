# -*- coding: utf-8 -*-
"""把浏览器通道抓到的内容丢进系统。

    python import_raw.py "C:/path/to/xhs_dump.json"
    python import_raw.py dump.csv --run        导入后立刻跑一轮抽取

文件会被放进 data/inbox/，由主流水线读取并归档到 data/inbox_done/。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config, path_of        # noqa: E402
from src.logutil import get_logger                  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="浏览器通道 · 内容导入")
    ap.add_argument("file", nargs="+", help="json / csv 文件路径")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--run", action="store_true", help="导入后立刻跑一轮")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log = get_logger("import", path_of(cfg, "project.log_dir", "logs"))
    inbox = path_of(cfg, "sources.browser_import.inbox_dir", "data/inbox")

    for f in args.file:
        src = Path(f)
        if not src.exists():
            log.error("文件不存在：%s", src)
            continue
        dst = inbox / f"{datetime.now():%Y%m%d%H%M%S}_{src.name}"
        shutil.copy2(str(src), str(dst))
        log.info("已放入待处理队列：%s", dst)

    if args.run:
        from src.runner import Runner
        runner = Runner(cfg, log=log)
        runner.run_round()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
