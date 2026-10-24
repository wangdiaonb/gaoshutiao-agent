# -*- coding: utf-8 -*-
"""演示数据：不联网、不调模型，直接产出 100 条形态真实的样例，用于验收报告与卡片格式"""
from __future__ import annotations

import random
from datetime import datetime

AUDIENCES = [
    ("25-32岁敏感肌女生", 28, "换季就烂脸，试错成本太高"),
    ("996通勤打工人", 22, "每天地铁两小时，只想省事"),
    ("新手宝妈", 18, "带娃没时间，要能闭眼买"),
    ("预算紧张大学生", 14, "一个月生活费就那么多"),
    ("精致银发族", 9, "不想被当老年人对待"),
    ("健身增肌男生", 7, "想吃又不能乱吃"),
]
SCENARIOS = [
    ("换季烂脸急救", 24, "换季第三天脸颊开始泛红刺痛"),
    ("早八通勤5分钟", 21, "闹钟响三遍才起，没时间化妆"),
    ("深夜加班回血", 18, "凌晨一点还在改方案"),
    ("周末轻露营", 14, "第一次露营不想买一堆装备"),
    ("节庆送礼", 13, "送长辈怕不合心意"),
    ("租房小空间", 8, "房东的厨房只有一平米"),
]
NEEDS = [
    ("不刺激又要有效", 27, "成分表看不懂，怕又过敏"),
    ("一步到位省时间", 23, "步骤超过三步就放弃"),
    ("预算有限但要好用", 20, "学生党想买大牌平替"),
    ("想被理解的情绪价值", 16, "不是想买东西，是想被安慰"),
    ("要真实测评别恰饭", 12, "看多了广告不敢相信"),
]
BRANDS = [
    ("薇诺娜", "美妆个护"), ("珀莱雅", "美妆个护"), ("夸迪", "美妆个护"),
    ("三顿半", "食品饮料"), ("Babycare", "母婴亲子"), ("添可", "家居家装"),
    ("蕉下", "服饰鞋包"), ("SKG", "医药健康"), ("自由点", "母婴亲子"),
    ("网易严选", "家居家装"), ("元气森林", "食品饮料"), ("usmile", "医药健康"),
]
TITLES = [
    "换季烂脸第三天，我把它当急救包用",
    "早八人5分钟出门的秘密，全在这了",
    "凌晨一点改完方案，我只想喝口热的",
    "第一次露营带这一箱就够了，别学我交智商税",
    "送我妈这个，她第一次没说我乱花钱",
    "一平米厨房也能做出像样的饭",
    "敏感肌姐妹别乱买了，我踩过的坑都在这",
    "同事问我是不是偷偷去做了医美",
    "带娃两年，我终于找到不崩溃的办法",
    "月薪5k也配拥有这种质感吗",
    "健身三个月，教练终于没骂我",
    "这个价位做到这样，我有点意外",
]
HOOKS = [
    "先共情痛点再给解法，弱化推销感",
    "用具体时间成本对比制造焦虑，再给捷径",
    "以真实翻车经历建立可信度后反转",
    "用身份认同（我们这类人）圈定同类",
    "制造稀缺与限时，推动即时决策",
]
ANGLES = [
    "痛点场景前置，产品作为道具后置",
    "素人视角横向测评，用排除法衬托主推",
    "把使用门槛降到最低，强调零学习成本",
    "价格锚点对比，突出性价比而非品牌",
    "从结果倒推（我变好了），隐藏过程投入",
]


def build(n: int = 100) -> list[dict]:
    rnd = random.Random(42)
    a_w = [w for _, w, _ in AUDIENCES]
    s_w = [w for _, w, _ in SCENARIOS]
    d_w = [w for _, w, _ in NEEDS]

    notes = []
    for i in range(n):
        brand, industry = rnd.choice(BRANDS)
        aud = rnd.choices(AUDIENCES, weights=a_w, k=1)[0]
        scn = rnd.choices(SCENARIOS, weights=s_w, k=1)[0]
        nd = rnd.choices(NEEDS, weights=d_w, k=1)[0]
        extra_a = rnd.choices(AUDIENCES, weights=a_w, k=1)[0] if rnd.random() < 0.35 else None
        extra_d = rnd.choices(NEEDS, weights=d_w, k=1)[0] if rnd.random() < 0.3 else None

        nid = f"{rnd.getrandbits(96):024x}"
        analysis = {
            "is_commercial": True,
            "advertiser": brand,
            "industry": industry,
            "audience": [{"label": aud[0], "evidence": aud[2]}]
                        + ([{"label": extra_a[0], "evidence": extra_a[2]}] if extra_a else []),
            "scenario": [{"label": scn[0], "evidence": scn[2]}],
            "need": [{"label": nd[0], "evidence": nd[2]}]
                    + ([{"label": extra_d[0], "evidence": extra_d[2]}] if extra_d else []),
            "hook": rnd.choice(HOOKS),
            "angle": rnd.choice(ANGLES),
            "score": rnd.choices([1, 2, 3, 4, 5], weights=[3, 8, 24, 14, 6], k=1)[0],
        }
        notes.append({
            "note_id": nid,
            "title": rnd.choice(TITLES),
            "desc": f"（示例正文）{aud[2]}，{scn[2]}。后来试了{brand}，{nd[2]}这件事算是解决了。",
            "tags": ["#" + rnd.choice(["护肤", "好物分享", "打工人日常", "母婴", "露营", "测评", "送礼"])],
            "author": "示例博主" + str(rnd.randint(1, 99)),
            "liked": f"{rnd.randint(3, 980)}",
            "url": f"https://www.xiaohongshu.com/explore/{nid}",
            "is_ad": True,
            "ad_confidence": 0.93,
            "ad_signals": ["页面标识:赞助"],
            "keyword": "示例关键词",
            "crawled_at": datetime.now().isoformat(timespec="seconds"),
            "analysis": analysis,
        })
    return notes
