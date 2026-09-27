# -*- coding: utf-8 -*-
"""浏览器通道入库。

小红书 / 知乎 / 微博这类强反爬站点，明文请求会被风控。
正确做法是用你本机已经登录的真实浏览器去抓，把结果落成一个 json 丢进 data/inbox/，
本模块负责把它接进主流水线。

支持的输入格式（放在 data/inbox/ 下，文件名随意）：
    *.json  -> 单个对象 或 对象数组
    *.csv   -> 至少含 content 列；可选 source,url,title,author,published_at,keyword

每条对象的字段：
    source        string  平台名，如 xiaohongshu / zhihu / weibo（不填则从 url 猜）
    url           string
    title         string
    content       string  必填，正文/评论
    author        string
    published_at  string  ISO 时间，可空
    keyword       string

处理完的文件会被移到 inbox_done/ 归档。
"""
from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .base import BaseSource, RawItem

HOST_ALIAS = {
    "xiaohongshu.com": "xiaohongshu", "xhs.link": "xiaohongshu",
    "zhihu.com": "zhihu", "zhuanlan.zhihu.com": "zhihu",
    "weibo.com": "weibo", "weibo.cn": "weibo",
    "douban.com": "douban", "tieba.baidu.com": "tieba",
    "bilibili.com": "bilibili", "hupu.com": "hupu",
    "nga.cn": "nga", "smzdm.com": "smzdm", "ithome.com": "ithome",
}


def guess_source(url: str, fallback: str = "browser") -> str:
    host = (urlparse(url or "").hostname or "").replace("www.", "")
    for k, v in HOST_ALIAS.items():
        if host == k or host.endswith("." + k):
            return v
    return fallback


def _read_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else [data]


def _read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def load_inbox(inbox: Path, done_dir: Path, log=print) -> list[RawItem]:
    if not inbox.exists():
        inbox.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)
    items: list[RawItem] = []
    files = sorted(list(inbox.glob("*.json")) + list(inbox.glob("*.csv")))
    for fp in files:
        try:
            rows = _read_json(fp) if fp.suffix == ".json" else _read_csv(fp)
        except Exception as e:
            log(f"  [导入] {fp.name} 解析失败: {e}")
            shutil.move(str(fp), str(done_dir / f"{datetime.now():%Y%m%d%H%M%S}_{fp.name}.bad"))
            continue
        n = 0
        for row in rows:
            content = str(row.get("content") or row.get("text") or row.get("评论") or "").strip()
            if len(content) < 2:
                continue
            url = str(row.get("url") or row.get("链接") or "")
            src = str(row.get("source") or row.get("平台") or "").strip()
            items.append(RawItem(
                source=src or guess_source(url),
                source_id=str(row.get("id") or "") or None,
                url=url,
                title=str(row.get("title") or row.get("标题") or ""),
                content=content,
                author=str(row.get("author") or row.get("作者") or ""),
                published_at=str(row.get("published_at") or row.get("时间") or ""),
                keyword=str(row.get("keyword") or row.get("关键词") or ""),
            ))
            n += 1
        log(f"  [导入] {fp.name} -> {n} 条")
        shutil.move(str(fp), str(done_dir / f"{datetime.now():%Y%m%d%H%M%S}_{fp.name}"))
    return items


class BrowserImportSource(BaseSource):
    name = "browser_import"

    def collect(self, keywords: list[str]) -> list[RawItem]:
        if not self._enabled():
            return []
        from ..config import path_of
        inbox = path_of(self.cfg, "sources.browser_import.inbox_dir", "data/inbox")
        done = inbox.parent / "inbox_done"
        items = load_inbox(inbox, done, log=self.log)
        self.found += len(items)
        return items
