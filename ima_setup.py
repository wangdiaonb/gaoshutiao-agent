# -*- coding: utf-8 -*-
"""IMA 知识库配置助手

用法：
  python ima_setup.py              # 检查凭证，列出可选的知识库
  python ima_setup.py --test       # 传一个测试文件，验证整条链路
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import config as C
from core import ima


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="上传测试文件验证链路")
    ap.add_argument("--kb", default=None, help="指定知识库 ID")
    ap.add_argument("--verify", action="store_true", help="查看知识库现有内容")
    a = ap.parse_args()

    cfg = C.load_config()
    print("=" * 56)
    print("IMA 知识库配置检查")
    print("=" * 56)

    if not ima.has_credentials(cfg):
        print("[x] 没有找到凭证")
        print("    1. 打开 https://ima.qq.com/agent-interface 获取 Client ID 和 API Key")
        print("    2. 二选一：")
        print("       · 填到 config.yaml 的 ima.client_id / ima.api_key")
        print("       · 或写入文件：~/.config/ima/client_id 与 ~/.config/ima/api_key")
        sys.exit(1)
    print("[✓] 凭证已就绪")

    try:
        kbs = ima.list_kbs(cfg)
    except ima.ImaError as e:
        print(f"[x] 读取知识库列表失败：{e}")
        sys.exit(1)

    if not kbs:
        print("[!] 没读到知识库，确认 IMA 里已创建知识库，且这个 API Key 有权限")
    else:
        print(f"\n共 {len(kbs)} 个知识库：")
        for kb in kbs:
            kid = kb.get("kb_id") or kb.get("id") or ""
            name = kb.get("kb_name") or kb.get("name") or ""
            cnt = kb.get("content_count")
            extra = f"（{cnt} 条内容）" if cnt not in (None, "") else ""
            print(f"  {kid}  {name}{extra}")
        print("\n把要用的 ID 填到 config.yaml 的 ima.knowledge_base_id")

    kb_id = a.kb or (cfg.get_path("ima.knowledge_base_id") or "").strip()
    if a.test:
        if not kb_id:
            print("\n[x] 没指定知识库，先填 ima.knowledge_base_id 或用 --kb 指定")
            sys.exit(1)
        tmp = C.DATA_DIR / "ima_test.md"
        tmp.write_text("# IMA 接入测试\n\n这条是小红书广告洞察工具上传的测试文件。\n",
                       encoding="utf-8")
        print(f"\n[·] 上传测试文件到 {kb_id} …")
        try:
            r = ima.upload_file(cfg, tmp, kb_id)
            print(f"[✓] 成功：{r['file_name']}")
            tmp.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            print(f"[x] 失败：{e}")
            sys.exit(1)
    elif kb_id:
        print(f"\n当前配置的知识库：{kb_id}")

    if a.verify and kb_id:
        print(f"\n[·] 读取 {kb_id} 的内容…")
        cnt = ima.kb_content_count(cfg, kb_id)
        items = ima.list_content(cfg, kb_id, 30)
        print(f"[✓] content_count={cnt}，实际返回 {len(items)} 条")
        for it in items:
            print(f"  - {it.get('title') or it.get('name')}（类型 {it.get('media_type')}）")


if __name__ == "__main__":
    main()
