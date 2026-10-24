# -*- coding: utf-8 -*-
"""体检：一次性检查配置、登录态、依赖是否齐全

用法：
  python doctor.py                 # 只做静态检查
  python doctor.py --test-llm      # 额外真实调用一次大模型
  python doctor.py --test-all      # 两者都测
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

OK, BAD, WARN = "[✓]", "[x]", "[!]"
issues: list[str] = []


def line(mark: str, title: str, detail: str = "") -> None:
    print(f"  {mark} {title}" + (f"　{detail}" if detail else ""))


def check_deps() -> bool:
    print("\n1) 运行环境")
    line(OK, f"Python {sys.version.split()[0]}", sys.executable)
    ok = True
    for mod in ("playwright", "requests", "yaml"):
        try:
            __import__(mod)
            line(OK, f"依赖 {mod}")
        except ImportError:
            line(BAD, f"依赖 {mod} 缺失", "运行 pip install -r requirements.txt")
            ok = False
    return ok


def check_config():
    print("\n2) 配置文件")
    from core import config as C

    p = C.ROOT / "config.yaml"
    if not p.exists():
        line(BAD, "config.yaml 不存在", "复制 config.example.yaml 为 config.yaml")
        issues.append("创建 config.yaml")
        return None
    cfg = C.load_config()
    line(OK, "config.yaml 已加载")


    api_key = (cfg.get_path("llm.api_key") or "").strip()
    base = (cfg.get_path("llm.base_url") or "").strip()
    model = (cfg.get_path("llm.model") or "").strip()
    if not api_key:
        line(BAD, "大模型 api_key 未填", "DeepSeek 可到 platform.deepseek.com 申请")
        issues.append("填写 llm.api_key")
    else:
        line(OK, "大模型 key 已填", f"{api_key[:6]}...（{model} @ {base}）")
    if not base or not model:
        line(BAD, "base_url 或 model 为空")
        issues.append("补全 llm.base_url / llm.model")

    ind_name = (cfg.get_path("industry.name", "") or "").strip()
    if ind_name:
        per = cfg.get_path("industry.per_keyword", 5)
        line(OK, "采集模式", f"行业链路 · {ind_name} · 每词 {per} 条")
        plan_p = Path("data") / f"industry_plan_{ind_name}.json"
        if plan_p.exists():
            line(OK, "行业拆解已有缓存", str(plan_p))
        else:
            line(WARN, "行业拆解待首次生成", "首次运行会调用模型拆环节")
    else:
        kws = cfg.get_path("crawl.keywords", []) or []
        line(OK if kws else BAD, f"采集模式：平铺关键词 · {len(kws)} 个")
        if not kws:
            issues.append("填写 crawl.keywords 或 industry.name")

    line(OK, "广告主/作者上限",
         f"每广告主 {cfg.get_path('industry.max_per_advertiser', 1)} 条 / "
         f"每作者 {cfg.get_path('industry.max_per_author', 1)} 条")
    return cfg


def check_login() -> bool:
    print("\n3) 小红书登录态")
    import json
    from datetime import datetime

    from core.config import BROWSER_DIR

    marker = BROWSER_DIR / "login_ok.json"
    if not marker.exists():
        line(BAD, "尚未确认登录", "运行 python login_xhs.py 扫码（建议用小号）")
        issues.append("扫码登录小红书")
        return False
    try:
        at = json.loads(marker.read_text(encoding="utf-8")).get("at", "")
        days = (datetime.now() - datetime.fromisoformat(at)).days
    except Exception:
        at, days = "未知", 0
    if days >= 25:
        line(WARN, f"登录于 {at}（{days} 天前）", "接近过期窗口，下次运行可能要求重扫码")
    else:
        line(OK, f"登录于 {at}（{days} 天前）")
    line(WARN, "实际有效性以运行结果为准", "过期时会自动提示重扫码")
    return True


def check_emulator(cfg) -> None:
    print("\n7) 模拟器")
    from core import emulator as emu

    if not bool(cfg.get_path("emulator.auto_start", True)):
        line(WARN, "未启用自动启动", "config.yaml 里 emulator.auto_start = false")
        return
    try:
        mgr = emu._manager()
        line(OK, "MuMuManager 已定位", str(mgr))
        st = emu.vm_status(int(cfg.get_path("emulator.vm_index", 0)))
        started = bool(st and st.get("is_android_started"))
        port = int(cfg.get_path("emulator.adb_port", 16384))
        online = emu._adb_connected(port)
        if started and online:
            line(OK, "模拟器运行中", f"adb 127.0.0.1:{port} 已就绪")
        elif started:
            line(WARN, "已开机但 adb 未就绪", "重跑时脚本会自动重连")
        else:
            line(WARN, "模拟器未启动", "跑 run_daily 时会自动拉起")
    except emu.EmuError as e:
        line(BAD, "模拟器不可用", str(e))
        issues.append("检查 MuMu 安装（或设置 MUMU_MANAGER 环境变量）")


def check_ima(cfg) -> None:
    print("\n6) IMA 知识库")
    from core import ima

    if not bool(cfg.get_path("ima.enabled", False)):
        line(WARN, "未启用", "config.yaml 里 ima.enabled = false")
        return
    kb = (cfg.get_path("ima.knowledge_base_id") or "").strip()
    if not ima.has_credentials(cfg):
        line(BAD, "缺少 IMA 凭证", "https://ima.qq.com/agent-interface 或 ~/.config/ima/")
        issues.append("配置 IMA 凭证（client_id / api_key）")
        return
    line(OK, "凭证已就绪")
    if not kb:
        line(BAD, "未填 knowledge_base_id", "运行 python ima_setup.py 列出你的知识库")
        issues.append("填写 ima.knowledge_base_id")
    else:
        line(OK, "知识库 ID 已填", kb)


def check_llm(cfg) -> None:
    print("\n4) 大模型连通性（真实调用，会产生极少量费用）")
    from core.analyzer import LLMError, call_llm

    try:
        out = call_llm(cfg, '只回一个 JSON：{"ok":true}', temperature=0)
        line(OK, "调用成功", out.strip()[:60])
    except LLMError as e:
        line(BAD, "调用失败", str(e)[:120])
        issues.append("排查 llm 配置（key/base_url/model 是否匹配同一服务商）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-llm", "--test-all", dest="test_llm", action="store_true",
                    help="真实调用一次大模型验证连通性")
    a = ap.parse_args()

    print("=" * 56)
    print("小红书广告洞察工具 · 自检")
    print("=" * 56)

    check_deps()
    cfg = check_config()
    check_login()
    if cfg:
        check_ima(cfg)
        check_emulator(cfg)
    if cfg and a.test_llm:
        check_llm(cfg)
    if cfg and not a.test_llm:
        print("\n4) 大模型连通性测试默认跳过（会产生真实调用），需要时加 --test-llm")

    print("\n" + "=" * 56)
    if issues:
        print("还需要处理：")
        for i, s in enumerate(issues, 1):
            print(f"  {i}. {s}")
    else:
        print("配置齐全，可以跑了：python run.py <行业名>")
    print("=" * 56)


if __name__ == "__main__":
    main()
