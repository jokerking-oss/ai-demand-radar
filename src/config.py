# -*- coding: utf-8 -*-
"""配置加载。所有开关都集中在 config/config.yaml。"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.is_absolute():
        p = ROOT / p
    cfg: dict = {}
    if p.exists():
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # 单独拆出来的密钥文件，优先级高于 config.yaml。
    # 目的：config.yaml 里全是业务配置，可以随便复制给别人看；
    # 凭据只在 secrets.yaml 里，不跟着一起外泄。
    sec = ROOT / "config" / "secrets.yaml"
    if sec.exists():
        try:
            s = yaml.safe_load(sec.read_text(encoding="utf-8")) or {}
            if isinstance(s, dict):
                cfg = _merge(cfg, s)
        except Exception:
            pass

    if overrides:
        cfg = _merge(cfg, overrides)
    return cfg


def g(cfg: dict, dotted: str, default=None):
    """按 'a.b.c' 取值，取不到返回 default。"""
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur if cur is not None else default


def path_of(cfg: dict, dotted: str, default_rel: str) -> Path:
    v = g(cfg, dotted)
    p = Path(v) if v else (ROOT / default_rel)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def api_key(cfg: dict) -> str:
    """优先读配置文件里的明文，其次读环境变量。"""
    key = (g(cfg, "llm.api_key", "") or "").strip()
    if key:
        return key
    for env_name in (g(cfg, "llm.api_key_env", "DEEPSEEK_API_KEY"), "OPENAI_API_KEY"):
        val = (os.environ.get(env_name, "") or "").strip()
        if val:
            return val
    return ""


def llm_enabled(cfg: dict) -> bool:
    return bool(g(cfg, "llm.enabled", True)) and bool(api_key(cfg))
