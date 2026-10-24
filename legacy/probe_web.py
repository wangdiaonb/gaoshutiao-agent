# -*- coding: utf-8 -*-
"""诊断脚本：一次性回答三个问题

1. 首页 / 搜索页的卡片能不能抓到、滚动有没有生效、累计多少条
2. 卡片上有没有任何广告标识或商业线索
3. 详情页能不能拿到正文、话题、评论（这决定了能不能用大模型判定商业性）

用法：
  python probe.py            # 桌面端，跑首页+搜索+2条详情
  python probe.py --mobile   # 移动端 UA（已确认不可用，仅留作复核）
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import sys
from pathlib import Path

import argparse
from datetime import datetime

from core import config as C
from core.config import RAW_DIR
from core.xhs_crawler import DOM_EXTRACT_JS, XhsCrawler

AD_WORDS = ["赞助", "广告", "品牌合作", "商业推广"]
_lines: list[str] = []


def out(msg: str = "") -> None:
    print(msg)
    _lines.append(msg)


def scan_page(crawler: XhsCrawler, url: str, name: str, api_key: str,
              scrolls: int = 6) -> list[dict]:
    page = crawler.page
    out(f"\n{'=' * 56}\n【{name}】{url}\n{'=' * 56}")
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception as e:  # noqa: BLE001
        out(f"  打开失败: {e}")
        return []
    page.wait_for_timeout(3500)

    reason = crawler._block_reason()
    if reason:
        out(f"  [!] 页面被拦截: {reason}")
        return []

    try:
        b0 = page.inner_text("body")
        if "广告屏蔽" in b0 or "插件白名单" in b0:
            out("  [!] 出现「检测到广告屏蔽插件」提示 —— 可能影响广告位展示")
    except Exception:
        pass

    uniq: dict[str, dict] = {}
    lab: dict[str, dict] = {}
    com: dict[str, dict] = {}
    api_items = api_matched = 0
    indice: list[int] = []

    for i in range(scrolls):
        moved = crawler._human_scroll()
        page.wait_for_timeout(1800)

        got = crawler._drain_responses(api_key)
        api_matched += crawler.last_api_matched
        for p in got:
            try:
                api_items += len((p.get("data") or {}).get("items") or [])
            except Exception:
                pass

        try:
            cards = page.evaluate(DOM_EXTRACT_JS)
        except Exception as e:  # noqa: BLE001
            out(f"  DOM 失败: {e}")
            cards = []
        for c in cards:
            uniq.setdefault(c["note_id"], c)
            if c.get("labels"):
                lab.setdefault(c["note_id"], c)
            if c.get("commercial"):
                com.setdefault(c["note_id"], c)

        try:
            idx = page.evaluate(
                "() => Array.from(document.querySelectorAll('section.note-item'))"
                ".map(e => Number(e.dataset.index)).filter(n => !isNaN(n))")
            if idx:
                indice = idx
        except Exception:
            pass

        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        cnt = {w: body.count(w) for w in AD_WORDS}
        out(f"  第{i + 1}滚 | 位移{int(moved)}px | 本屏{len(cards)} | 累计唯一{len(uniq)} | "
            f"接口{api_matched}次/{api_items}条 | 标识{len(lab)} | 线索{len(com)} | {cnt}")
        if moved <= 0:
            out("     ↑ 位移 0，这一滚没生效")

    out(f"\n  ── 小结 ──")
    out(f"  累计唯一笔记: {len(uniq)} 条")
    out(f"  虚拟列表索引范围: {min(indice) if indice else '-'} ~ {max(indice) if indice else '-'}")
    out(f"  接口命中: {api_matched} 次，解析 {api_items} 条")
    out(f"  带广告标识: {len(lab)} 条")
    out(f"  带商业线索元素: {len(com)} 条")
    if not lab and not com:
        out("  → 卡片层面没有任何广告/商业标识，只能靠详情页正文判定")

    try:
        path = RAW_DIR / f"probe_{name}_{datetime.now():%H%M%S}.html"
        path.write_text(page.content(), encoding="utf-8")
        out(f"  页面已存: {path.name}")
    except Exception:
        pass
    return list(uniq.values())


def scan_detail(crawler: XhsCrawler, card: dict) -> None:
    page = crawler.page
    out(f"\n{'-' * 56}\n【详情页】{card['title'][:30]}\n  {card['href'][:100]}")
    try:
        page.goto(card["href"], wait_until="domcontentloaded")
    except Exception as e:  # noqa: BLE001
        out(f"  打开失败: {e}")
        return
    page.wait_for_timeout(4000)

    try:
        info = page.evaluate(r"""() => {
          const pick = (sels) => {
            for (const s of sels) {
              const el = document.querySelector(s);
              if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
            }
            return '';
          };
          const body = document.body.innerText || '';
          return {
            url: location.href,
            title: pick(['#detail-title', '.note-content .title', 'h1']),
            desc: pick(['#detail-desc .note-text', '#detail-desc', '.note-content .desc']),
            author: pick(['.author-container .username', '.author-wrapper .username']),
            tags: Array.from(document.querySelectorAll('#detail-desc a.tag, a[href*="search_result"]'))
                    .map(a => (a.innerText || '').trim()).filter(t => t.startsWith('#')).slice(0, 12),
            commentCount: document.querySelectorAll('.comment-item, .parent-comment').length,
            hasGoods: !!document.querySelector('[class*=goods], [class*=product], [class*=shop]'),
            head: body.slice(0, 400)
          };
        }""")
    except Exception as e:  # noqa: BLE001
        out(f"  提取失败: {e}")
        return

    out(f"  最终URL: {info['url'][:90]}")
    out(f"  标题: {info['title'][:40] or '（空）'}")
    out(f"  正文长度: {len(info['desc'])}　前80字: {info['desc'][:80] or '（空）'}")
    out(f"  作者: {info['author'] or '（空）'}　话题 {len(info['tags'])} 个")
    out(f"  评论条数: {info['commentCount']}　商品元素: {info['hasGoods']}")
    out(f"  页面开头含广告词: { {w: info['head'].count(w) for w in AD_WORDS} }")

    try:
        path = RAW_DIR / f"probe_detail_{card['note_id'][:12]}_{datetime.now():%H%M%S}.html"
        path.write_text(page.content(), encoding="utf-8")
        out(f"  详情页已存: {path.name}")
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mobile", action="store_true", help="iPhone UA（已知不可用）")
    a = ap.parse_args()

    cfg = C.load_config()
    cfg["crawl"]["headless"] = False
    if a.mobile:
        cfg["crawl"]["mobile"] = True

    crawler = XhsCrawler(cfg)
    try:
        crawler.start()
        if not crawler.ensure_login():
            out("[x] 登录未完成")
            return
        tag = "移动端" if a.mobile else "桌面端"
        scan_page(crawler, "https://www.xiaohongshu.com/explore",
                  f"{tag}首页", "/api/sns/web/v1/homefeed", scrolls=4)
        cards = scan_page(crawler,
                          "https://www.xiaohongshu.com/search_result?keyword=送礼"
                          "&source=web_explore_feed&type=51",
                          f"{tag}搜索", "/api/sns/web/v1/search/notes", scrolls=4)
        for c in cards[:2]:
            scan_detail(crawler, c)
    finally:
        crawler.close()

    report = RAW_DIR / "probe_report.txt"
    report.write_text("\n".join(_lines), encoding="utf-8")
    print(f"\n报告已写入 {report}")


if __name__ == "__main__":
    main()
