# -*- coding: utf-8 -*-
"""Android 端探测：验证「广告」标签能不能被读到

原理：不破解 SSL，直接用 uiautomator2 读 Android 的 UI 树（所有 TextView 的文字）。
      「广告」标签是原生文字元素，UI 树里必然能看到。

前置：
  1. 装 Android 模拟器（MuMu / 夜神 / LDPlayer 任选）或连一台真机/云手机
  2. 模拟器里装好小红书 App 并登录
  3. 确认 adb 能看到设备：adb devices

用法：
  python android_probe.py                # 自动找唯一设备
  python android_probe.py 127.0.0.1:16384    # 指定 MuMu 默认端口
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import sys
from pathlib import Path

import re
import sys
import time
from collections import Counter

PKG = "com.xingin.xhs"
AD_WORDS = ["广告", "赞助", "品牌合作"]


def dump_texts(d) -> list[str]:
    """把当前界面的所有文字节点抓出来"""
    xml = d.dump_hierarchy()
    # text="" 与 content-desc="" 都可能承载文案
    texts = re.findall(r'text="([^"]*)"', xml)
    descs = re.findall(r'content-desc="([^"]*)"', xml)
    seen, out = set(), []
    for t in texts + descs:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def main() -> None:
    try:
        import uiautomator2 as u2
    except ImportError:
        print("[x] 缺依赖，先执行：pip install uiautomator2")
        return

    serial = sys.argv[1] if len(sys.argv) > 1 else None
    if not serial:
        try:
            from core.android_crawler import AndroidCrawler
            serial = AndroidCrawler._pick_device() or None
        except Exception:
            serial = None
    print("=" * 56)
    print("Android 端探测")
    print("=" * 56)

    try:
        d = u2.connect(serial) if serial else u2.connect()
        print(f"[✓] 已连接设备: {d.serial}")
        print(f"    分辨率: {d.window_size()}")
        print(f"    系统版本: {d.device_info.get('version')}")
    except Exception as e:  # noqa: BLE001
        print(f"[x] 连接失败: {e}")
        print("    1) 模拟器是否已启动？ 2) adb devices 能看到设备吗？")
        return

    info = d.app_current()
    if info.get("package") != PKG:
        print(f"[·] 当前不是小红书（{info.get('package')}），正在启动...")
        try:
            d.app_start(PKG)
            time.sleep(6)
        except Exception as e:  # noqa: BLE001
            print(f"[x] 启动失败，App 装了吗？{e}")
            return

    print("\n[·] 请确认 App 已登录，且停留在「发现」页")
    print("[·] 开始向下滑动并统计（每屏停留 2 秒）\n")

    total_hits = 0
    reports = []
    for i in range(6):
        d.swipe_ext("up", scale=0.7)
        time.sleep(2.0)
        texts = dump_texts(d)
        hits = [t for t in texts if t in AD_WORDS or any(w in t for w in AD_WORDS)]
        ad_tags = [t for t in texts if t.strip() in AD_WORDS]
        reports.append((i + 1, len(texts), ad_tags, hits))
        total_hits += len(ad_tags)
        print(f"第{i + 1}屏 | 文字节点 {len(texts)} | 「广告」类标签 {len(ad_tags)} {ad_tags[:5]}")

    print("\n" + "=" * 56)
    if total_hits:
        print(f"[✓] 读到 {total_hits} 个广告标签 —— Android 路线可行")
        print("    下一步：把 android_crawler.py 接上，按「有广告标签的卡片」采集")
    else:
        print("[!] 没读到任何「广告」标签")
        print("    三种可能：① 这一屏确实没广告 ② 抖音式懒加载导致标签未渲染")
        print("③ 模拟器被广告系统排除（最常见的坑）——建议换真机/云手机验证")
    print("=" * 56)

    try:
        d.dump_hierarchy(compressed=False, pretty=True)
        open("data/raw/android_ui.xml", "w", encoding="utf-8").write(
            d.dump_hierarchy(compressed=False))
        print("UI 树已保存: data/raw/android_ui.xml")
    except Exception:
        pass


if __name__ == "__main__":
    main()
