# -*- coding: utf-8 -*-
"""统计汇总：给看板提供数据。"""
from __future__ import annotations

from datetime import datetime, timedelta


def build_report(store, days: int = 30, top_limit: int = 40) -> dict:
    overview = store.overview(days)
    top = store.top_demands(days=days, limit=top_limit)
    trend = store.demand_trend([int(r["cluster_id"]) for r in top[:12]], days=days)

    rows = []
    for r in top:
        cid = int(r["cluster_id"])
        series = trend.get(cid, [])
        total = sum(p["count"] for p in series)
        half = max(1, len(series) // 2)
        recent = sum(p["count"] for p in series[-half:])
        older = sum(p["count"] for p in series[:half])
        growth = ((recent - older) / older * 100) if older else (100.0 if recent else 0.0)
        cnt = int(r["cnt"])
        imp = int(r.get("implicit_cnt") or 0)
        rows.append({
            "cluster_id": cid,
            "canonical": r["canonical"],
            "count": cnt,
            "implicit_cnt": imp,
            "implicit_ratio": round(imp / cnt * 100) if cnt else 0,
            "distinct_days": len(series),
            "avg_pain": round(float(r["avg_pain"] or 0), 2),
            "source_count": r["src_cnt"],
            "sources": [s for s in (r["sources"] or "").split(",") if s],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "growth_pct": round(growth, 1),
            "trend": series,
            "example_quote": r["example_quote"] or "",
        })

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "overview": overview,
        "top_demands": rows,
        "daily": store.daily_counts(days),
        "sources": store.source_distribution(days),
        "pain_hist": store.pain_distribution(days),
        "keyword_hits": store.keyword_hits(days),
        "top_products": store.top_products(days),
    }


def write_markdown(report: dict, path) -> str:
    """每日/每轮可读报告，方便直接丢进微信或文档里看。"""
    days = report["days"]
    ov = report["overview"]
    window = "全部时间" if days <= 0 else f"近 {days} 天"
    lines = [
        "# AI 需求雷达 · 痛点排行",
        "",
        f"生成时间：{report['generated_at']}　统计窗口：{window}",
        "",
        f"- 累计原始评论：**{ov['raw_total']}** 条（{window}内 {ov.get(f'raw_last_{days}d', 0)} 条）",
        f"- 抽出需求：**{ov['needs_total']}** 条"
        f"（其中 **{ov.get('insight_implicit', 0)}** 条是从平常话里挖出来的隐性需求，"
        f"占 {round(ov.get('insight_implicit', 0) / max(1, ov['needs_total']) * 100)}%）",
        f"- 归并后的独立需求：**{ov['clusters_total']}** 个",
        "",
        "## Top 需求（被提到次数最多的痛点）",
        "",
        "| # | 需求 | 次数 | 隐性占比 | 痛感 | 来源数 | 近期趋势 | 最近出现 |",
        "|---|------|------|----------|------|--------|----------|----------|",
    ]
    for i, row in enumerate(report["top_demands"][:30], 1):
        trend_txt = f"{row['growth_pct']:+.0f}%"
        lines.append(
            f"| {i} | {row['canonical']} | {row['count']} | {row.get('implicit_ratio', 0)}% | "
            f"{row['avg_pain']} | "
            f"{row['source_count']} | {trend_txt} | {row['last_seen']} |")

    lines += ["", "> 隐性占比 = 这条需求有多大比例是「AI 从用户没抱怨的话里分析出来的」，"
                   "而不是用户自己直接说出来。这个数越高，说明它越可能是别人还在忽视的机会。", ""]

    lines += ["", "## 代表性原话", ""]
    for row in report["top_demands"][:15]:
        if row["example_quote"]:
            lines.append(f"- **{row['canonical']}** — “{row['example_quote'][:120]}”")

    if report.get("top_products"):
        lines += ["", "## 被点名的产品", ""]
        for p in report["top_products"][:15]:
            lines.append(f"- {p['product']}：{p['c']} 次，平均痛感 {p['avg_pain']:.1f}")

    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
