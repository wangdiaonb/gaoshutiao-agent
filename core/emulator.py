# -*- coding: utf-8 -*-
"""自动启动 MuMu 模拟器并打开小红书

用 MuMu 自带的命令行工具 MuMuManager.exe（比模拟点击窗口可靠）：
  MuMuManager.exe info -v all       # 看虚拟机状态（is_android_started）
  MuMuManager.exe control -v 0 launch  # 启动
安装路径优先从 %APPDATA%\\Netease\\MuMuPlayer\\install_config.json 读，
这样换版本/换盘也不用改代码。
"""
from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
import re
import subprocess
import time
from pathlib import Path

from . import config as C

PKG = "com.xingin.xhs"


class EmuError(RuntimeError):
    pass


# ---------------------------------------------------------------- 定位
def _install_root() -> Path | None:
    """从 MuMu 自己的安装配置里读安装目录"""
    cand = [
        Path(os.environ.get("APPDATA", "")) / "Netease" / "MuMuPlayer" / "install_config.json",
        Path.home() / "AppData/Roaming/Netease/MuMuPlayer/install_config.json",
    ]
    for p in cand:
        try:
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
                root = (d.get("product") or {}).get("install_dir")
                if root and Path(root).exists():
                    return Path(root)
        except Exception:
            continue
    return None


def _manager() -> Path:
    """MuMuManager.exe 路径"""
    env = (os.environ.get("MUMU_MANAGER") or "").strip()
    if env and Path(env).exists():
        return Path(env)

    root = _install_root()
    if root:
        m = root / "nx_main" / "MuMuManager.exe"
        if m.exists():
            return m

    for base in (r"C:\Program Files\Netease", r"D:\Program Files\Netease", r"E:\Program Files\Netease"):
        m = Path(base) / "MuMu" / "nx_main" / "MuMuManager.exe"
        if m.exists():
            return m
    raise EmuError("找不到 MuMuManager.exe（可用环境变量 MUMU_MANAGER 指定完整路径）")


def _adb() -> Path | None:
    """MuMu 自带的 adb.exe，用来补连端口"""
    root = _install_root()
    for base in ([root] if root else []) + [Path(r"C:\Program Files\Netease\MuMu"),
                                           Path(r"D:\Program Files\Netease\MuMu")]:
        a = Path(base) / "nx_main" / "adb.exe"
        if a.exists():
            return a
    return None


def _run(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"超时 {timeout}s"
    except FileNotFoundError as e:
        return -1, f"命令不存在: {e}"


# ---------------------------------------------------------------- 状态
def _parse_vm(out: str, idx: int) -> dict | None:
    """info 输出可能是单个对象，也可能是数组"""
    text = out.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        # 多台时输出可能是多段 JSON，逐个抓
        objs = []
        for m in re.finditer(r"\{[^{}]*\}", text, re.S):
            try:
                objs.append(json.loads(m.group(0)))
            except Exception:
                pass
        data = objs[0] if len(objs) == 1 else objs
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for d in data:
            if str(d.get("index")) == str(idx):
                return d
        return data[0] if data else None
    return None


def vm_status(idx: int = 0) -> dict | None:
    mgr = _manager()
    rc, out = _run([str(mgr), "info", "-v", str(idx)])
    if rc != 0:
        raise EmuError(f"读取模拟器状态失败：{out.strip()[:150]}")
    return _parse_vm(out, idx)


def is_android_started(idx: int = 0) -> bool:
    st = vm_status(idx)
    return bool(st and st.get("is_android_started"))


# ---------------------------------------------------------------- 控制
def adb_serials() -> list[str]:
    """列出状态为 device 的序列号（MuMu 有时是 emulator-5554，有时是 127.0.0.1:16384，不能写死）"""
    a = _adb()
    if not a:
        return []
    rc, out = _run([str(a), "devices"])
    if rc != 0:
        return []
    serials = []
    for line in out.splitlines():
        m = re.match(r"^(\S+)\tdevice\s*$", line.strip())
        if m:
            serials.append(m.group(1))
    return serials


def _has_pkg(serial: str, pkg: str = PKG) -> bool:
    a = _adb()
    if not a:
        return False
    rc, out = _run([str(a), "-s", serial, "shell", "pm", "path", pkg], timeout=25)
    return rc == 0 and "package:" in out


def find_device(port_hint: int | None = None, prefer_pkg: str = PKG) -> str | None:
    """挑一台可用的设备：优先装了小红书的"""
    serials = adb_serials()
    if not serials and port_hint:
        connect_adb(port_hint)
        serials = adb_serials()
    if not serials:
        return None
    for s_ in serials:
        if _has_pkg(s_, prefer_pkg):
            return s_
    # 有端口提示且在线，就用它
    if port_hint and f"127.0.0.1:{port_hint}" in serials:
        return f"127.0.0.1:{port_hint}"
    return serials[0]


def _adb_connected(port: int | None = None) -> bool:
    """兼容旧调用：有任意设备在线就算连上"""
    return bool(adb_serials())


def wait_device_online(port: int | None = None, timeout: int = 90) -> bool:
    """等任意设备变成 device 状态；顺便尝试连一下历史端口"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if adb_serials():
            return True
        if port:
            connect_adb(port)
        time.sleep(3)
    return False


def wait_boot_done(port: int = 16384, timeout: int = 120) -> bool:
    """等 Android 真正启动完成（能连上 uiautomator 且 boot_completed=1）"""
    import uiautomator2 as u2

    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = u2.connect(f"127.0.0.1:{port}")
            if str(d.shell("getprop sys.boot_completed")).strip() == "1":
                return True
        except Exception:
            pass
        time.sleep(4)
    return False


def connect_adb(port: int = 16384) -> bool:
    a = _adb()
    if not a:
        return False
    _run([str(a), "connect", f"127.0.0.1:{port}"], timeout=30)
    time.sleep(1)
    return _adb_connected(port)


def start(idx: int = 0) -> None:
    """启动模拟器。

    必须用 DETACHED_PROCESS 脱离父进程，否则调用方的终端/进程树一结束，
    模拟器会被一起回收（实测：不脱离时开机 24 秒后随即消失）。
    """
    mgr = _manager()
    print(f"[·] 启动模拟器（实例 {idx}）…")
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [str(mgr), "control", "-v", str(idx), "launch"],
            creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:  # noqa: BLE001
        raise EmuError(f"启动失败：{e}") from e


def stop(idx: int = 0) -> None:
    mgr = _manager()
    _run([str(mgr), "control", "-v", str(idx), "shutdown"], timeout=60)


def ensure_running(cfg: C.Cfg | None = None, idx: int | None = None,
                   timeout: int | None = None) -> bool:
    """确保模拟器已开机且 adb 可见；返回是否就绪"""
    idx = int(idx if idx is not None else (cfg.get_path("emulator.vm_index", 0) if cfg else 0))
    timeout = int(timeout or (cfg.get_path("emulator.boot_timeout", 180) if cfg else 180))
    port = int(cfg.get_path("emulator.adb_port", 16384) if cfg else 16384)

    try:
        if is_android_started(idx):
            print(f"[✓] 模拟器已在运行（实例 {idx}）")
            if not _adb_connected(port):
                print("[·] adb 未就绪，尝试连接端口", port)
                if not wait_device_online(port, 60):
                    print("[!] adb 连接不上，可能模拟器还在启动")
                    return False
            return True

        start(idx)
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(5)
            if is_android_started(idx):
                print(f"[✓] 模拟器已开机（{int(time.time() - t0)}s）")
                if not wait_device_online(port, 90):
                    print("[!] adb 未就绪")
                    return False
                print(f"[✓] adb 已连接 127.0.0.1:{port}")
                return True
            print(f"    等待开机… {int(time.time() - t0)}s")
        raise EmuError(f"等待开机超时（{timeout}s）")
    except EmuError:
        raise
    except Exception as e:  # noqa: BLE001
        raise EmuError(f"模拟器启动异常：{e}") from e


def wait_system_ready(port: int | None = None, timeout: int = 120) -> str | None:
    """等系统能响应（包管理器可查），返回可用设备序列号"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        serial = find_device(port)
        if serial and _has_pkg(serial):
            return serial
        time.sleep(3)
    return None


def ensure_app(cfg: C.Cfg | None = None, wait: int = 10) -> bool:
    """打开小红书并确认在前台；未登录时自动点「重新登录」把二维码调出来"""
    import uiautomator2 as u2

    port = int(cfg.get_path("emulator.adb_port", 16384) if cfg else 16384)

    print("[·] 等待系统就绪…")
    serial = wait_system_ready(port, 120)
    if not serial:
        raise EmuError("模拟器已开机但系统还没响应（包列表查不到），稍等一会儿重跑即可")
    print(f"[✓] 系统就绪：{serial}")

    try:
        d = u2.connect(serial)
        _ = d.window_size()
    except Exception as e:  # noqa: BLE001
        raise EmuError(f"连不上设备 {serial}：{e}") from e

    # 打开小红书：最多试 3 次，期间可能系统还在加载
    for attempt in range(3):
        cur = (d.app_current() or {}).get("package", "")
        if cur == PKG:
            break
        print(f"[·] 打开小红书（第 {attempt + 1} 次）…")
        try:
            d.app_start(PKG)
        except Exception as e:  # noqa: BLE001
            print(f"    app_start 失败：{e}")
        for _ in range(wait):
            time.sleep(1)
            if (d.app_current() or {}).get("package") == PKG:
                break
        if (d.app_current() or {}).get("package") == PKG:
            break
        # 兜底：用 monkey 走 LAUNCHER intent
        try:
            d.shell(f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")
            time.sleep(4)
        except Exception:
            pass

    cur = (d.app_current() or {}).get("package", "")
    if cur != PKG:
        raise EmuError("小红书没能进入前台（可能没安装，或系统还在加载）")

    # 判断是否掉登录（设备下线 / 未登录）
    try:
        xml = d.dump_hierarchy(compressed=False)
    except Exception:
        xml = ""
    act = (d.app_current() or {}).get("activity", "") or ""
    need_login = any(k in xml for k in ("账号下线", "重新登录", "已在其他设备登录", "扫码登录")) \
        or "login" in act.lower() or "DeviceOffline" in act

    if need_login:
        print("[!] 小红书需要重新登录（账号在其它设备登录过，模拟器这边被踢下线）")
        if cfg is None or bool(cfg.get_path("emulator.auto_relogin", True)):
            try:
                from .android_crawler import center, parse_bounds
                for n in ET.fromstring(xml).iter("node"):
                    t = (n.get("text") or "").strip()
                    if t in ("重新登录", "登录", "立即登录"):
                        b = parse_bounds(n.get("bounds"))
                        if b:
                            d.shell(f"input tap {center(b)[0]} {center(b)[1]}")
                            print("    [·] 已点「重新登录」，请用手机小红书扫码")
                            time.sleep(2)
                            break
            except Exception:
                pass
        return False

    print("[✓] 小红书已在前台且已登录")
    return True


def prepare(cfg: C.Cfg | None = None) -> bool:
    """一键准备：开机 → adb → 等系统启动完成 → 打开小红书"""
    port = int(cfg.get_path("emulator.adb_port", 16384) if cfg else 16384)
    if not ensure_running(cfg):
        raise EmuError("模拟器没能就绪")
    print("[·] 等待系统启动…")
    return ensure_app(cfg)


if __name__ == "__main__":
    import sys

    cfg = C.load_config()
    if "--stop" in sys.argv:
        stop(int(cfg.get_path("emulator.vm_index", 0)))
        print("[✓] 已关闭模拟器")
    else:
        st = vm_status(int(cfg.get_path("emulator.vm_index", 0)))
        print("当前状态:", json.dumps(st, ensure_ascii=False))
        print("准备就绪:", prepare(cfg))
