# -*- coding: utf-8 -*-
"""广告洞察分析：调用大模型抽取 特殊人群 / 特殊场景 / 特殊需求

两段式：
1. 逐条抽取（并发）—— 得到原始标签 + 原文依据
2. 标签归并（一次调用）—— 把上百个近义标签收敛成 ≤12 个标准标签，才能统计
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

import requests

from . import config as C
from . import db

SYSTEM_PROMPT = "你是资深广告投放策略师与消费者洞察分析师，擅长从小红书商业笔记里反推投放逻辑。只输出 JSON。"

ANALYZE_PROMPT = """下面是一条小红书笔记（可能为商业推广内容），请做广告洞察拆解。

【标题】{title}
【正文】{desc}
【话题】{tags}
【作者】{author}
【点赞】{liked}
【热评】{comments}

请输出严格 JSON（不要 markdown 代码块、不要解释）：
{{
  "is_commercial": true 或 false,
  "advertiser": "投放品牌名，看不出来就填空字符串",
  "industry": "所属行业，从这些里选一个：美妆个护/食品饮料/母婴亲子/3C数码/家居家装/服饰鞋包/医药健康/教育培训/本地生活/汽车出行/宠物/其他",
  "audience": [{{"label": "人群标签，12字以内", "evidence": "原文依据，20字以内"}}],
  "scenario": [{{"label": "使用或购买场景，12字以内", "evidence": "原文依据，20字以内"}}],
  "need": [{{"label": "被击中或未被满足的需求，12字以内", "evidence": "原文依据，20字以内"}}],
  "hook": "这条广告的情绪钩子/说服逻辑，30字以内",
  "angle": "可复用的投放切入角度，30字以内",
  "score": 1到5的整数，代表对广告从业者的参考价值（5=创意或洞察很强）
}}
要求：audience / scenario / need 各 1-3 条，确实没有就给空数组；label 要具体，不要写"年轻人""女性"这种废话。"""

MERGE_PROMPT = """下面是小红书广告笔记里抽取出的「{kind}」原始标签（JSON 数组）。
请把它们归并成不超过 {limit} 个标准标签，合并同义/近义项，保留区分度高的。

原始标签：{labels}

只输出 JSON：{{"map": {{"原始标签": "标准标签"}}}}
每个原始标签都必须出现在 map 里。"""


class LLMError(RuntimeError):
    pass


def _endpoint(cfg: C.Cfg) -> str:
    base = (cfg.get_path("llm.base_url") or "").rstrip("/")
    if not base:
        raise LLMError("未配置 llm.base_url")
    return base + "/chat/completions"


def call_llm(cfg: C.Cfg, prompt: str, system: str = SYSTEM_PROMPT,
             temperature: float = 0.3) -> str:
    key = cfg.get_path("llm.api_key") or ""
    if not key:
        raise LLMError("未配置 llm.api_key，无法做分析")
    payload = {
        "model": cfg.get_path("llm.model", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    timeout = int(cfg.get_path("llm.timeout", 90))
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(
                _endpoint(cfg),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if r.status_code >= 400:
                raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt == 2:
                break
    raise LLMError(str(last_err))


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    raise ValueError(f"无法解析模型返回的 JSON: {text[:160]}")


def _note_prompt(note: dict) -> str:
    comments = note.get("comments") or []
    return ANALYZE_PROMPT.format(
        title=(note.get("title") or "")[:120],
        desc=(note.get("desc") or note.get("_body_text") or "")[:900],
        tags="、".join((note.get("tags") or [])[:12]),
        author=note.get("author") or "",
        liked=note.get("liked") or "",
        comments=" | ".join(comments[:8]) or "无",
    )


def analyze_one(cfg: C.Cfg, note: dict) -> dict:
    raw = call_llm(cfg, _note_prompt(note))
    data = _extract_json(raw)
    data.setdefault("audience", [])
    data.setdefault("scenario", [])
    data.setdefault("need", [])
    data.setdefault("score", 3)
    data.setdefault("advertiser", "")
    data.setdefault("industry", "其他")
    data.setdefault("hook", "")
    data.setdefault("angle", "")
    return data


def analyze_all(cfg: C.Cfg, conn, notes: list[dict], use_cache: bool = True) -> list[dict]:
    """并发分析，命中缓存的直接复用"""
    results: dict[str, dict] = {}
    todo: list[dict] = []

    for n in notes:
        cached = db.get_cached_analysis(conn, n["note_id"]) if use_cache else None
        if cached:
            results[n["note_id"]] = cached
        else:
            todo.append(n)

    print(f"[·] 需分析 {len(todo)} 条（缓存命中 {len(results)} 条）")
    if not todo:
        return [{**n, "analysis": results.get(n["note_id"], {})} for n in notes]

    workers = max(1, int(cfg.get_path("llm.concurrency", 4)))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(analyze_one, cfg, n): n for n in todo}
        for fut in as_completed(futures):
            n = futures[fut]
            try:
                data = fut.result()
                results[n["note_id"]] = data
                db.save_analysis(conn, n["note_id"], data, cfg.get_path("llm.model", ""))
                done += 1
                print(f"    [{done}/{len(todo)}] {n['note_id']} ok")
            except Exception as e:  # noqa: BLE001
                print(f"    [!] {n['note_id']} 分析失败: {e}")

    return [{**n, "analysis": results.get(n["note_id"], {})} for n in notes if n["note_id"] in results]


# ------------------------------------------------------------------ 标签归并
KINDS = {"audience": "人群", "scenario": "场景", "need": "需求"}


def merge_labels(cfg: C.Cfg, items: list[dict], conn=None) -> dict[str, dict[str, str]]:
    """返回 {kind: {原始标签: 标准标签}}"""
    out: dict[str, dict[str, str]] = {}
    for kind, cname in KINDS.items():
        labels = sorted({lb.get("label", "").strip()
                         for it in items
                         for lb in (it.get("analysis", {}).get(kind) or [])
                         if lb.get("label")})
        labels = [l for l in labels if l]
        if not labels:
            out[kind] = {}
            continue
        out[kind] = _strict_merge(cfg, items, kind, 12)
    return out


# ------------------------------------------------------------------ 强约束归并
CATEGORIZE_PROMPT = """下面是小红书广告笔记里抽取出的「{kind}」原始标签（可能上百个）。
请归纳出不超过 {limit} 个标准分类，要求：
1. 每个分类用一个简短名词短语命名（不超过 12 字）；
2. 分类要覆盖绝大多数原始标签的语义，不要遗漏常见类型；
3. 只输出 JSON：{{"categories": ["分类1", "分类2", ...]}}"""

MAP_PROMPT = """把下列「{kind}」原始标签逐个映射到给定的标准分类中。

标准分类（只能从这 {n} 个里选，不允许新增、不允许输出"其他"或"未知"）：
{cats}

原始标签：
{labels}

只输出 JSON：{{"map": {{"原始标签": "标准分类"}}}}
每个原始标签都必须出现且必须映射到上面列表里的某一项。"""


def _strict_merge(cfg: C.Cfg, items: list[dict], kind: str, limit: int = 12) -> dict[str, str]:
    labels = sorted({(lb.get("label") or "").strip()
                     for it in items
                     for lb in (it.get("analysis", {}).get(kind) or [])
                     if (lb.get("label") or "").strip()})
    if not labels:
        return {}
    if len(labels) <= limit:
        return {l: l for l in labels}

    # 第一步：定标准分类
    try:
        raw = call_llm(cfg, CATEGORIZE_PROMPT.format(
            kind=KINDS[kind], limit=limit), temperature=0.2)
        cats = [str(c).strip() for c in (_extract_json(raw).get("categories") or []) if str(c).strip()]
        cats = cats[:limit]
    except Exception as e:  # noqa: BLE001
        print(f"[!] {KINDS[kind]}分类归纳失败: {e}")
        cats = []
    if len(cats) < 3:
        # 退化为按频次取前 N 个原始标签当分类
        from collections import Counter
        cnt = Counter()
        for it in items:
            for lb in (it.get("analysis", {}).get(kind) or []):
                if (lb.get("label") or "").strip():
                    cnt[lb["label"].strip()] += 1
        cats = [k for k, _ in cnt.most_common(limit)]

    # 第二步：分批强制映射
    mapping: dict[str, str] = {}
    batch = 40
    for i in range(0, len(labels), batch):
        chunk = labels[i:i + batch]
        try:
            raw = call_llm(cfg, MAP_PROMPT.format(
                kind=KINDS[kind], n=len(cats),
                cats="、".join(cats),
                labels=json.dumps(chunk, ensure_ascii=False)), temperature=0.1)
            m = _extract_json(raw).get("map") or {}
            for lab in chunk:
                v = str(m.get(lab) or "").strip()
                mapping[lab] = v if v in cats else _nearest(lab, cats)
        except Exception as e:  # noqa: BLE001
            print(f"[!] {KINDS[kind]}映射批次失败: {e}")
            for lab in chunk:
                mapping[lab] = _nearest(lab, cats)

    used = sorted(set(mapping.values()))
    print(f"[✓] {KINDS[kind]}标签归并：{len(labels)} -> {len(used)} 类")
    return mapping


def _nearest(label: str, cats: list[str]) -> str:
    """字符重叠度兜底，避免落到空值"""
    if not cats:
        return label
    best, score = cats[0], -1
    for c in cats:
        s = len(set(label) & set(c))
        if s > score:
            best, score = c, s
    return best


def _cap(items: list[dict], kind: str, mapping: dict[str, str],
         limit: int = 12) -> dict[str, str]:
    """模型没收敛到上限时，按频次保留前 limit-1 类，其余并入「其他」"""
    from collections import Counter

    cnt: Counter = Counter()
    for it in items:
        for lb in (it.get("analysis", {}).get(kind) or []):
            raw = (lb.get("label") or "").strip()
            if raw:
                cnt[mapping.get(raw, raw)] += 1
    if len(cnt) <= limit:
        return mapping
    keep = {k for k, _ in cnt.most_common(limit - 1)}
    return {raw: (std if std in keep else "其他") for raw, std in mapping.items()}


def aggregate(items: list[dict], mapping: dict[str, dict[str, str]],
              top_n: int = 12) -> dict[str, list[dict]]:
    """统计每个标准标签命中的笔记数"""
    agg: dict[str, list[dict]] = {}
    mapping = {k: _cap(items, k, mapping.get(k, {})) for k in KINDS}
    for kind in KINDS:
        counter: dict[str, dict] = {}
        for it in items:
            seen = set()
            for lb in (it.get("analysis", {}).get(kind) or []):
                label = lb.get("label", "").strip()
                if not label:
                    continue
                std = mapping.get(kind, {}).get(label, label)
                if std in seen:
                    continue
                seen.add(std)
                slot = counter.setdefault(std, {"label": std, "count": 0,
                                                "evidence": [], "samples": []})
                slot["count"] += 1
                ev = (lb.get("evidence") or "").strip()
                if ev and ev not in slot["evidence"] and len(slot["evidence"]) < 3:
                    slot["evidence"].append(ev)
                if len(slot["samples"]) < 3:
                    slot["samples"].append({
                        "note_id": it["note_id"],
                        "title": it.get("title", "")[:40],
                        "url": it.get("url", ""),
                    })
        ranked = sorted(counter.values(), key=lambda x: x["count"], reverse=True)[:top_n]
        agg[kind] = ranked
    return agg
