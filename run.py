# -*- coding: utf-8 -*-
"""一句话入口：给一个行业名，全自动跑完

用法：
    python run.py 宠物用品                  # 自动开机 → 采集 → 分析 → 传 IMA 知识库
    python run.py 宠物用品 --per-keyword 3  # 每个关键词只取 3 条
    python run.py 宠物用品 --fresh          # 不从断点续采，重新开始
    python run.py --check                   # 自检（等价 doctor.py）
    python run.py --list                    # 看已有哪些行业拆解缓存

任何工具（Codex / Claude Code / WorkBuddy / 计划任务）都只需要这一条命令。
会自动切换到项目自带的 .venv 解释器，不需要你记路径。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def _ensure_venv() -> None:
    """不在项目虚拟环境里就切过去（换机器/换工具也照样能跑）"""
    if not VENV_PY.exists():
        return
    try:
        same = Path(sys.executable).resolve() == VENV_PY.resolve()
    except Exception:
        same = False
    if not same:
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])


def _usage() -> None:
    print(__doc__)


def main() -> int:
    _ensure_venv()
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        _usage()
        return 0

    if args[0] in ("--check", "--doctor"):
        import runpy
        sys.argv = ["doctor.py", *args[1:]]
        runpy.run_path(str(ROOT / "doctor.py"), run_name="__main__")
        return 0

    if args[0] == "--list":
        plans = sorted((ROOT / "data").glob("industry_plan_*.json"))
        if not plans:
            print("还没有行业拆解缓存，跑一次任意行业就会生成")
        for p in plans:
            name = p.stem.replace("industry_plan_", "")
            print(f"  {name}  ({p.name})")
        return 0

    # 第一个非选项参数当行业名
    industry = None
    rest: list[str] = []
    for a in args:
        if industry is None and not a.startswith("-"):
            industry = a
        else:
            rest.append(a)

    if not industry:
        print("[x] 缺少行业名，例如：python run.py 宠物用品")
        _usage()
        return 2

    print("=" * 56)
    print(f"行业：{industry}")
    print("=" * 56)

    sys.argv = ["run_daily.py", "--source", "android", "--industry", industry, *rest]
    import runpy
    runpy.run_path(str(ROOT / "run_daily.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
