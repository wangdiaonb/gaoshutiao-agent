# -*- coding: utf-8 -*-
"""把日报自动导入 IMA 知识库

流程（与 ima-skill 规范一致）：
  preflight-check → check_repeated_names → create_media → COS 上传 → add_knowledge

只上传文本型文件：Markdown 摘要（media_type=7）和 CSV 明细（media_type=5）。
HTML 不在 IMA 支持范围内（只允许在桌面端手动添加）。

凭证二选一：
  · 环境变量 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY
  · 文件 ~/.config/ima/client_id 与 ~/.config/ima/api_key
  也可以写进 config.yaml 的 ima.client_id / ima.api_key。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import config as C

TOOLS = C.ROOT / "tools" / "ima"
TIMEOUT = 120


class ImaError(RuntimeError):
    pass


# ---------------------------------------------------------------- 基础
def _node() -> str:
    cands = [
        os.environ.get("IMA_NODE", ""),
        r"C:\Users\wangdiao\.workbuddy\binaries\node\versions\22.22.2-6\node.exe",
        "/usr/local/bin/node",
    ]
    for c in cands:
        if c and Path(c).exists():
            return c
    w = shutil.which("node")
    if w:
        return w
    raise ImaError("找不到 node（需要 Node 18+）")


def has_credentials(cfg: C.Cfg | None = None) -> bool:
    if cfg:
        if (cfg.get_path("ima.client_id") or "").strip() and (cfg.get_path("ima.api_key") or "").strip():
            return True
    if os.environ.get("IMA_OPENAPI_CLIENTID") or os.environ.get("IMA_CLIENT_ID"):
        return True
    home = Path.home() / ".config" / "ima"
    return (home / "client_id").exists() and (home / "api_key").exists()


def _opts(cfg: C.Cfg | None) -> str:
    cid = ((cfg.get_path("ima.client_id", "") if cfg else "") or "").strip()
    key = ((cfg.get_path("ima.api_key", "") if cfg else "") or "").strip()
    if cid and key:
        return json.dumps({"clientId": cid, "apiKey": key}, ensure_ascii=False)
    return "{}"


def _run(args: list[str], timeout: int = TIMEOUT) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"超时（{timeout}s）"
    except FileNotFoundError as e:
        return -1, "", f"命令不存在: {e}"


def api(cfg: C.Cfg | None, api_path: str, payload: dict, timeout: int = 90) -> dict:
    code, out, err = _run([_node(), str(TOOLS / "ima_api.cjs"), api_path,
                           json.dumps(payload, ensure_ascii=False), _opts(cfg)], timeout)
    if code != 0:
        msg = err.strip() or out.strip() or f"退出码 {code}"
        try:
            msg = json.loads(err).get("msg") or msg
        except Exception:
            pass
        raise ImaError(msg[:300])
    try:
        d = json.loads(out)
    except Exception as e:
        raise ImaError(f"返回不是 JSON：{out[:200]} ({e})") from e
    if d.get("code") not in (0, None):
        raise ImaError(str(d.get("msg") or d)[:300])
    return d.get("data") or {}


def list_kbs(cfg: C.Cfg | None = None) -> list[dict]:
    """列出可用的知识库"""
    data = api(cfg, "openapi/wiki/v1/search_knowledge_base",
               {"query": "", "cursor": "", "limit": 20})
    for key in ("info_list", "knowledge_base_list", "knowledge_bases", "list", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def list_content(cfg: C.Cfg | None, kb_id: str, limit: int = 20) -> list[dict]:
    """列出知识库里的条目（字段是 knowledge_list，注意和知识库列表的 info_list 不同）"""
    data = api(cfg, "openapi/wiki/v1/get_knowledge_list",
               {"knowledge_base_id": kb_id, "cursor": "", "limit": limit})
    return data.get("knowledge_list") or []


def kb_content_count(cfg: C.Cfg | None, kb_id: str) -> int | None:
    """从知识库列表里取 content_count，用来快速确认上传是否落库"""
    for kb in list_kbs(cfg):
        if (kb.get("kb_id") or kb.get("id")) == kb_id:
            try:
                return int(kb.get("content_count"))
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------- 上传
def _preflight(path: Path) -> dict:
    code, out, err = _run([_node(), str(TOOLS / "preflight-check.cjs"), "--file", str(path)])
    if code != 0 or not out.strip():
        raise ImaError(err.strip() or "preflight 失败")
    d = json.loads(out)
    if not d.get("pass", True):
        raise ImaError(f"文件不受支持：{d.get('reason') or d}")
    return d


def upload_file(cfg: C.Cfg, path: Path, kb_id: str) -> dict:
    """上传单个文件，返回 {file_name, media_id, ok}"""
    path = Path(path)
    if not path.exists():
        raise ImaError(f"文件不存在：{path}")

    info = _preflight(path)
    file_name = info["file_name"]
    media_type = int(info["media_type"])
    file_size = int(info["file_size"])
    content_type = info.get("content_type") or "application/octet-stream"
    file_ext = info.get("file_ext") or path.suffix.lstrip(".")

    # 重名检查：同名则加时间戳另存一份，绝不覆盖
    rep = api(cfg, "openapi/wiki/v1/check_repeated_names",
              {"params": [{"name": file_name, "media_type": media_type}],
               "knowledge_base_id": kb_id})
    # 返回形如 {"results":[{"name":"x.md","is_repeated":true}]}，标记在数组元素里
    repeated = False
    for item in (rep.get("results") or []):
        if isinstance(item, dict) and item.get("is_repeated"):
            repeated = True
            break
    if not repeated:
        repeated = bool(rep.get("is_repeated") or rep.get("repeated"))
    if repeated:
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        new_name = f"{Path(file_name).stem}_{stamp}.{file_ext}"
        tmp = path.with_name(new_name)
        shutil.copy2(path, tmp)
        path = tmp
        file_name = new_name
        print(f"    [·] 知识库已有同名文件，改名为 {new_name}")

    # 创建媒体，拿 COS 凭证
    media = api(cfg, "openapi/wiki/v1/create_media", {
        "file_name": file_name, "file_size": file_size,
        "content_type": content_type, "knowledge_base_id": kb_id,
        "file_ext": file_ext,
    })
    media_id = media.get("media_id") or media.get("id")
    cred = media.get("cos_credential") or {}
    if not media_id or not cred:
        raise ImaError(f"create_media 返回缺字段：{list(media)}")

    # COS 上传
    code, out, err = _run([
        _node(), str(TOOLS / "cos-upload.cjs"),
        "--file", str(path),
        "--secret-id", str(cred.get("secret_id", "")),
        "--secret-key", str(cred.get("secret_key", "")),
        "--token", str(cred.get("token", "")),
        "--bucket", str(cred.get("bucket_name", "")),
        "--region", str(cred.get("region", "")),
        "--cos-key", str(cred.get("cos_key", "")),
        "--content-type", content_type,
        "--start-time", str(cred.get("start_time", "")),
        "--expired-time", str(cred.get("expired_time", "")),
        "--timeout", "300000",
    ], timeout=300)
    if code != 0:
        raise ImaError(f"COS 上传失败：{(err or out).strip()[:200]}")

    # 落库
    api(cfg, "openapi/wiki/v1/add_knowledge", {
        "media_type": media_type,
        "media_id": media_id,
        "title": file_name,
        "knowledge_base_id": kb_id,
        "file_info": {"cos_key": cred.get("cos_key"),
                      "file_size": file_size, "file_name": file_name},
    })
    return {"file_name": file_name, "media_id": media_id, "ok": True}


def upload_reports(cfg: C.Cfg, paths: list[Path], kb_id: str | None = None) -> list[dict]:
    """把一批报告文件传进知识库；未配置则静默跳过"""
    if not bool(cfg.get_path("ima.enabled", False)):
        return []
    kb = (kb_id or cfg.get_path("ima.knowledge_base_id") or "").strip()
    if not kb:
        print("[·] 未配置 ima.knowledge_base_id，跳过知识库导入")
        return []
    if not has_credentials(cfg):
        print("[·] 未配置 IMA 凭证，跳过知识库导入（见 README「导入 IMA 知识库」）")
        return []

    results = []
    for p in paths:
        if not Path(p).exists():
            continue
        try:
            r = upload_file(cfg, Path(p), kb)
            print(f"[✓] 已导入知识库：{r['file_name']}")
            results.append(r)
        except Exception as e:  # noqa: BLE001
            print(f"[!] 导入失败（{Path(p).name}）：{e}")
            results.append({"file_name": Path(p).name, "ok": False, "error": str(e)})
    return results
