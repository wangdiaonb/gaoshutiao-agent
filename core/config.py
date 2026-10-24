# -*- coding: utf-8 -*-
"""配置加载与路径管理"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
REPORT_DIR = DATA_DIR / "reports"
DB_PATH = DATA_DIR / "notes.db"
BROWSER_DIR = DATA_DIR / "browser"      # Playwright 持久化登录态
USER_DATA_DIR = BROWSER_DIR / "profile"


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, REPORT_DIR, BROWSER_DIR):
        d.mkdir(parents=True, exist_ok=True)


class Cfg(dict):
    """支持 cfg.get_path('a.b.c', default) 的字典包装"""

    def get_path(self, path: str, default=None):
        cur = self
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def load_config(path: str | None = None) -> Cfg:
    p = Path(path) if path else ROOT / "config.yaml"
    if not p.exists():
        example = ROOT / "config.example.yaml"
        print(f"[x] 找不到配置文件 {p}")
        print(f"    请先复制 {example.name} 为 config.yaml 并填写")
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = Cfg(raw)
    ensure_dirs()
    return cfg
