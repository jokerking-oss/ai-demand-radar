# -*- coding: utf-8 -*-
"""SQLite 存储层。单文件数据库，零运维。

四张表：
  raw_items 原始评论（脱敏后）
  needs     从每条评论里抽出来的需求
  clusters  需求聚类（同一个需求的不同说法归到一起）
  runs      每轮采集的运行记录
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_items (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    source_id     TEXT,
    url           TEXT,
    title         TEXT,
    content       TEXT NOT NULL,
    content_fp    TEXT,
    author_hash   TEXT,
    published_at  TEXT,
    day           TEXT,
    keyword       TEXT,
    meta          TEXT,
    collected_at  TEXT,
    run_id        INTEGER,
    processed     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_raw_processed ON raw_items(processed);
CREATE INDEX IF NOT EXISTS ix_raw_day       ON raw_items(day);
CREATE INDEX IF NOT EXISTS ix_raw_source    ON raw_items(source);
CREATE INDEX IF NOT EXISTS ix_raw_fp        ON raw_items(content_fp);

CREATE TABLE IF NOT EXISTS needs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id       TEXT NOT NULL,
    day          TEXT,
    demand       TEXT NOT NULL,
    demand_norm  TEXT,
    cluster_id   INTEGER,
    category     TEXT,
    scene        TEXT,
    pain_score   INTEGER DEFAULT 3,
    product      TEXT,
    quote        TEXT,
    extractor    TEXT,
    implicit     INTEGER DEFAULT 0,   -- 0=用户自己说出来的  1=从话里分析出来的（隐性需求）
    reasoning    TEXT,                -- 为什么要这么推：给隐性需求留证据，防止瞎编
    created_at   TEXT,
    UNIQUE(raw_id, demand)
);
CREATE INDEX IF NOT EXISTS ix_need_cluster ON needs(cluster_id);
CREATE INDEX IF NOT EXISTS ix_need_day     ON needs(day);

CREATE TABLE IF NOT EXISTS clusters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical   TEXT UNIQUE,
    keywords    TEXT,
    description TEXT,
    example_quote TEXT,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT,
    finished_at  TEXT,
    status       TEXT,
    items_fetched INTEGER DEFAULT 0,
    items_new     INTEGER DEFAULT 0,
    needs_new     INTEGER DEFAULT 0,
    llm_calls     INTEGER DEFAULT 0,
    note          TEXT
);
"""


def _since(days: int) -> str:
    """统计窗口下限。days<=0 表示不限时间。

    为什么要支持「不限时间」：库里的 day 字段是**原帖发布时间**，
    全网抓来的帖子跨度可能有十几年。用「近30天」看，首次跑起来会漏掉一大半数据。
    """
    if days is None or days <= 0:
        return "0000-01-01"
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """给老库补列，不动已有数据。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(needs)")}
        for col, ddl in (("implicit", "ALTER TABLE needs ADD COLUMN implicit INTEGER DEFAULT 0"),
                         ("reasoning", "ALTER TABLE needs ADD COLUMN reasoning TEXT")):
            if col not in cols:
                try:
                    self.conn.execute(ddl)
                except Exception:
                    pass

    # ---------------- runs ----------------
    def start_run(self) -> int:
        with self._lock:
            cur = self.conn.execute("INSERT INTO runs(started_at,status) VALUES(?,?)", (_now(), "running"))
            self.conn.commit()
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, **fields) -> None:
        fields.setdefault("status", "ok")
        fields.setdefault("finished_at", _now())
        cols = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))
            self.conn.commit()

    # ---------------- raw_items ----------------
    def insert_items(self, items: Iterable[dict]) -> tuple[int, list[dict]]:
        """返回 (新增条数, 新增明细)。已存在的自动忽略。"""
        new_count, new_rows = 0, []
        with self._lock:
            for it in items:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO raw_items "
                    "(id,source,source_id,url,title,content,content_fp,author_hash,"
                    " published_at,day,keyword,meta,collected_at,processed) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (it["id"], it["source"], it.get("source_id", ""), it.get("url", ""),
                     it.get("title", ""), it["content"], it.get("content_fp", ""),
                     it.get("author_hash", "anon"), it.get("published_at", ""),
                     it.get("day", datetime.now().strftime("%Y-%m-%d")), it.get("keyword", ""),
                     json.dumps(it.get("meta", {}), ensure_ascii=False), _now()),
                )
                if cur.rowcount > 0:
                    new_count += 1
                    new_rows.append(it)
            self.conn.commit()
        return new_count, new_rows

    def fetch_unprocessed(self, limit: int = 300) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(
                "SELECT * FROM raw_items WHERE processed=0 ORDER BY collected_at DESC LIMIT ?", (limit,)))

    def mark_processed(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock:
            self.conn.executemany("UPDATE raw_items SET processed=1 WHERE id=?", [(i,) for i in ids])
            self.conn.commit()

    # ---------------- needs ----------------
    def insert_needs(self, needs: list[dict]) -> int:
        if not needs:
            return 0
        added = 0
        with self._lock:
            for n in needs:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO needs "
                    "(raw_id,day,demand,demand_norm,cluster_id,category,scene,pain_score,"
                    " product,quote,extractor,implicit,reasoning,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (n["raw_id"], n.get("day", datetime.now().strftime("%Y-%m-%d")),
                     n["demand"], n.get("demand_norm", ""), n.get("cluster_id"),
                     n.get("category", ""), n.get("scene", ""), int(n.get("pain_score", 3) or 3),
                     n.get("product", ""), n.get("quote", ""), n.get("extractor", "llm"),
                     int(n.get("implicit", 0) or 0), n.get("reasoning", "") or "", _now()),
                )
                added += cur.rowcount
            self.conn.commit()
        return added

    def unassigned_needs(self, limit: int = 5000) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(
                "SELECT * FROM needs WHERE cluster_id IS NULL ORDER BY id LIMIT ?", (limit,)))

    def assign_cluster(self, need_id: int, cluster_id: int) -> None:
        with self._lock:
            self.conn.execute("UPDATE needs SET cluster_id=? WHERE id=?", (cluster_id, need_id))
            self.conn.commit()

    # ---------------- clusters ----------------
    def all_clusters(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute("SELECT * FROM clusters ORDER BY id"))

    def upsert_cluster(self, canonical: str, keywords: str = "", example_quote: str = "") -> int:
        with self._lock:
            row = self.conn.execute("SELECT id FROM clusters WHERE canonical=?", (canonical,)).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE clusters SET updated_at=?,  keywords=CASE WHEN keywords IS NULL OR keywords='' "
                    "THEN ? ELSE keywords END WHERE id=?", (_now(), keywords, row["id"]))
                self.conn.commit()
                return int(row["id"])
            cur = self.conn.execute(
                "INSERT INTO clusters(canonical,keywords,example_quote,created_at,updated_at) VALUES(?,?,?,?,?)",
                (canonical, keywords, example_quote, _now(), _now()))
            self.conn.commit()
            return int(cur.lastrowid)

    def touch_cluster_quote(self, cluster_id: int, quote: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE clusters SET example_quote=? WHERE id=? AND (example_quote IS NULL OR example_quote='')",
                (quote[:200], cluster_id))
            self.conn.commit()

    # ---------------- 统计 ----------------
    def overview(self, days: int = 30) -> dict:
        since = _since(days)
        with self._lock:
            raw_total = self.conn.execute("SELECT COUNT(*) c FROM raw_items").fetchone()["c"]
            raw_win = self.conn.execute(
                "SELECT COUNT(*) c FROM raw_items WHERE day>=?", (since,)).fetchone()["c"]
            needs_total = self.conn.execute("SELECT COUNT(*) c FROM needs").fetchone()["c"]
            clusters_total = self.conn.execute("SELECT COUNT(*) c FROM clusters").fetchone()["c"]
            implicit_c = self.conn.execute(
                "SELECT COUNT(*) c FROM needs WHERE COALESCE(implicit,0)=1").fetchone()["c"]
            last = self.conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "raw_total": raw_total,
            f"raw_last_{days}d": raw_win,
            "needs_total": needs_total,
            "insight_implicit": implicit_c,          # 从平常话里挖出来的
            "insight_explicit": needs_total - implicit_c,  # 用户直接说出来的
            "clusters_total": clusters_total,
            "last_run": dict(last) if last else None,
        }

    def top_demands(self, days: int = 30, limit: int = 40, source: str | None = None) -> list[dict]:
        since = _since(days)
        sql = """
        SELECT c.id AS cluster_id, c.canonical, c.example_quote,
               COUNT(*) AS cnt,
               AVG(n.pain_score) AS avg_pain,
               MAX(n.day) AS last_seen,
               MIN(n.day) AS first_seen,
               COUNT(DISTINCT r.source) AS src_cnt,
               SUM(CASE WHEN COALESCE(n.implicit,0)=1 THEN 1 ELSE 0 END) AS implicit_cnt,
               GROUP_CONCAT(DISTINCT r.source) AS sources
        FROM needs n
        JOIN raw_items r ON r.id = n.raw_id
        JOIN clusters c ON c.id = n.cluster_id
        WHERE n.cluster_id IS NOT NULL AND n.day >= ?
        """
        params: list = [since]
        if source:
            sql += " AND r.source = ?"
            params.append(source)
        sql += " GROUP BY c.id ORDER BY cnt DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = [dict(r) for r in self.conn.execute(sql, params)]
        return rows

    def demand_trend(self, cluster_ids: list[int], days: int = 30) -> dict[int, list]:
        since = _since(days)
        out: dict[int, list] = {cid: [] for cid in cluster_ids}
        if not cluster_ids:
            return out
        qmarks = ",".join("?" * len(cluster_ids))
        sql = f"""
        SELECT cluster_id, day, COUNT(*) c FROM needs
        WHERE cluster_id IN ({qmarks}) AND day >= ?
        GROUP BY cluster_id, day ORDER BY day
        """
        with self._lock:
            for r in self.conn.execute(sql, (*cluster_ids, since)):
                out.setdefault(r["cluster_id"], []).append({"day": r["day"], "count": r["c"]})
        return out

    def daily_counts(self, days: int = 30) -> list[dict]:
        since = _since(days)
        with self._lock:
            raw = {r["day"]: r["c"] for r in self.conn.execute(
                "SELECT day, COUNT(*) c FROM raw_items WHERE day>=? GROUP BY day", (since,))}
            needs_map = {r["day"]: r["c"] for r in self.conn.execute(
                "SELECT day, COUNT(*) c FROM needs WHERE day>=? GROUP BY day", (since,))}
        days_list = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days, -1, -1)]
        return [{"day": d, "items": raw.get(d, 0), "needs": needs_map.get(d, 0)} for d in days_list]

    def source_distribution(self, days: int = 30) -> list[dict]:
        since = _since(days)
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT source, COUNT(*) c FROM raw_items WHERE day>=? GROUP BY source ORDER BY c DESC", (since,))]

    def pain_distribution(self, days: int = 30) -> dict:
        since = _since(days)
        with self._lock:
            rows = list(self.conn.execute(
                "SELECT pain_score AS s, COUNT(*) c FROM needs WHERE day>=? GROUP BY pain_score", (since,)))
        return {str(r["s"]): r["c"] for r in rows}

    def keyword_hits(self, days: int = 30, limit: int = 15) -> list[dict]:
        since = _since(days)
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT keyword, COUNT(*) c FROM raw_items WHERE day>=? AND keyword<>'' "
                "GROUP BY keyword ORDER BY c DESC LIMIT ?", (since, limit))]

    def cluster_evidence(self, cluster_id: int, limit: int = 30) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT r.source, r.url, r.title, r.content, r.published_at, "
                "n.demand, n.quote, n.pain_score, COALESCE(n.implicit,0) AS implicit, "
                "COALESCE(n.reasoning,'') AS reasoning "
                "FROM needs n JOIN raw_items r ON r.id=n.raw_id "
                "WHERE n.cluster_id=? ORDER BY n.id DESC LIMIT ?", (cluster_id, limit))]

    def top_products(self, days: int = 30, limit: int = 20) -> list[dict]:
        since = _since(days)
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT product, COUNT(*) c, AVG(pain_score) avg_pain FROM needs "
                "WHERE day>=? AND product<>'' GROUP BY product ORDER BY c DESC LIMIT ?", (since, limit))]

    def count_pending(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) c FROM raw_items WHERE processed=0").fetchone()["c"]
