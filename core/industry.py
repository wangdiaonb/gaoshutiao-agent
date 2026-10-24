# -*- coding: utf-8 -*-
"""行业拆解：把一个行业拆成商业链路的各个环节，并为每个环节生成搜索词

输入：行业名（如「跨境电商」）
输出：
{
  "industry": "跨境电商",
  "segments": [
    {"name": "物流货代", "desc": "...", "keywords": ["跨境物流 推荐", "货代 靠谱"]},
    ...
  ]
}

计划会缓存到 data/industry_plan_<行业>.json —— 可以人工编辑后再跑，
省掉模型调用，也方便你调整环节和搜索词。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config as C

PLAN_PROMPT = """你是行业研究与广告投放专家。请把「{industry}」这个行业的商业链路拆解成若干环节。

要求：
1. 拆出 {lo}~{hi} 个环节，覆盖完整链路（从上游供给、工具服务，到中游运营，再到下游获客/履约），
   每个环节要是这个行业里真实会花钱采购或投放的东西；
2. 每个环节给 3~5 个搜索词，必须是**小红书用户真实会输入的短句**（口语化、可带品类词），
   不要生硬行业术语；这些词用来搜该环节的广告笔记；
3. 环节名称控制在 8 字以内，desc 一句话说明该环节在行业里干什么；
4. 只输出 JSON，不要解释：
{{"segments":[{{"name":"环节名","desc":"一句话说明","keywords":["词1","词2"]}}]}}"""


def _slug(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fa5]+", "_", name).strip("_") or "industry"


def plan_path(name: str) -> Path:
    return C.DATA_DIR / f"industry_plan_{_slug(name)}.json"


def build_plan(cfg: C.Cfg, industry: str, lo: int = 6, hi: int = 12,
               use_cache: bool = True) -> dict:
    """生成（或读取缓存的）行业链路拆解"""
    industry = (industry or "").strip()
    if not industry:
        raise ValueError("行业名不能为空")

    p = plan_path(industry)
    if use_cache and p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            segs = data.get("segments") or []
            if segs:
                print(f"[✓] 读取行业拆解缓存：{industry} · {len(segs)} 个环节")
                return data
        except Exception:
            pass

    from .analyzer import _extract_json, call_llm

    print(f"[·] 正在拆解行业链路：{industry}")
    raw = call_llm(cfg, PLAN_PROMPT.format(industry=industry, lo=lo, hi=hi),
                   temperature=0.4)
    data = _extract_json(raw)
    segs = []
    for s in (data.get("segments") or []):
        name = str(s.get("name") or "").strip()
        kws = [str(k).strip() for k in (s.get("keywords") or []) if str(k).strip()]
        if name and kws:
            segs.append({"name": name[:12], "desc": str(s.get("desc") or "").strip(),
                         "keywords": kws[:3]})
    if not segs:
        raise RuntimeError(f"行业拆解失败，模型没有返回有效环节：{raw[:200]}")

    plan = {"industry": industry, "segments": segs}
    try:
        p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    print(f"[✓] 拆出 {len(segs)} 个环节：{' / '.join(s['name'] for s in segs)}")
    return plan


def load_or_build(cfg: C.Cfg, industry: str | None = None) -> dict:
    name = industry or cfg.get_path("industry.name", "")
    lo = int(cfg.get_path("industry.segments_min", 6))
    hi = int(cfg.get_path("industry.segments_max", 12))
    return build_plan(cfg, name, lo, hi)


EXPAND_PROMPT = """行业「{industry}」已拆出下列环节，每个环节已有若干搜索词。

请为**每个环节**再补充 {add} 个**不同角度**的搜索词，要求：
1. 不要与已有词重复或近似；
2. 更像真实用户的说法：可以换场景、换人群、换痛点、换平台名、换品类；
3. 依然要能搜到该环节的商业内容；
4. 只输出 JSON：{{"segments": {{"环节名": ["新词1","新词2"]}}}}
   每个环节都必须出现。

已有环节与搜索词：
{current}"""


def expand_keywords(cfg: C.Cfg, plan: dict, add: int = 2) -> dict:
    """保留环节结构，为每个环节补充搜索词"""
    from .analyzer import _extract_json, call_llm

    segs = plan.get("segments") or []
    if not segs:
        return plan
    current = {s["name"]: s.get("keywords", []) for s in segs}
    raw = call_llm(cfg, EXPAND_PROMPT.format(
        industry=plan.get("industry", ""), add=add,
        current=json.dumps(current, ensure_ascii=False)), temperature=0.5)
    got = _extract_json(raw).get("segments") or {}

    total_new = 0
    for s in segs:
        extra = [str(k).strip() for k in (got.get(s["name"]) or []) if str(k).strip()]
        have = {k.lower() for k in s["keywords"]}
        fresh = [k for k in extra if k.lower() not in have][:add]
        if fresh:
            s["keywords"] = list(dict.fromkeys(s["keywords"] + fresh))
            total_new += len(fresh)

    print(f"[✓] 关键词扩充：新增 {total_new} 个，"
          f"平均每环节 {sum(len(s['keywords']) for s in segs) / len(segs):.1f} 个")
    p = plan_path(plan.get("industry", ""))
    try:
        p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return plan


CHECK_PROMPT = """行业：{industry}
环节：{segment}（{desc}）

下面是若干条小红书广告笔记。请判断每条广告的投放方**是否真的在卖这个环节的产品/服务**，
而不是被关键词顺带搜进来的其它行业广告。

{items}

判断标准（宽松）：
- 只要广告主是在为这个环节提供产品/服务/工具/咨询/方法论课程，就算匹配；
- 只有当广告明显属于**其它环节或其它行业**时才判不匹配。
  例如环节是「广告投流」，那么卖 AI 营销工具、广告素材工具、投放代运营的 **都算匹配**；
  但卖奶粉、卖留学申请、卖衣服的显然不算。
- 宁可漏杀，不要错杀。
只输出 JSON：{{"results":[{{"i":0,"match":true,"reason":"10字内理由"}}]}}"""


def check_segments(cfg: C.Cfg, items: list[dict], plan: dict,
                   batch: int = 12) -> list[dict]:
    """校验每条广告是否属于它被分配的环节，打上 segment_match 标记"""
    from .analyzer import _extract_json, call_llm

    industry = plan.get("industry", "")
    by_seg: dict[str, list[dict]] = {}
    for it in items:
        by_seg.setdefault(it.get("segment") or "", []).append(it)

    desc_map = {s["name"]: s.get("desc", "") for s in plan.get("segments", [])}
    dropped = 0
    for seg, group in by_seg.items():
        if not seg:
            continue
        for i in range(0, len(group), batch):
            chunk = group[i:i + batch]
            lines = []
            for n, it in enumerate(chunk):
                body = (it.get("desc") or "")[:120].replace("\n", " ")
                lines.append(f'{n}. 标题：{(it.get("title") or "")[:50]}｜正文：{body}')
            try:
                raw = call_llm(cfg, CHECK_PROMPT.format(
                    industry=industry, segment=seg, desc=desc_map.get(seg, ""),
                    items="\n".join(lines)), temperature=0.1)
                res = _extract_json(raw).get("results") or []
                verdict = {int(r.get("i", -1)): r for r in res if str(r.get("i", "")).lstrip("-").isdigit()}
                for n, it in enumerate(chunk):
                    r = verdict.get(n)
                    if r is None:
                        it["segment_match"] = True
                        continue
                    ok = bool(r.get("match"))
                    it["segment_match"] = ok
                    it["segment_reason"] = str(r.get("reason") or "")[:16]
                    if not ok:
                        dropped += 1
            except Exception as e:  # noqa: BLE001
                print(f"[!] 环节校验失败（{seg}）: {e}")
                for it in chunk:
                    it["segment_match"] = True

    print(f"[✓] 环节校验：剔除 {dropped} 条不属于该环节的广告")
    return items
