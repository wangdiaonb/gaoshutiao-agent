# -*- coding: utf-8 -*-
"""品牌名归一：把「maxicosi」「迈可适（maxicosi）」「迈可适」合并成同一个品牌

两段式：
1. 规则清洗 —— 去括号、去后缀（旗舰店/官方/专营店…）、统一大小写
2. 模型归并 —— 清洗后仍不同的写法（中英文、简称、错字）交给模型判断是否同一品牌

归并结果缓存在 data/brand_alias.json，只在出现新名字时才调用模型。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config as C

CACHE = C.DATA_DIR / "brand_alias.json"

DROP_SUFFIX = ("官方旗舰店", "旗舰店", "官方店", "专卖店", "专营店", "品牌号", "官方",
               "中国", "有限公司", "股份", "集团", "inc", "ltd", "co", "official")
BRACKETS = re.compile(r"[（(\[【].*?[）)\]】]")


def clean(name: str) -> str:
    s = (name or "").strip()
    s = BRACKETS.sub("", s)          # 「迈可适（maxicosi）」-> 「迈可适」
    s = re.sub(r"\s+", "", s)
    low = s.lower()
    for suf in DROP_SUFFIX:
        if low.endswith(suf) and len(s) > len(suf) + 1:
            s = s[: -len(suf)]
            low = s.lower()
    return s.strip("·-—_ ®™")


def _load() -> dict[str, str]:
    try:
        if CACHE.exists():
            return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save(cache: dict[str, str]) -> None:
    try:
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


PROMPT = """下面是小红书广告笔记里识别出的投放品牌名，存在同一品牌的不同写法
（中英文、简称、括号注释、错字等）。

请把它们归并为规范品牌名，规则：
1. 同一品牌只用最常见的那个写法；
2. 看不出是同一品牌的就各自保留；
3. 只输出 JSON：{{"map": {{"原始写法": "规范名"}}}}
每个原始写法都必须出现在 map 里。

品牌列表：
{names}"""


def _llm_merge(cfg: C.Cfg, names: list[str]) -> dict[str, str]:
    from .analyzer import _extract_json, call_llm

    if not names:
        return {}
    batch = 80
    out: dict[str, str] = {}
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        try:
            raw = call_llm(cfg, PROMPT.format(names=json.dumps(chunk, ensure_ascii=False)),
                           temperature=0.1)
            m = _extract_json(raw).get("map") or {}
            for n in chunk:
                v = str(m.get(n) or "").strip()
                out[n] = v if v and len(v) <= 30 else n
        except Exception as e:  # noqa: BLE001
            print(f"[!] 品牌归并失败，保留原名: {e}")
            for n in chunk:
                out[n] = n
    return out


def build_alias(cfg: C.Cfg, names: list[str], use_llm: bool = True) -> dict[str, str]:
    """返回 {原始品牌名: 规范品牌名}，带磁盘缓存"""
    cache = _load()
    raw = sorted({(n or "").strip() for n in names if (n or "").strip()})
    cleaned = {n: clean(n) for n in raw}

    # 已知的直接用缓存
    pending = sorted({c for c in cleaned.values() if c and c not in cache})
    if pending and use_llm:
        print(f"[·] 品牌归并：{len(pending)} 个新品牌名待判定")
        cache.update(_llm_merge(cfg, pending))
        _save(cache)
    for c in cleaned.values():
        cache.setdefault(c, c)
    _save(cache)

    return {n: cache.get(cleaned[n], cleaned[n]) or n for n in raw}


def normalize_items(cfg: C.Cfg, items: list[dict]) -> list[dict]:
    names = [(it.get("analysis", {}) or {}).get("advertiser", "") for it in items]
    alias = build_alias(cfg, names)
    changed = 0
    for it in items:
        a = it.get("analysis") or {}
        old = (a.get("advertiser") or "").strip()
        if not old:
            continue
        new = alias.get(old, old)
        if new and new != old:
            a["advertiser"] = new
            a["advertiser_raw"] = old
            changed += 1
    if changed:
        print(f"[✓] 品牌归一：修正 {changed} 条的品牌写法")
    return items


def dedupe_by_advertiser(items: list[dict], max_per: int = 1) -> list[dict]:
    """同一广告主只保留最多 max_per 条（按参考分优先）

    Ozon 这类投放会写出很多篇不同的笔记，但本质是同一次广告，报告里只该出现一次。
    """
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    keep: list[dict] = []
    for it in items:
        adv = ((it.get("analysis") or {}).get("advertiser") or "").strip()
        key = clean(adv) if adv else ""
        if not key:
            keep.append(it)
            continue
        groups[key].append(it)

    dropped = 0
    for key, lst in groups.items():
        lst.sort(key=lambda x: (x.get("analysis", {}).get("score") or 0), reverse=True)
        keep.extend(lst[:max_per])
        dropped += max(0, len(lst) - max_per)

    if dropped:
        print(f"[✓] 广告主去重：合并 {dropped} 条同广告主的重复笔记")
    return sorted(keep, key=lambda x: (x.get("analysis", {}).get("score") or 0), reverse=True)


def _author_key(name: str) -> str:
    a = re.sub(r"[\s（）()【】《》「」·\-_]+", "", (name or "").lower())
    return re.sub(r"[^\w\u4e00-\u9fa5]", "", a)[:20]


def dedupe_by_author(items: list[dict], max_per: int = 1) -> list[dict]:
    """同一作者只保留最多 max_per 条：一个号连发多条，多半是同一次投放"""
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    keep: list[dict] = []
    for it in items:
        k = _author_key(it.get("author", ""))
        if len(k) < 2:
            keep.append(it)
            continue
        groups[k].append(it)

    dropped = 0
    for k, lst in groups.items():
        lst.sort(key=lambda x: (x.get("analysis", {}).get("score") or 0), reverse=True)
        keep.extend(lst[:max_per])
        dropped += max(0, len(lst) - max_per)
    if dropped:
        print(f"[✓] 作者去重：合并 {dropped} 条同作者笔记")
    return sorted(keep, key=lambda x: (x.get("analysis", {}).get("score") or 0), reverse=True)
