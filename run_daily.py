# -*- coding: utf-8 -*-
"""每日主流程：抓取 -> 分析 -> 生成日报 -> 导入 IMA 知识库

用法：
  python run_daily.py                # 正式跑一次
  python run_daily.py --demo         # 用示例数据跑通全流程（不联网、不需要 key）
  python run_daily.py --limit 30     # 临时改抓取条数
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date

from core import analyzer, brand, demo, report
from core import config as C
from core import db


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="小红书广告洞察日报")
    ap.add_argument("--config", default=None, help="配置文件路径")
    ap.add_argument("--demo", action="store_true", help="示例数据模式")
    ap.add_argument("--limit", type=int, default=None, help="覆盖抓取条数")
    ap.add_argument("--source", choices=["web", "android"], default=None,
                    help="抓取来源：web=网页版(无广告标签) android=App端(有广告标签)")
    ap.add_argument("--industry", default=None,
                    help='行业名，如 "跨境电商"：拆解行业链路后逐环节采集（每环节默认 5 条）')
    ap.add_argument("--per-keyword", type=int, default=None,
                    help="每个关键词目标新增条数（默认 5）")
    ap.add_argument("--per-segment", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--no-dedup", action="store_true", help="不过滤近 2 天已推过的笔记")
    args = ap.parse_args()
    return args


def run(args: argparse.Namespace) -> int:
    cfg = C.load_config(args.config)
    conn = db.connect()
    today = date.today().isoformat()
    industry_ctx = None

    # ---------------- 1. 数据 ----------------
    if args.demo:
        print("[·] 示例数据模式（不联网、不调用模型）")
        items = demo.build(args.limit or 100)
        mapping = {k: {} for k in analyzer.KINDS}
        keywords_used = {"示例关键词"}
        stats = {"count": len(items), "keywords": len(keywords_used)}
    else:
        source = args.source or cfg.get_path("crawl.source", "web")
        target = args.limit or int(cfg.get_path("crawl.target_count", 100))

        if source == "android":
            from core.android_crawler import AndroidCrawler

            # 自动拉起模拟器并打开小红书
            if bool(cfg.get_path("emulator.auto_start", True)):
                from core import emulator as emu
                try:
                    if not emu.prepare(cfg):
                        print("[x] 小红书未登录：请在模拟器里用手机扫码登录一次，之后就能全自动")
                        return 8
                except emu.EmuError as e:
                    print(f"[x] 模拟器准备失败：{e}")
                    print("    可手动启动 MuMu 后重跑，或在 config.yaml 关掉 emulator.auto_start")
                    return 9

            ac = AndroidCrawler(cfg)
            try:
                ac.connect()
            except RuntimeError as e:
                print(f"[x] {e}")
                return 7

            industry_name = (args.industry or cfg.get_path("industry.name", "") or "").strip()
            if industry_name:
                from core import industry as ind

                plan = ind.load_or_build(cfg, industry_name)
                per = (args.per_keyword or args.per_segment
                       or int(cfg.get_path("industry.per_keyword", 5)))
                notes = ac.collect_by_industry(plan, per)
                keywords_used = {sg["name"] for sg in plan["segments"]}
                industry_ctx = {"industry": industry_name,
                                "segments_all": plan["segments"], "plan": plan}
            else:
                notes = ac.collect(target)
                keywords_used = {"App发现页"}
        else:
            from core.xhs_crawler import XhsCrawler

            keywords = cfg.get_path("crawl.keywords", []) or []
            if not keywords:
                print("[x] 配置里 crawl.keywords 为空")
                return 2

            crawler = XhsCrawler(cfg)
            try:
                crawler.start()
                if not crawler.ensure_login():
                    if crawler.last_block == "ip_risk":
                        print("[x] 当前网络 IP 被小红书风控（300012），本轮中止")
                        print("    换到自己的宽带、关掉代理后重试；详见 README「排错」")
                        return 6
                    print("[x] 登录失败，退出。可先运行 python login_xhs.py 单独登录")
                    return 3
                notes = crawler.collect(keywords, target)
            finally:
                crawler.close()
            keywords_used = {n.get("keyword") for n in notes}

        if not notes:
            print("[x] 没抓到笔记。可能原因：登录态失效 / 风控 / 关键词无结果 / 广告判定过严")
            return 4

        # 去掉近 2 天已推过的，避免重复
        seen = set() if args.no_dedup else db.recent_note_ids(conn, days=2)
        fresh = [n for n in notes if n["note_id"] not in seen]
        if len(fresh) < len(notes):
            print(f"[·] 过滤重复，{len(notes)} -> {len(fresh)}")
        items_raw = fresh or notes
        for n in items_raw:
            db.upsert_note(conn, n)

        # 采集成功即清断点：否则下次会把这次的数据当成"续采结果"重复带进来
        try:
            _pf = C.DATA_DIR / "android_partial.json"
            if _pf.exists():
                _pf.unlink()
        except Exception:
            pass

        # ---------------- 2. 分析 ----------------
        try:
            items = analyzer.analyze_all(cfg, conn, items_raw)
        except analyzer.LLMError as e:
            print(f"[x] 分析失败：{e}")
            return 5

        items = brand.normalize_items(cfg, items)
        items = brand.dedupe_by_advertiser(
            items, int(cfg.get_path("industry.max_per_advertiser", 1)))
        items = brand.dedupe_by_author(
            items, int(cfg.get_path("industry.max_per_author", 1)))

        # 环节归属校验：剔掉被关键词顺带搜进来的其它行业广告
        if industry_ctx and industry_ctx.get("plan"):
            from core import industry as _ind
            items = _ind.check_segments(cfg, items, industry_ctx["plan"])
            keep = [i for i in items if i.get("segment_match", True)]
            if len(keep) < len(items):
                print(f"[·] 剔除环节不符 {len(items) - len(keep)} 条，剩 {len(keep)} 条")
            items = keep
        mapping = analyzer.merge_labels(cfg, items)
        keywords_used = {n.get("keyword") for n in items if n.get("keyword")}
        stats = {"count": len(items), "keywords": len(keywords_used)}

    agg = analyzer.aggregate(items, mapping)
    stats["count"] = len(items)
    if industry_ctx:
        stats.update(industry_ctx)

    # ---------------- 3. 报告 ----------------
    html_path = C.REPORT_DIR / f"小红书广告日报_{today}.html"
    csv_path = C.REPORT_DIR / f"小红书广告明细_{today}.csv"
    md_path = C.REPORT_DIR / f"小红书广告日报_{today}.md"
    report.write_html(items, agg, stats, html_path)
    report.write_csv(items, csv_path)
    report.write_markdown(items, stats, md_path)
    print(f"[✓] 报告：{html_path}")
    print(f"[✓] 明细：{csv_path}")
    print(f"[✓] 摘要：{md_path}")

    # ---------------- 4. 导入 IMA 知识库 ----------------
    try:
        from core import ima
        ima.upload_reports(cfg, [md_path, csv_path])
    except Exception as e:  # noqa: BLE001
        print(f"[!] IMA 导入异常（不影响主流程）：{e}")

    # 终端摘要
    print("\n" + "=" * 52)
    print(f"今日 {len(items)} 条｜人群 {len(agg.get('audience', []))} 类 "
          f"｜场景 {len(agg.get('scenario', []))} 类｜需求 {len(agg.get('need', []))} 类")
    for kind, name in (("audience", "人群"), ("scenario", "场景"), ("need", "需求")):
        top = agg.get(kind, [])[:3]
        print(f"{name} TOP3: " + " / ".join(f"{r['label']}({r['count']})" for r in top))
    print("=" * 52)
    return 0


def main() -> None:
    args = parse_args()
    try:
        sys.exit(run(args))
    except KeyboardInterrupt:
        print("\n[·] 已中断")
        sys.exit(130)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
