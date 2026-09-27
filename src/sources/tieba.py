# -*- coding: utf-8 -*-
"""百度贴吧采集器。

用的是贴吧移动端公开 JSON 搜索接口：
    https://tieba.baidu.com/mo/q/search/thread?word=<关键词>&pn=<页>
返回 data.post_list，每条含 tid / title / content / time / user / forum_name。
每页 20 条，max_pages 控制翻页深度。
"""
from __future__ import annotations

import urllib.parse

from .base import BaseSource, RawItem, strip_tags, ts_to_iso

SEARCH_API = "https://tieba.baidu.com/mo/q/search/thread?word={kw}&pn={pn}"


class TiebaSource(BaseSource):
    name = "tieba"

    def collect(self, keywords: list[str]) -> list[RawItem]:
        if not self._enabled():
            return []
        pages = int(self._opt("pages", 2))
        items: list[RawItem] = []
        for kw in keywords:
            for page in range(1, pages + 1):
                url = SEARCH_API.format(kw=urllib.parse.quote(kw), pn=page)
                data = self.fetcher.get_json(url)
                if not data or not isinstance(data, dict):
                    continue
                posts = (data.get("data") or {}).get("post_list") or []
                if not posts:
                    break
                got = 0
                for p in posts:
                    title = strip_tags(p.get("title") or "")
                    body = strip_tags(p.get("content") or "")
                    if not (title or body):
                        continue
                    user = p.get("user") or {}
                    tid = str(p.get("tid") or "")
                    items.append(RawItem(
                        source="tieba",
                        source_id=f"{tid}:{p.get('pid','')}",
                        url=f"https://tieba.baidu.com/p/{tid}" if tid else "",
                        title=title,
                        content=body,
                        author=user.get("user_name") or "",
                        published_at=ts_to_iso(p.get("create_time") or p.get("time")),
                        keyword=kw,
                        meta={"forum": p.get("forum_name") or "",
                              "like": p.get("like_num", 0),
                              "reply": p.get("post_num", 0)},
                    ))
                    got += 1
                self.found += got
                self.log(f"  [贴吧] {kw} p{page} -> {got} 条")
                if not (data.get("data") or {}).get("has_more"):
                    break
        return items
