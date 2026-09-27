# -*- coding: utf-8 -*-
"""手动跑一轮。调试、换关键词、立刻看结果都用它。

用法：
    python run_once.py
    python run_once.py --keywords "AI工具,AI写作"          指定关键词
    python run_once.py --consolidate                        跑完做一次需求归并
    python run_once.py --source-only bilibili               只跑某一个采集器（调试用）
    python run_once.py --extract-only                       不采集，只重跑库里的积压评论（换模型/改提示词后用它）
    python run_once.py --dry-run                            只采集不抽取，看看能抓到什么
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config, path_of        # noqa: E402
from src.logutil import get_logger                  # noqa: E402
from src.runner import Runner                       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 需求雷达 · 单轮跑批")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--keywords", default="", help="逗号分隔，覆盖配置文件里的关键词")
    ap.add_argument("--consolidate", action="store_true", help="跑完做一次大模型需求归并")
    ap.add_argument("--source-only", default="", help="只跑指定采集器")
    ap.add_argument("--extract-only", action="store_true",
                    help="不联网采集，只把库里积压的未处理评论跑一遍抽取。"
                         "换了模型或改了提示词后重跑历史数据用这个")
    ap.add_argument("--dry-run", action="store_true", help="只采集入库，不做抽取")
    ap.add_argument("--set", dest="sets", action="append", default=[],
                    help="临时覆盖配置，如 --set sources.bilibili.videos_per_keyword=3")
    args = ap.parse_args()

    cfg = load_config(args.config)
    for expr in args.sets:
        if "=" not in expr:
            print(f"--set 格式应为 a.b.c=值，收到：{expr}")
            continue
        dotted, raw = expr.split("=", 1)
        parts = dotted.split(".")
        node = cfg
        for p in parts[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                raise SystemExit(f"--set 路径冲突：{dotted}")
        val = raw.strip()
        if val.lower() in ("true", "false"):
            node[parts[-1]] = (val.lower() == "true")
        else:
            try:
                node[parts[-1]] = int(val)
            except ValueError:
                node[parts[-1]] = val
        print(f"覆盖配置 {dotted} = {node[parts[-1]]}")
    log = get_logger("radar", path_of(cfg, "project.log_dir", "logs"))

    if args.source_only:
        for name in cfg.get("sources", {}):
            if isinstance(cfg["sources"].get(name), dict):
                cfg["sources"][name]["enabled"] = (name == args.source_only)
    if args.extract_only:
        for name in cfg.get("sources", {}):
            if isinstance(cfg["sources"].get(name), dict):
                cfg["sources"][name]["enabled"] = False
        print("仅抽取模式：跳过全部采集，处理库里积压的评论")
    if args.dry_run:
        cfg["llm"]["enabled"] = False
        cfg["llm"]["fallback_to_rules"] = False

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    from src.config import api_key
    if cfg.get("llm", {}).get("enabled", True) and not api_key(cfg):
        log.warning("未检测到大模型 Key：本次将用规则兜底抽取。"
                    "想用 DeepSeek，请把 key 填进 config/config.yaml 的 llm.api_key，"
                    "或设置环境变量 %s。", cfg.get("llm", {}).get("api_key_env", "DEEPSEEK_API_KEY"))

    runner = Runner(cfg, log=log)
    summary = runner.run_round(keywords=keywords or None, do_consolidate=args.consolidate)

    print("\n---------- 本轮结果 ----------")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n报表已生成：{Path(runner.export_dir) / 'latest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
