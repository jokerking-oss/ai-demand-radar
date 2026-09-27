# -*- coding: utf-8 -*-
"""文本清洗、脱敏、去重指纹。"""
from __future__ import annotations

import hashlib
import re

_PII = [
    (re.compile(r"1[3-9]\d{9}"), "[手机号]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[邮箱]"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "[身份证]"),
    (re.compile(r"\b\d{16,19}\b"), "[卡号]"),
    (re.compile(r"(?:微信|weixin|wx|vx|v信|威信)\s*[:：]?\s*([-_a-zA-Z0-9\u4e00-\u9fa5]{5,20})", re.I), "[联系方式]"),
    (re.compile(r"(?:qq|QQ)\s*[:：]?\s*(\d{5,12})"), "[联系方式]"),
    (re.compile(r"(?:加|私|扣)[vV威]\b"), "[联系方式]"),
]

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile(
    "[" "\U0001F300-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F000-\U0001F2FF" "]+", flags=re.UNICODE
)
_REPEAT = re.compile(r"(.)\1{3,}")
_BRACKET = re.compile(r"\[.{1,12}?\]")


def redact_pii(text: str) -> str:
    """把手机号/邮箱/联系方式等个人信息打码后入库。"""
    for pat, repl in _PII:
        text = pat.sub(repl, text)
    return text


def clean_text(text: str, *, redact: bool = True) -> str:
    if not text:
        return ""
    text = _CTRL.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _EMOJI.sub(" ", text)
    text = _BRACKET.sub(" ", text)
    text = _REPEAT.sub(r"\1\1\1", text)
    text = _WS.sub(" ", text).strip()
    if redact:
        text = redact_pii(text)
    return text.strip()


def author_hash(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "anon"
    return hashlib.sha1(name.encode("utf-8", "ignore")).hexdigest()[:16]


def content_fingerprint(text: str) -> str:
    """跨平台的转载判定：去掉标点空白后取 sha1。"""
    norm = re.sub(r"[\s\W_]+", "", text or "")
    return hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:20]


def item_id(source: str, source_id: str, author_key: str) -> str:
    raw = f"{source}|{source_id or ''}|{author_key or ''}"
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:24]


def strip_html_tags(html: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", " ", html or ""))
