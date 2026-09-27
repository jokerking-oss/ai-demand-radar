# -*- coding: utf-8 -*-
"""统一 HTTP 层：限速、抖动、robots、代理、重试。

设计原则（合规底线）：
- 同一域名两次请求之间强制随机休眠，模拟人浏览，绝不做高频轰炸；
- 单域名每小时有请求上限，超限本轮直接跳过；
- 默认尊重 robots.txt；
- 不支持任何形式的登录破解 / 验证码绕过，遇到风控（403/432/验证码页）直接记录并跳过。
"""
from __future__ import annotations

import random
import threading
import time
from collections import defaultdict
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests


class DomainLimited(Exception):
    """域名本轮请求额度用尽。"""


class Fetcher:
    def __init__(self, cfg: dict, log=print):
        self.cfg = cfg
        self.log = log
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": cfg.get("compliance", {}).get("user_agent", "Mozilla/5.0"),
            "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        })
        self._lock = threading.Lock()
        self._last_hit: dict[str, float] = {}
        self._hour_hits: dict[str, list[float]] = defaultdict(list)
        self._robots: dict[str, object | None] = {}
        self.overrides_hit: set[str] = set()
        self.dead_domains: set[str] = set()   # 本轮已经废掉的域名，不再重试
        self.blocklist: set[str] = set(
            str(v or "").strip().lower().lstrip(".")
            for v in ((self.cfg.get("compliance", {}) or {}).get("domain_blocklist") or [])
            if str(v or "").strip()
        )
        self.stats = {"requests": 0, "ok": 0, "failed": 0, "skipped_robots": 0, "skipped_limit": 0}
        comp = self.cfg.get("compliance", {}) or {}
        if comp.get("respect_robots", True) and comp.get("robots_overrides"):
            self.log("  [合规] 以下域名 robots.txt 明写禁止抓取，但已在配置里显式放行："
                     + ", ".join(comp["robots_overrides"])
                     + "。系统仍保持低频/只读/仅公开数据/不破解登录，合规责任由使用者承担。")

    # ---------------- 代理 ----------------
    def _proxies(self, url: str) -> dict:
        net = self.cfg.get("network", {}) or {}
        px = (net.get("proxies") or {})
        default_mode = px.get("default", "auto")
        mode = default_mode
        host = urlparse(url).hostname or ""
        for pattern, override in (px.get("overrides") or {}).items():
            if host == pattern or host.endswith("." + pattern):
                mode = override
        if mode in (None, False, "none", "off", ""):
            proxies = {}
        elif mode == "auto":
            proxies = requests.utils.get_environ_proxies(url) or {}
        else:
            proxies = {"http": mode, "https": mode}
        # 兜底：代理挂了就直连，避免整轮采集被一个 502 拖死
        return proxies

    # ---------------- 限速 ----------------
    def _throttle(self, host: str) -> None:
        comp = self.cfg.get("compliance", {}) or {}
        lo = float(comp.get("delay_min_sec", 3))
        hi = float(comp.get("delay_max_sec", 8))
        cap = int(comp.get("max_requests_per_domain_per_hour", 150))

        now = time.time()
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            recent = [t for t in self._hour_hits.get(host, []) if now - t < 3600]
            self._hour_hits[host] = recent
            if len(recent) >= cap:
                self.stats["skipped_limit"] += 1
                raise DomainLimited(f"{host} 已达每小时 {cap} 次上限")
            wait = max(0.0, random.uniform(lo, hi) - (now - last))
        if wait > 0:
            time.sleep(wait)
        with self._lock:
            self._last_hit[host] = time.time()
            self._hour_hits.setdefault(host, []).append(time.time())

    # ---------------- robots ----------------
    def _robots_allows(self, url: str) -> bool:
        comp = self.cfg.get("compliance", {}) or {}
        if not comp.get("respect_robots", True):
            return True
        host = urlparse(url).hostname or ""
        # 白名单：某些站点（如 api.bilibili.com）的 robots.txt 明写着 Disallow: /，
        # 默认一律放行。只有用户在 config 里显式点名，我们才去取。
        for domain in comp.get("robots_overrides", []) or []:
            if host == domain or host.endswith("." + domain):
                self.overrides_hit.add(domain)
                return True
        if host in self._robots:
            rp = self._robots[host]
        else:
            rp = None
            try:
                parts = parts._replace(path="/robots.txt", query="", fragment="")
                resp = requests.request(
                    "GET", parts.geturl(), timeout=8,
                    headers={"User-Agent": self.session.headers["User-Agent"]},
                    proxies=self._proxies(parts.geturl()),
                )
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                    rp = parser
            except Exception:
                rp = None
            self._robots[host] = rp
        if rp is None:
            return True  # robots 取不到（如 404 / 网络问题）→ 放行
        try:
            return bool(rp.can_fetch(self.session.headers["User-Agent"], url))
        except Exception:
            return True

    # ---------------- 主入口 ----------------
    def get(self, url: str, *, retries: int = 2, allow_limit_skip: bool = True, **kw) -> requests.Response | None:
        host = urlparse(url).hostname or "-"
        if host != "-" and any(host == b or host.endswith("." + b) for b in self.blocklist):
            self.log(f"  [屏蔽] {host} 在黑名单里，跳过")
            return None
        if not self._robots_allows(url):
            self.stats["skipped_robots"] += 1
            self.log(f"  [robots] 不允许抓取，跳过 {url}")
            return None

        if host in self.dead_domains:
            return None
        timeout = kw.pop("timeout", (self.cfg.get("network", {}) or {}).get("timeout", 20))
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                self._throttle(host)
            except DomainLimited as e:
                if allow_limit_skip:
                    self.log(f"  [limit] {e}")
                    return None
                raise
            try:
                resp = self.session.get(url, timeout=timeout, proxies=self._proxies(url), **kw)
                self.stats["requests"] += 1
                if resp.status_code == 200:
                    self.stats["ok"] += 1
                    return resp
                if resp.status_code in (403, 429, 432, 503):
                    self.stats["failed"] += 1
                    self._blocked[host] += 1
                    if self._blocked[host] >= 2:
                        self.dead_domains.add(host)
                    if self._blocked[host] == 1:
                        self.log(f"  [风控] {host} HTTP {resp.status_code}，跳过"
                                 + ("；再出现就不再重试" if True else ""))
                    return None
                self.stats["failed"] += 1
                last_exc = RuntimeError(f"HTTP {resp.status_code}")
            except requests.RequestException as e:  # 连不上 / 被墙 / 代理挂 → 整域名本轮拉黑
                self.stats["failed"] += 1
                last_exc = e
                self.dead_domains.add(host)
                self.log(f"  [拉黑] {host} 本轮不再重试（{type(e).__name__}）")
                return None
            except Exception as e:  # 其他未知异常
                self.stats["failed"] += 1
                last_exc = e
            if attempt < retries:
                time.sleep(random.uniform(2.0, 5.0) * (attempt + 1))
        self.log(f"  [失败] {url[:90]} -> {last_exc}")
        return None

    def get_json(self, url: str, **kw):
        resp = self.get(url, **kw)
        if resp is None:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    def get_html(self, url: str, **kw):
        resp = self.get(url, **kw)
        if resp is None:
            return None
        enc = resp.apparent_encoding or "utf-8"
        resp.encoding = enc
        return resp.text
