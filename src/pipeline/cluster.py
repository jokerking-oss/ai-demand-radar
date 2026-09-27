# -*- coding: utf-8 -*-
"""需求归一化与聚类：把「说同一件事的不同说法」算成同一个需求。

两道工艺：
1. 规则归一化（去前缀、去标点、切词粒度统一）+ 二元组 Jaccard 相似度，落到已有簇；
2. 可选 LLM 归并：把簇代表句交给大模型，把"会员太贵/买不起/价格高"统一成一句标准短语。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from ..clean import clean_text

PUNCT = re.compile(r"[\s，。、；！？,.!?;:'\"“”‘’（）()\[\]【】《》~～\-—_/\\|]+")
NUM = re.compile(r"\d+")
STRIP_PREFIX = [
    "希望", "想要", "想", "要是", "如果", "能不能", "能否", "可以", "请", "需要", "请帮我", "求",
    "建议", "最好", "有没有", "为什么", "为啥", "我希望能", "我想要的",
]
STRIP_SUFFIX = ["的问题", "问题", "需求", "这个", "方面", "功能", "的情况"]


def normalize(demand: str) -> str:
    d = clean_text(demand or "").lower()
    d = PUNCT.sub("", d)
    d = NUM.sub("", d)
    for p in sorted(STRIP_PREFIX, key=len, reverse=True):
        if d.startswith(p) and len(d) - len(p) >= 3:
            d = d[len(p):]
    for s in STRIP_SUFFIX:
        if d.endswith(s) and len(d) - len(s) >= 3:
            d = d[:-len(s)]
    return d[:40]


def bigrams(text: str) -> set[str]:
    t = normalize(text)
    if len(t) < 2:
        return {t}
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


class ClusterIndex:
    def __init__(self, store, threshold: float = 0.46):
        self.store = store
        self.threshold = threshold
        self.rows = store.all_clusters()
        self.sigs: dict[int, set[str]] = {}
        for r in self.rows:
            learned = set()
            try:
                learned = set(json.loads(r["keywords"] or "[]"))
            except Exception:
                learned = set()
            learned |= bigrams(r["canonical"])
            self.sigs[int(r["id"])] = learned

    def match(self, demand: str) -> int | None:
        sig = bigrams(demand)
        best_id, best = None, 0.0
        for cid, csig in self.sigs.items():
            s = similarity(sig, csig)
            if s > best:
                best, best_id = s, cid
        if best_id is not None and best >= self.threshold:
            # 把这个新说法的词也并入该簇签名，下次更准
            self.sigs[best_id] = self.sigs[best_id] | sig
            return best_id
        return None

    def create(self, canonical: str, quote: str = "") -> int:
        cid = self.store.upsert_cluster(canonical, example_quote=quote)
        self.sigs[cid] = bigrams(canonical)
        return cid


def assign_clusters(store, threshold: float = 0.46, log=print) -> int:
    pending = store.unassigned_needs(limit=20000)
    if not pending:
        return 0
    idx = ClusterIndex(store, threshold)
    done = 0
    for need in pending:
        cid = idx.match(need["demand"])
        if cid is None:
            cid = idx.create(need["demand"][:40], quote=need["quote"])
        store.assign_cluster(int(need["id"]), cid)
        store.touch_cluster_quote(cid, need["quote"] or "")
        done += 1
    log(f"  [聚类] {done} 条需求已归入 {len(idx.sigs)} 个需求簇")
    return done


def pairwise_merge(store, llm, topk: int = 5, min_sim: float = 0.10,
                   max_pairs: int = 400, batch: int = 20, log=print) -> int:
    """夹一道「成对比对」：让大模型直接判断两句话是不是同一个需求。

    为什么要这一步：让模型各自给大批句子起标签，同一个需求往往被起成不同的名字，
    比如「长篇创作保持角色人设不跑偏」和「长篇创作跨章节记忆设定」——
    这俩的字符串相似度只有 0.16，靠相似度永远合不到一起，只有让模型读出来。

    做法：先用便宜的字符串相似度挑出候选配对（不做全量 n² 比对），
    再把这些配对交给模型二选一。一轮能合掉多少，决定了排行能不能看。
    """
    if not llm.available:
        return 0
    rows = store.all_clusters()
    if len(rows) < 2:
        return 0
    ids = [int(r["id"]) for r in rows]
    texts = [r["canonical"] for r in rows]
    sigs = [bigrams(t) for t in texts]

    # 候选配对：每个簇取最像自己的 topk 个邻居
    pairs: set[tuple[int, int]] = set()
    for i in range(len(ids)):
        scored = []
        for j in range(len(ids)):
            if i == j:
                continue
            s = similarity(sigs[i], sigs[j])
            if s >= min_sim:
                scored.append((s, j))
        scored.sort(reverse=True)
        for _, j in scored[:topk]:
            a, b = sorted((i, j))
            pairs.add((a, b))
    plist = sorted(pairs)[:max_pairs]
    if not plist:
        return 0

    verdicts: list[bool] = []
    for start in range(0, len(plist), batch):
        chunk = plist[start:start + batch]
        payload = "\n".join(
            f"{n}. A：{texts[a]}\n   B：{texts[b]}" for n, (a, b) in enumerate(chunk))
        sys = (
            "判断下面每一组里的 A 和 B 是不是**同一个需求**（用户想要的其实是同一件事）。\n"
            "只看语义是否等价，不要因为措辞不同就判否；也不要因为都属于同一个大方向就判是。\n"
            f"严格只输出 {len(chunk)} 行，每行「序号=是」或「序号=否」，不要任何解释。"
        )
        try:
            raw = llm._chat([{"role": "system", "content": sys},
                             {"role": "user", "content": payload}],
                            max_tokens=max(300, len(chunk) * 20))
            got = {int(k): v for k, v in re.findall(r"(\d+)\s*[=＝:：]\s*([是否是否yesnoYESNO]+)", raw)}
            for n in range(len(chunk)):
                verdicts.append(str(got.get(n, "")).startswith(("是", "y", "Y")))
        except Exception as e:
            log(f"  [归并] 成对比对批次失败: {type(e).__name__} {str(e)[:60]}")
            verdicts.extend([False] * len(chunk))

    # 并查集合并判定为同一需求的所有簇
    parent = {cid: cid for cid in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    with store._lock:
        cnt_rows = store.conn.execute(
            "SELECT cluster_id, COUNT(*) c FROM needs WHERE cluster_id IS NOT NULL GROUP BY cluster_id").fetchall()
    counts = {int(r["cluster_id"]): r["c"] for r in cnt_rows}

    for (a, b), yes in zip(plist, verdicts):
        if yes:
            union(ids[a], ids[b])

    groups: dict[int, list[int]] = {}
    for cid in ids:
        groups.setdefault(find(cid), []).append(cid)

    merged = 0
    with store._lock:
        for members in groups.values():
            if len(members) < 2:
                continue
            # 主簇取需求条数最多的那个
            members_sorted = sorted(members, key=lambda c: (-counts.get(c, 0), c))
            master = members_sorted[0]
            label = texts[ids.index(master)]
            store.conn.execute("UPDATE clusters SET canonical=?, updated_at=? WHERE id=?",
                               (label[:80], datetime.now().isoformat(timespec="seconds"), master))
            for other in members_sorted[1:]:
                store.conn.execute("UPDATE needs SET cluster_id=? WHERE cluster_id=?", (master, other))
                store.conn.execute("DELETE FROM clusters WHERE id=?", (other,))
            merged += len(members) - 1
        store.conn.commit()
    log(f"  [归并] 成对比对 {len(plist)} 组，合并掉 {merged} 个重复簇")
    return merged


def consolidate_with_llm(store, llm, min_count: int = 2, batch: int = 30, passes: int = 3, log=print) -> int:
    """把表达同一件事的多个簇合并成同一个标准短语。

    关键点：必须先把**全部**簇短语一次性交给大模型拿到统一标签，再做全局合并。
    如果分批做，不同批次各自造出来的标签互不相同，跨批次的重复就永远合不掉——
    那样每个需求都只剩 1 条，排行就没有意义了。
    """
    if not llm.available:
        log("  [归并] 无可用大模型，跳过")
        return 0
    # 多跑几轮：每轮重新打标签，上一轮没撞上的跨批次重复在下一轮有机会合到一起。
    # 单轮只能合掉同批次内的重复，轮数不够收敛不到。
    total = 0
    for _round in range(1, passes + 1):
        clusters = store.all_clusters()
        if len(clusters) < 2:
            break
        phrases = [c["canonical"] for c in clusters]
        ids = [int(c["id"]) for c in clusters]

        with store._lock:
            cnt_rows = store.conn.execute(
                "SELECT cluster_id, COUNT(*) c FROM needs WHERE cluster_id IS NOT NULL GROUP BY cluster_id").fetchall()
        counts = {int(r["cluster_id"]): r["c"] for r in cnt_rows}

        canonicals: list[str] = []
        for start in range(0, len(phrases), batch):
            canonicals.extend(llm.canonicalize(phrases[start:start + batch]))

        # 全局归组：同一标签的所有簇合并到一个主簇。跨批次也能合到一起。
        groups: dict[str, list[int]] = {}
        labels: dict[int, str] = {}
        for cid, new in zip(ids, canonicals):
            if not new:
                continue
            labels[cid] = new
            groups.setdefault(normalize(new) or new[:20], []).append(cid)

        merged = 0
        with store._lock:
            for key, cids in groups.items():
                if len(cids) < 2:
                    continue
                cids_sorted = sorted(cids, key=lambda c: (-counts.get(c, 0), c))
                master = cids_sorted[0]
                label = labels[master]
                store.conn.execute("UPDATE clusters SET canonical=?, updated_at=? WHERE id=?",
                                   (label[:80], datetime.now().isoformat(timespec="seconds"), master))
                for other in cids_sorted[1:]:
                    store.conn.execute("UPDATE needs SET cluster_id=? WHERE cluster_id=?", (master, other))
                    store.conn.execute("DELETE FROM clusters WHERE id=?", (other,))
                merged += len(cids) - 1
            store.conn.commit()
        total += merged
        log(f"  [归并] 第{_round}轮：{len(ids)} 个簇 → {len(groups)} 个独立需求（合并 {merged}）")
        if merged == 0:
            break
    if total:
        log(f"  [归并] 累计合并掉 {total} 个重复簇")
    return total
