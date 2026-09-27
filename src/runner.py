# -*- coding: utf-8 -*-
"""一轮完整跑批：采集 -> 入库 -> 筛痛点评候选 -> 大模型抽取 -> 聚类 -> 出报表。"""
from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

from .config import g, path_of
from .http import Fetcher
from .clean import content_fingerprint
from .storage import Storage
from .sources import REGISTRY
from .llm import LLMClient, rule_extract
from .pipeline.cluster import assign_clusters, consolidate_with_llm, pairwise_merge
from .pipeline.stats import build_report, write_markdown


class Runner:
    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        self.store = Storage(path_of(cfg, "project.db_path", "data/demand.db"))
        self.llm = LLMClient(cfg, log=log)
        self.export_dir = path_of(cfg, "project.export_dir", "data/export")

    # --------- 关键词轮播：不用每轮全量，省请求量 ---------
    def pick_keywords(self) -> list[str]:
        all_kw = (self.cfg.get("topic", {}) or {}).get("keywords", []) or []
        limit = int(g(self.cfg, "schedule.per_round_max_keywords", 6))
        if not all_kw:
            return []
        limit = max(1, min(limit, len(all_kw)))
        return random.sample(all_kw, limit)

    # --------- 抽取候选闸门 ---------
    # 注意这里的设计取向：**默认不做痛点词筛选**。
    # 显性抱怨（"太难用/太贵"）是所有人都能看到的红海信号；
    # 真正有价值的是用户顺口说出来、自己都没意识到是痛的那些平常话。
    # 所以闸门只负责剔除垃圾（广告/灌水/无语义），剩下的全交给大模型去读懂。
    def _is_candidate(self, row) -> bool:
        import re as _re
        gates = self.cfg.get("gates", {}) or {}
        topic = self.cfg.get("topic", {}) or {}

        body = (row["content"] or "").strip()
        text = f"{row['title'] or ''} {body}"

        # --- 1. 垃圾过滤，任何模式都生效 ---
        for bad in topic.get("exclude", []) or []:
            if bad in text:
                return False
        if len(body) < int(gates.get("min_chars", 8)) or len(body) > int(gates.get("max_chars", 800)):
            return False
        # 无语义：去掉标点空白后字符种类过少（"哈哈哈"、"6666"这种）
        if len(set(_re.sub(r"[\s\W_]+", "", body))) < int(gates.get("min_unique_chars", 4)):
            return False
        if gates.get("skip_if_no_hanzi", True) and not _re.search(r"[\u4e00-\u9fff]", body):
            return False

        # --- 2. 按模式决定要不要进一步卡 ---
        mode = gates.get("mode", "all_relevant")

        if mode == "hints_only":
            # 省钱模式：只送命中显性痛点词的。会漏掉隐性需求，**不是推荐模式**
            return any(h in text for h in (topic.get("pain_hints", []) or []))

        if mode == "topic_relevant":
            # 送所有跟主题沾边的发言
            kws = topic.get("relevance_keywords", []) or []
            if not kws:
                return True
            return any(k in text for k in kws)

        return True   # all_relevant：采集本身就是按主题搜的，全部送审

    def run_round(self, keywords: list[str] | None = None, do_consolidate: bool = False,
                  force: bool = False) -> dict:
        started = datetime.now()
        keywords = keywords or self.pick_keywords()
        run_id = self.store.start_run()
        summary = {"run_id": run_id, "keywords": keywords, "fetched": 0, "new": 0,
                   "candidates": 0, "needs": 0, "llm_calls": 0, "extractor": "none"}

        self.log(f"===== 第 {run_id} 轮开始 · 关键词：{', '.join(keywords) or '（无）'} =====")

        # ---------- 1. 采集 ----------
        # REGISTRY 的顺序经过安排：search_engine 在前边跑，把发现的 URL
        # 交给后边的 generic_page 去取正文。
        fetcher = Fetcher(self.cfg, log=self.log)
        discovered_urls: list[str] = []
        generic_page: object | None = None
        items: list = []

        for name, cls in REGISTRY.items():
            if not (self.cfg.get("sources", {}) or {}).get(name, {}).get("enabled", True):
                continue
            try:
                src = cls(self.cfg, fetcher, log=self.log)
                if name == "generic_page":
                    src.extra_urls = list(discovered_urls)
                    generic_page = src
                raw_items = src.collect(keywords)
                items.extend(raw_items)
                if name == "search_engine" and getattr(src, "discovered", None):
                    discovered_urls = list(dict.fromkeys(src.discovered))[:20]
            except Exception as e:
                self.log(f"  [异常] 采集器 {name} 出错：{type(e).__name__} {str(e)[:120]}")

        # ---------- 2. 入库 ----------
        records, seen = [], set()
        for it in items:
            rec = it.to_record(redact=g(self.cfg, "compliance.redact_pii", True))
            fp = rec["content_fp"]
            key = (rec["source"], fp)
            if fp and key in seen:  # 同一平台内的重复文本（刷屏/复读）只留一条
                continue
            seen.add(key)
            records.append(rec)

        cap = int(g(self.cfg, "schedule.per_round_max_items", 300))
        new_count, new_rows = self.store.insert_items(records[:cap])
        summary.update(fetched=len(records), new=new_count)
        self.log(f"  采集 {len(records)} 条，去重后新入库 {new_count} 条")

        # ---------- 3. 抽取 ----------
        pool = [dict(r) for r in (new_rows or self.store.fetch_unprocessed(cap))]
        candidates = [dict(r) for r in pool if self._is_candidate(r)]
        summary["candidates"] = len(candidates)
        self.log(f"  候选评论 {len(candidates)} 条（闸门放行，全部交给大模型做深度解读）")

        needs: list[dict] = []
        processed_ids = [r["id"] for r in pool]

        use_llm = bool(g(self.cfg, "llm.enabled", True)) and self.llm.available
        if candidates and use_llm:
            feed = [{"index": i, "text": f"{c['title'] or ''}\n{c['content']}"[:1200],
                     "platform": c["source"], "keyword": c["keyword"]}
                    for i, c in enumerate(candidates)]
            res = self.llm.extract_many(feed)
            summary["llm_calls"] = self.llm.calls
            summary["tokens_in"] = self.llm.tokens_in
            summary["tokens_out"] = self.llm.tokens_out
            hit_ids = set()
            for i, ds in res.items():
                if i >= len(candidates):
                    continue
                c = candidates[i]
                hit_ids.add(i)
                for d in ds:
                    needs.append({**d, "raw_id": c["id"], "day": c["day"],
                                  "extractor": "llm"})
            summary["extractor"] = "llm"
            # 成功送进大模型的才标记已处理，超预算的留到下一轮
            processed_ids = [candidates[i]["id"] for i in range(len(candidates)) if i in hit_ids]
            # 没命中的（判定为非痛点）也要标记处理，避免反复筛
            processed_ids += [candidates[i]["id"] for i in range(len(candidates))
                              if i < len(candidates) and i in hit_ids]
            processed_ids = list(dict.fromkeys(processed_ids))

        elif candidates and g(self.cfg, "llm.fallback_to_rules", True):
            hints = (self.cfg.get("topic", {}) or {}).get("pain_hints", []) or []
            for c in candidates:
                for d in rule_extract(c["content"], hints):
                    needs.append({**d, "raw_id": c["id"], "day": c["day"],
                                  "extractor": "rule"})
            summary["extractor"] = "rule"
            processed_ids = [c["id"] for c in candidates]
        else:
            if candidates and not self.llm.available:
                self.log("  [提示] 未配置大模型 Key，且规则兜底关闭，跳过抽取")

        if needs:
            summary["implicit"] = sum(1 for n in needs if int(n.get("implicit", 0) or 0) == 1)
            added = self.store.insert_needs(needs)
            summary["needs"] = added
            self.log(f"  抽出需求 {added} 条（{summary['extractor']}）")
            self.store.mark_processed(processed_ids)
        elif candidates:
            self.store.mark_processed(processed_ids)

        # ---------- 4. 聚类 ----------
        if summary["needs"] or do_consolidate:
            assign_clusters(self.store, log=self.log)
            if do_consolidate:
                consolidate_with_llm(self.store, self.llm, log=self.log)
                # 标签改写合不掉「说法不同但语义相同」的簇，再补一道成对比对
                pairwise_merge(self.store, self.llm, log=self.log)

        # ---------- 5. 出报表 ----------
        days = int(g(self.cfg, "dashboard.days", 30))
        report = build_report(self.store, days=days)
        summary["top"] = [{"cluster_id": r["cluster_id"], "canonical": r["canonical"],
                           "count": r["count"], "avg_pain": r["avg_pain"]}
                          for r in report["top_demands"][:10]]
        self.export_dir.mkdir(parents=True, exist_ok=True)
        (self.export_dir / "latest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        write_markdown(report, self.export_dir / f"report_{datetime.now():%Y%m%d}.md")

        if summary["extractor"] == "llm":
            tk_in = summary.get("tokens_in", 0)
            tk_out = summary.get("tokens_out", 0)
            # 单价可在 config.llm.price_in / price_out 配（单位：¥ / 百万 tokens）。
            # 第三方网关的实际单价通常和官方不一致，这里只是个可校准的估算。
            p_in = float(g(self.cfg, "llm.price_in", 2) or 2)
            p_out = float(g(self.cfg, "llm.price_out", 8) or 8)
            cost = tk_in / 1_000_000 * p_in + tk_out / 1_000_000 * p_out
            summary["cost_rmb"] = round(cost, 3)
            self.log(f"  大模型调用 {summary['llm_calls']} 次 · "
                     f"输入 {tk_in} tokens / 输出 {tk_out} tokens · 本轮约 ¥{cost:.2f}")
            self.log(f"  其中挖出来的隐性需求 {summary['implicit']} 条"
                     f"（总需求 {summary['needs']} 条）")

        cost = (datetime.now() - started).total_seconds()
        summary["seconds"] = round(cost, 1)
        self.store.finish_run(run_id, status="ok", items_fetched=summary["fetched"],
                             items_new=summary["new"], needs_new=summary["needs"],
                             llm_calls=summary["llm_calls"],
                             note=json.dumps({"extractor": summary["extractor"],
                                              "keywords": keywords,
                                              "tokens_in": summary.get("tokens_in", 0),
                                              "tokens_out": summary.get("tokens_out", 0),
                                              "cost_rmb": summary.get("cost_rmb", 0)},
                                             ensure_ascii=False))
        self.log(f"===== 第 {run_id} 轮结束 · 耗时 {cost:.0f}s · "
                 f"新增 {summary['new']} 条 / 需求 {summary['needs']} 条 =====")
        return summary
