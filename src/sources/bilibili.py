# -*- coding: utf-8 -*-
"""B 站采集器：视频搜索 -> 每条视频的评论区。

两个公开接口：
1. 搜索  https://api.bilibili.com/x/web-interface/search/all/v2?keyword=<kw>
   需要先访问一次 https://www.bilibili.com/ 让浏览器拿到 buvid3 cookie（正常访问，非破解）。
2. 评论  https://api.bilibili.com/x/v2/reply?type=1&oid=<aid>&pn=<n>&ps=20&sort=1
"""
from __future__ import annotations

import urllib.parse

from .base import BaseSource, RawItem, strip_tags, ts_to_iso

SEARCH_API = "https://api.bilibili.com/x/web-interface/search/all/v2?keyword={kw}&page={page}"
REPLY_API = "https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&pn={pn}&ps=20&sort=1"
HOME = "https://www.bilibili.com/"


class BilibiliSource(BaseSource):
    name = "bilibili"

    def _seed_cookie(self) -> None:
        """访问首页换取 buvid3/b_nut cookie。只做一次。"""
        if getattr(self, "_seeded", False):
            return
        try:
            self.fetcher.get(HOME)
            self._seeded = True
        except Exception:
            self._seeded = True

    def _search_videos(self, kw: str, page: int) -> list[dict]:
        self._seed_cookie()
        data = self.fetcher.get_json(SEARCH_API.format(kw=urllib.parse.quote(kw), page=page))
        if not data or data.get("code") != 0:
            if data:
                self.log(f"  [B站] 搜索被风控 code={data.get('code')} msg={data.get('message')}")
            return []
        for group in (data.get("data") or {}).get("result") or []:
            if group.get("result_type") == "video":
                return group.get("data") or []
        return []

    def _comments(self, aid: int, want: int) -> list[tuple[dict, dict]]:
        """返回 [(reply, video_meta)]"""
        out: list[tuple[dict, dict]] = []
        per_page = 20
        pages = max(1, min(10, (want + per_page - 1) // per_page))
        for pn in range(1, pages + 1):
            data = self.fetcher.get_json(REPLY_API.format(aid=aid, pn=pn))
            if not data or data.get("code") != 0:
                break
            replies = (data.get("data") or {}).get("replies") or []
            if not replies:
                break
            out.extend((r, {}) for r in replies)
            if len(out) >= want:
                break
        return out[:want]

    def collect(self, keywords: list[str]) -> list[RawItem]:
        if not self._enabled():
            return []
        vpk = int(self._opt("videos_per_keyword", 8))
        cpv = int(self._opt("comments_per_video", 40))
        items: list[RawItem] = []

        for kw in keywords:
            videos = self._search_videos(kw, 1)[:vpk]
            if not videos:
                self.log(f"  [B站] {kw} 无搜索结果")
                continue
            self.log(f"  [B站] {kw} 命中视频 {len(videos)} 个，开始取评论")
            total = 0
            for v in videos:
                aid = v.get("aid")
                if not aid:
                    continue
                v_title = strip_tags(v.get("title") or "")
                v_url = v.get("arcurl") or (f"https://www.bilibili.com/video/{v.get('bvid')}"
                                            if v.get("bvid") else "")
                author = v.get("author") or v.get("uname") or ""
                for r, _ in self._comments(int(aid), cpv):
                    msg = strip_tags(((r.get("content") or {}).get("message")) or "")
                    if not msg:
                        continue
                    member = r.get("member") or {}
                    items.append(RawItem(
                        source="bilibili",
                        source_id=str(r.get("rpid") or ""),
                        url=v_url,
                        title=v_title,
                        content=msg,
                        author=member.get("uname") or "",
                        published_at=ts_to_iso(r.get("ctime")),
                        keyword=kw,
                        meta={"aid": aid, "up": author, "like": r.get("like", 0),
                              "video_url": v_url},
                    ))
                    total += 1
            self.found += total
            self.log(f"  [B站] {kw} -> 评论 {total} 条")
        return items
