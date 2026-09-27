# -*- coding: utf-8 -*-
"""通用网页抓取器：给一批 URL，把正文抽出来。

负责：
- 上一轮搜索引擎发现的 URL；
- config/watch_urls.txt 里你自己填的常驻盯梢页；
- RSS / Atom 源。
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime

from bs4 import BeautifulSoup

from .base import BaseSource, RawItem, strip_tags


def _rss_items(xml: str) -> list[tuple[str, str, str, str]]:
    """返回 [(title, link, summary, pub)]"""
    soup = BeautifulSoup(xml, "lxml-xml")
    out = []
    for node in soup.find_all(["item", "entry"]):
        def pick(*tags):
            for t in tags:
                n = node.find(t)
                if n and n.text:
                    return strip_tags(n.text)
            return ""
        link = ""
        if node.find("link"):
            link = node.find("link").get_text(strip=True) or node.find("link").get("href", "")
        out.append((pick("title"), link, pick("description", "summary", "content"),
                    pick("pubDate", "published", "updated")))
    return out


class GenericPageSource(BaseSource):
    name = "generic_page"

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.extra_urls: list[str] = []

    def _watch_urls(self) -> list[str]:
        cfg_list = self._opt("watch_urls", []) or []
        path = self._opt("urls_file", "")
        file_list: list[str] = []
        if path:
            from ..config import path_of
            p = path_of(self.cfg, "sources.generic_page.urls_file", path)
            if p.exists():
                file_list = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                             if ln.strip() and not ln.strip().startswith("#")]
        seen, merged = set(), []
        for u in list(cfg_list) + file_list:
            if u not in seen:
                seen.add(u)
                merged.append(u)
        return merged

    def _fetch_one(self, url: str) -> RawItem | None:
        resp = self.fetcher.get(url)
        if resp is None:
            return None
        raw = resp.text
        ctype = (resp.headers.get("Content-Type") or "").lower()

        if "xml" in ctype or "rss" in ctype or url.endswith((".rss", ".xml", ".atom")):
            rows = _rss_items(raw)
            if rows:
                title, _link, summary, pub = rows[0]
                return RawItem(source="rss", source_id=url, url=url, title=title,
                               content=summary, published_at=pub[:25],
                               meta={"source_url": url})
        try:
            soup = BeautifulSoup(raw, "lxml")
        except Exception:
            return None
        for bad in soup(["script", "style", "noscript", "iframe"]):
            bad.decompose()

        title = strip_tags(soup.title.get_text(strip=True)) if soup.title else ""
        body_node = (soup.find("article") or soup.find("main")
                     or soup.select_one(".post-content, .rich-text, #content, .content")
                     or soup.body)
        text = ""
        if body_node:
            text = strip_tags(body_node.get_text(" ", strip=True))
        meta_desc = ""
        m = soup.find("meta", attrs={"name": "description"})
        if m:
            meta_desc = strip_tags(m.get("content", ""))
        content = text if len(text) > len(meta_desc) else meta_desc
        if len(content) < 10:
            return None
        host = urllib.parse.urlparse(url).hostname or "web"
        return RawItem(source=host.replace("www.", ""), source_id=url, url=url,
                       title=title[:200], content=content,
                       published_at=datetime.now().isoformat(timespec="seconds"))

    def collect(self, keywords: list[str]) -> list[RawItem]:
        if not self._enabled():
            return []
        urls = list(dict.fromkeys(list(self.extra_urls) + self._watch_urls()))
        if not urls:
            return []
        items: list[RawItem] = []
        self.log(f"  [通用] 待抓 {len(urls)} 个页面")
        for u in urls[:80]:
            it = self._fetch_one(u)
            if it:
                it.keyword = ",".join(keywords[:3])
                items.append(it)
        self.found += len(items)
        self.log(f"  [通用] 入库 {len(items)} 条")
        return items
