# -*- coding: utf-8 -*-
"""单独执行一次扫码登录，登录态会持久化到 data/browser/profile

用法：python login_xhs.py
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import sys
from pathlib import Path

import sys

from core import config as C
from core.xhs_crawler import login_only

if __name__ == "__main__":
    cfg = C.load_config()
    headless = cfg.get_path("crawl.headless", False)
    if headless:
        print("[!] 配置里 crawl.headless=true，登录时看不见浏览器。已临时改为显示窗口")
        cfg["crawl"]["headless"] = False
    try:
        login_only(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[x] 登录流程出错: {e}")
        sys.exit(1)
