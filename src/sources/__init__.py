# -*- coding: utf-8 -*-
from .base import BaseSource, RawItem
from .tieba import TiebaSource
from .bilibili import BilibiliSource
from .searchengine import SearchEngineSource
from .genericpage import GenericPageSource
from .browser_import import BrowserImportSource

# 顺序即执行顺序：浏览器导入最快，放最前
REGISTRY = {
    "browser_import": BrowserImportSource,
    "tieba": TiebaSource,
    "bilibili": BilibiliSource,
    "search_engine": SearchEngineSource,
    "generic_page": GenericPageSource,
}

__all__ = ["BaseSource", "RawItem", "REGISTRY", "TiebaSource", "BilibiliSource",
           "SearchEngineSource", "GenericPageSource", "BrowserImportSource"]
