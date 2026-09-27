# -*- coding: utf-8 -*-
"""搜索引擎发现器：用 Bing 搜索关键词，广撒网找新帖子 / 新讨论。

产出两部分：
1. 搜索结果标题 + 摘要，本身就是带痛点信号的文本，直接入库；
2. 发现到的 URL，交给通用网页抓取器（generic_page）去取正文。
"""
from __future__ import annotations

import re
import urllib.parse
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .base import BaseSource, RawItem, strip_tags

ENGINES = {
    "bing": "https://cn.bing.com/search?q={q}&first={offset}&FORM=PERE",
    "baidu": "https://www.baidu.com/s?wd={q}&pn={offset}",
    "so360": "https://www.so.com/s?q={q}&pn={page}",
}
_TRACK_HOSTS = ("baidu.com/baidu.php", "jump.bdimg.com", "cn.bing.com/", "doubleclick")


def _clean_bing_href(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        href = "https://cn.bing.com" + href
    return href


class SearchEngineSource(BaseSource):
    name = "search_engine"

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.discovered: list[str] = []

    def _parse_bing(self, html: str, kw: str) -> list[RawItem]:
        soup = BeautifulSoup(html, "lxml")
        out: list[RawItem] = []
        for node in soup.select("li.b_algo"):
            a = node.find("a", href=True)
            if not a:
                continue
            link = _clean_bing_href(a["href"])
            title = strip_tags(a.get_text(" ", strip=True))
            cap = node.select_one(".b_caption p, .b_lineclamp1, .b_lineclamp2, .b_algoSlug, p")
            snippet = strip_tags(cap.get_text(" ", strip=True)) if cap else ""
            self.discovered.append(link)
            text = f"{title}。{snippet}".strip()
            if len(text) < 8:
                continue
            out.append(RawItem(
                source="bing",
                source_id=re.sub(r"\W+", "", urlparse(link).path + urlparse(link).query)[:40] or str(hash(link)),
                url=link, title=title, content=snippet or title, keyword=kw,
                meta={"engine": "bing"},
            ))
        return out

    def _parse_generic(self, html: str, kw: str, engine: str) -> list[RawItem]:
        soup = BeautifulSoup(html, "lxml")
        out: list[RawItem] = []
        for a in soup.select("h2 a[href], h3 a[href], .result-title a[href]"):
            link, title = a.get("href", ""), strip_tags(a.get_text(" ", strip=True))
            if not link.startswith("http"):
                continue
            self.discovered.append(link)
            out.append(RawItem(source=engine, source_id=str(abs(hash(link)) % 10**12),
                               url=link, title=title, content=title, keyword=kw,
                               meta={"engine": engine}))
        return out

    def collect(self, keywords: list[str]) -> list[RawItem]:
        if not self._enabled():
            return []
        engines = self._opt("engines", ["bing"]) or ["bing"]
        pages = int(self._opt("pages", 1))
        self.discovered.clear()
        out: list[RawItem] = []

        for engine in engines:
            if engine == "baidu":
                self.log("  [搜索] 百度 Web 端有强度风控，建议关掉，改用 bing")
            for kw in keywords:
                for page in range(pages):
                    if engine == "bing":
                        url = ENGINES["bing"].format(q=urllib.parse.quote(kw), offset=page * 10 + 1)
                    elif engine == "so360":
                        url = ENGINES["so360"].format(q=urllib.parse.quote(kw), page=page + 1)
                    else:
                        url = ENGINES["baidu"].format(q=urllib.parse.quote(kw), offset=page * 10)
                    html = self.fetcher.get_html(url)
                    if not html:
                        continue
                    items = (self._parse_bing(html, kw) if engine == "bing"
                             else self._parse_generic(html, kw, engine))
                    out.extend(items)
                    self.found += len(items)
                    self.log(f"  [搜索-{engine}] {kw} p{page+1} -> {len(items)} 条")
                    if not items:
                        break
        return out
