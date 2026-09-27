# -*- coding: utf-8 -*-
"""采集器公共定义。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..clean import author_hash, clean_text, content_fingerprint


@dataclass
class RawItem:
    source: str
    source_id: str = ""
    url: str = ""
    title: str = ""
    content: str = ""
    author: str = ""
    published_at: str = ""
    keyword: str = ""
    meta: dict = field(default_factory=dict)

    def to_record(self, *, redact: bool = True) -> dict:
        body = clean_text(self.content, redact=redact)
        title = clean_text(self.title, redact=redact)
        if not body and title:
            body = title
        blob = f"{title}\n{body}" if title and body else (title or body)
        ah = author_hash(self.author)
        sid = self.source_id or hashlib.sha1(blob.encode("utf-8", "ignore")).hexdigest()[:16]
        return {
            "id": f"{self.source}:{sid}:{ah[:8]}",
            "source": self.source,
            "source_id": sid,
            "url": self.url,
            "title": title[:200],
            "content": body[:2000],
            "content_fp": content_fingerprint(body)[:20],
            "author_hash": ah,
            "published_at": self.published_at,
            "day": (self.published_at[:10] if self.published_at
                    else datetime.now().strftime("%Y-%m-%d")),
            "keyword": self.keyword,
            "meta": {k: v for k, v in self.meta.items() if v not in (None, "")},
        }


class BaseSource:
    name = "base"

    def __init__(self, cfg: dict, fetcher, log=print):
        self.cfg = cfg
        self.fetcher = fetcher
        self.log = log
        self.found = 0

    def collect(self, keywords: list[str]) -> list[RawItem]:
        raise NotImplementedError

    def _enabled(self) -> bool:
        return bool(self.cfg.get("sources", {}).get(self.name, {}).get("enabled", True))

    def _opt(self, key, default):
        return self.cfg.get("sources", {}).get(self.name, {}).get(key, default)


_TAG = re.compile(r"<[^>]{1,80}>")


def strip_tags(text: str) -> str:
    """去处组：说一下这东西是干啥的——把 HTML 标签和常见高亮标签清掉。"""
    text = _TAG.sub("", text or "")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">") \
               .replace("&quot;", '"').replace("&nbsp;", " ").replace("&#39;", "'")
    return text


def ts_to_iso(ts) -> str:
    try:
        ts = int(ts)
        if ts <= 0:
            return ""
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except Exception:
        return ""
