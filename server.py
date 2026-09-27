# -*- coding: utf-8 -*-
"""看板服务：一个本地网页，把"哪些需求被提得最多"直接摆出来。

    python server.py
    浏览器打开 http://127.0.0.1:8777
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import g, load_config, path_of       # noqa: E402
from src.logutil import get_logger                    # noqa: E402
from src.runner import Runner                         # noqa: E402
from src.storage import Storage                       # noqa: E402
from src.pipeline.stats import build_report           # noqa: E402

WEB_DIR = Path(__file__).resolve().parent / "web"
app = None  # type: ignore
_STATE = {"running": False, "last_msg": ""}


def build_app(cfg: dict, log):
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import FileResponse, JSONResponse

    store = Storage(path_of(cfg, "project.db_path", "data/demand.db"))
    api = FastAPI(title="AI 需求雷达", version="0.1.0")

    @api.get("/")
    def index():
        return FileResponse(str(WEB_DIR / "index.html"))

    @api.get("/api/stats")
    def stats(days: int = Query(30, ge=0, le=36500), limit: int = Query(40, ge=5, le=200)):
        rep = build_report(store, days=days, top_limit=limit)
        rep["state"] = dict(_STATE)
        return rep

    @api.get("/api/cluster/{cid}")
    def cluster_detail(cid: int, limit: int = Query(50, ge=1, le=300)):
        rows = store.cluster_evidence(cid, limit)
        if not rows:
            raise HTTPException(404, "no evidence")
        return {"cluster_id": cid, "canonical": rows[0]["demand"],
                "count": len(rows), "items": rows}

    @api.post("/api/run")
    def trigger_run(consolidate: bool = False):
        if _STATE["running"]:
            return JSONResponse({"ok": False, "msg": "上一轮还没跑完"}, status_code=409)

        def work():
            _STATE["running"] = True
            _STATE["last_msg"] = "运行中…"
            try:
                runner = Runner(cfg, log=log)
                s = runner.run_round(do_consolidate=consolidate)
                _STATE["last_msg"] = (f"第{s['run_id']}轮完成：新增 {s['new']} 条 / "
                                      f"需求 {s['needs']} 条")
            except Exception as e:
                _STATE["last_msg"] = f"失败：{type(e).__name__} {e}"
            finally:
                _STATE["running"] = False

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "msg": "已在后台开跑"}

    @api.get("/api/report.md")
    def report_md(days: int = Query(30, ge=0, le=36500)):
        rep = build_report(store, days=days)
        ov = rep["overview"]
        lines = [f"# AI 需求雷达 · 痛点排行（{('全部时间' if days<=0 else '近%u天' % days)}）", "",
                 f"原始评论 {ov['raw_total']}　需求 {ov['needs_total']}　"
                 f"独立需求 {ov['clusters_total']}", "", "| # | 需求 | 次数 | 痛感 | 趋势 |",
                 "|---|------|------|------|------|"]
        for i, r in enumerate(rep["top_demands"][:30], 1):
            lines.append(f"| {i} | {r['canonical']} | {r['count']} | {r['avg_pain']} | "
                         f"{r['growth_pct']:+.0f}% |")
        return JSONResponse({"markdown": "\n".join(lines)})

    @api.get("/api/top")
    def top(days: int = Query(30), limit: int = Query(20)):
        return {"top": store.top_demands(days=days, limit=limit)}

    return api


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 需求雷达 · 看板服务")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    log = get_logger("server", path_of(cfg, "project.log_dir", "logs"))
    app = build_app(cfg, log)

    import uvicorn
    host = args.host or g(cfg, "dashboard.host", "127.0.0.1")
    port = args.port or g(cfg, "dashboard.port", 8777)
    print(f"\n  看板地址： http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
