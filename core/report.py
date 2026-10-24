# -*- coding: utf-8 -*-
"""生成日报：CSV 明细 + HTML 可视化报告

HTML 版式：顶部全局分布概览，下方为「每条广告一张卡片」，
卡片内直接展开该条笔记的特殊人群 / 特殊场景 / 特殊需求（附原文依据）。
"""
from __future__ import annotations

import csv
import html
from datetime import date
from pathlib import Path

from . import config as C


_ARROW = ('<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">'
          '<path d="M6 3.5L10.5 8L6 12.5" fill="none" stroke="currentColor" '
          'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _esc(x) -> str:
    return html.escape(str(x or ""))


def write_csv(items: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "笔记ID", "标题", "作者", "广告主", "行业", "特殊人群", "特殊场景",
                    "特殊需求", "情绪钩子", "切入角度", "参考分", "点赞", "来源",
                    "判定依据", "链接"])
        for i, it in enumerate(items, 1):
            a = it.get("analysis", {})
            w.writerow([
                i, it.get("note_id", ""), it.get("title", ""), it.get("author", ""),
                a.get("advertiser", ""), a.get("industry", ""),
                " / ".join(lb.get("label", "") for lb in a.get("audience", [])),
                " / ".join(lb.get("label", "") for lb in a.get("scenario", [])),
                " / ".join(lb.get("label", "") for lb in a.get("need", [])),
                a.get("hook", ""), a.get("angle", ""), a.get("score", ""),
                it.get("liked", ""), it.get("keyword", ""),
                "; ".join(it.get("ad_signals", [])[:4]), it.get("url", ""),
            ])
    return path


def _axis(a: dict, kind: str) -> str:
    rows = a.get(kind) or []
    if not rows:
        return '<div class="axis-empty">未识别到</div>'
    out = []
    for r in rows:
        label = (r.get("label") or "").strip()
        if not label:
            continue
        ev = (r.get("evidence") or "").strip()
        out.append(
            f'<div class="lb"><span class="lb-t">{_esc(label)}</span>'
            + (f'<span class="lb-e">{_esc(ev)}</span>' if ev else "")
            + "</div>"
        )
    return "".join(out) or '<div class="axis-empty">未识别到</div>'


def _card(i: int, it: dict) -> str:
    a = it.get("analysis") or {}
    url = (it.get("url") or "").strip()
    _t = (it.get("title") or "（无标题）").strip()
    title_txt = _esc(_t if len(_t) <= 52 else _t[:52] + "…")
    title_html = (f'<a class="ad-title" href="{_esc(url)}" target="_blank">{title_txt}</a>'
                  if url else f'<span class="ad-title">{title_txt}</span>')

    meta = []
    if (it.get("author") or "").strip():
        meta.append(_esc(it["author"]))
    if (it.get("liked") or "").strip():
        meta.append(f"点赞 {_esc(it['liked'])}")
    _author = (it.get("author") or "").strip()
    _adv = (a.get("advertiser") or "").strip()
    if _adv and _adv != _author:
        meta.append(f'<span class="brand">{_esc(_adv)}</span>')
    if (a.get("industry") or "").strip():
        meta.append(_esc(a["industry"]))
    score = a.get("score")

    foot = []
    if (a.get("hook") or "").strip():
        foot.append(f'<div class="foot-item"><span class="foot-k">情绪钩子</span>{_esc(a["hook"])}</div>')
    if (a.get("angle") or "").strip():
        foot.append(f'<div class="foot-item"><span class="foot-k">可复用角度</span>{_esc(a["angle"])}</div>')

    return f"""
    <article class="ad">
      <div class="ad-top">
        <div class="ad-no">{i:02d}</div>
        <div class="ad-body">
          <div class="ad-title-row">{title_html}</div>
          <div class="ad-meta">{' · '.join(meta) or '—'}</div>
        </div>
        {f'<div class="ad-score">参考分 {_esc(score)}</div>' if score else ''}
      </div>
      <div class="ad-axes">
        <div class="axis axis-a"><div class="axis-h">特殊人群</div>{_axis(a, 'audience')}</div>
        <div class="axis axis-s"><div class="axis-h">特殊场景</div>{_axis(a, 'scenario')}</div>
        <div class="axis axis-n"><div class="axis-h">特殊需求</div>{_axis(a, 'need')}</div>
      </div>
      {f'<div class="ad-foot">{"".join(foot)}</div>' if foot else ''}
    </article>"""


def _cards_by_segment(ranked: list[dict], all_segments: list[dict] | None = None) -> str:
    """按行业环节分组，每个环节是一个可折叠的大类（<details>）"""
    if not any((it.get("segment") or "").strip() for it in ranked):
        return "\n".join(_card(i, it) for i, it in enumerate(ranked, 1))

    groups: dict[str, list[dict]] = {}
    for it in ranked:
        groups.setdefault((it.get("segment") or "未分类").strip(), []).append(it)

    seg_names = [s["name"] for s in (all_segments or [])]
    order = list(dict.fromkeys(seg_names + list(groups)))
    desc_map = {s["name"]: s.get("desc", "") for s in (all_segments or [])}

    out, n = [], 0
    for gi, seg in enumerate(order, 1):
        rows = groups.get(seg, [])
        rows = sorted(rows, key=lambda it: (it.get("analysis", {}).get("score") or 0), reverse=True)
        first = " open" if gi == 1 and rows else ""
        if not rows:
            out.append(f"""
    <details class="seg seg-nil">
      <summary>
        <span class="seg-arrow">{_ARROW}</span>
        <span class="seg-no">{gi:02d}</span>
        <span class="seg-name">{_esc(seg)}</span>
        <span class="seg-desc">{_esc(desc_map.get(seg, ''))}</span>
        <span class="seg-n nil">未投放</span>
      </summary>
      <div class="seg-body"><div class="seg-empty">该环节在小红书未采集到广告投放（换了 2~3 个搜索词都没命中「广告」标识）</div></div>
    </details>""")
            continue

        inner = []
        for it in rows:
            n += 1
            inner.append(_card(n, it))
        out.append(f"""
    <details class="seg"{first}>
      <summary>
        <span class="seg-arrow">{_ARROW}</span>
        <span class="seg-no">{gi:02d}</span>
        <span class="seg-name">{_esc(seg)}</span>
        <span class="seg-desc">{_esc(desc_map.get(seg, ''))}</span>
        <span class="seg-n">{len(rows)} 条</span>
      </summary>
      <div class="seg-body">
        {''.join(inner)}
      </div>
    </details>""")
    return "\n".join(out)


def write_html(items: list[dict], agg: dict[str, list[dict]], stats: dict,
               path: Path, top_n: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    def _num(it):
        s = str(it.get("liked", "")).replace("万", "0000").replace("+", "")
        digits = "".join(ch for ch in s if ch.isdigit())
        return int(digits) if digits else 0

    ranked = sorted(items, key=lambda it: (it.get("analysis", {}).get("score") or 0, _num(it)),
                    reverse=True)
    cards = _cards_by_segment(ranked, stats.get("segments_all"))
    industry = (stats.get("industry") or "").strip()
    seg_n = len({(it.get("segment") or "").strip() for it in ranked if (it.get("segment") or "").strip()})
    if industry:
        total_seg = len(stats.get("segments_all") or []) or seg_n
        sub_head = (f"{industry} · {seg_n}/{total_seg} 个环节有投放 · "
                    f"共 {len(ranked)} 条广告笔记")
    else:
        sub_head = f"共 {len(ranked)} 条广告笔记"

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小红书广告洞察日报 {today}</title>
<style>
  :root {{
    --bg:#f4f5f7; --card:#ffffff; --line:#e8eaee; --text:#16181d;
    --sub:#6b7280; --mute:#9aa1ab; --red:#e8453c;
    --blue:#2f6feb; --green:#0f9d58; --amber:#b45309;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    font-size:14px; line-height:1.6; }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:34px 20px 64px; }}
  header {{ margin-bottom:26px; }}
  h1 {{ font-size:25px; margin:0 0 5px; letter-spacing:-.3px; }}
  .sub {{ color:var(--sub); font-size:13px; }}
  .sec-head {{ display:flex; align-items:baseline; gap:10px; margin:0 0 12px; }}
  .sec-head h2 {{ font-size:16px; margin:0; font-weight:500; }}
  .sec-head span {{ color:var(--mute); font-size:12.5px; }}
  .seg {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
    margin-bottom:11px; overflow:hidden; }}
  .seg > summary {{ list-style:none; cursor:pointer; display:flex; align-items:center;
    gap:10px; padding:15px 18px; }}
  .seg > summary::-webkit-details-marker {{ display:none; }}
  .seg > summary:hover {{ background:#fafbfc; }}
  .seg-arrow {{ color:#b9bec7; display:flex; transition:transform .18s ease; }}
  .seg[open] .seg-arrow {{ transform:rotate(90deg); color:var(--red); }}
  .seg-no {{ color:#c9ced6; font-size:12px; }}
  .seg-name {{ font-size:15px; font-weight:500; }}
  .seg-desc {{ color:var(--mute); font-size:12px; flex:1; min-width:0;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .seg-n {{ font-size:12px; color:var(--sub); background:#f1f3f6;
    padding:1px 9px; border-radius:20px; flex:none; }}
  .seg-n.nil {{ color:#c1c5cc; background:#fafbfc; }}
  .seg-nil summary {{ cursor:pointer; }}
  .seg-nil .seg-name {{ color:#8b9199; }}
  .seg-body {{ padding:6px 14px 16px; background:#fafbfc; border-top:1px solid var(--line); }}
  .seg-empty {{ background:#fff; border:1px dashed var(--line); border-radius:10px;
    padding:14px 16px; color:var(--mute); font-size:12.5px; }}
  .seg-tools {{ display:flex; gap:8px; margin-left:auto; }}
  .seg-tools button {{ font:inherit; font-size:12px; color:var(--sub); background:var(--card);
    border:1px solid var(--line); border-radius:8px; padding:4px 11px; cursor:pointer; }}
  .seg-tools button:hover {{ color:var(--text); border-color:#cfd4da; }}
  .ad {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:16px 18px; margin-bottom:13px; break-inside:avoid; }}
  .ad-top {{ display:flex; gap:12px; align-items:flex-start; }}
  .ad-no {{ width:28px; height:28px; flex:none; border-radius:8px; background:#fff1f0;
    color:var(--red); font-size:12px; font-weight:500; display:flex;
    align-items:center; justify-content:center; }}
  .ad-body {{ flex:1; min-width:0; }}
  .ad-title-row {{ font-size:15px; line-height:1.45; }}
  .ad-title {{ color:var(--text); font-weight:500; text-decoration:none; }}
  a.ad-title:hover {{ color:var(--red); }}
  .ad-meta {{ color:var(--sub); font-size:12px; margin-top:3px; }}
  .brand {{ color:var(--red); }}
  .ad-score {{ flex:none; font-size:11.5px; color:var(--amber); background:#fff6e8;
    padding:2px 9px; border-radius:20px; }}
  .ad-axes {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px;
    margin-top:13px; padding-top:13px; border-top:1px dashed #ebedf0; }}
  @media(max-width:820px) {{ .ad-axes {{ grid-template-columns:1fr; gap:10px; }} }}
  .axis-h {{ font-size:11.5px; margin-bottom:8px; letter-spacing:.03em; }}
  .axis-a .axis-h {{ color:var(--blue); }}
  .axis-s .axis-h {{ color:var(--green); }}
  .axis-n .axis-h {{ color:var(--amber); }}
  .lb {{ margin-bottom:9px; }}
  .lb-t {{ display:inline-block; font-size:12.8px; font-weight:500; }}
  .axis-a .lb-t {{ color:#1d4ed8; }}
  .axis-s .lb-t {{ color:#0b7a45; }}
  .axis-n .lb-t {{ color:#96560b; }}
  .lb-e {{ display:block; font-size:11.5px; color:var(--mute); margin-top:1px; line-height:1.55; }}
  .axis-empty {{ font-size:12px; color:#c1c5cc; }}
  .ad-foot {{ margin-top:11px; padding-top:11px; border-top:1px dashed #ebedf0;
    display:flex; flex-direction:column; gap:5px; }}
  .foot-item {{ font-size:12.5px; color:#4b5563; }}
  .foot-k {{ display:inline-block; min-width:66px; color:var(--mute); font-size:11.5px; }}
  footer {{ margin-top:26px; color:#a3a9b3; font-size:12px; }}
</style></head>
<body><div class="wrap">
  <header>
    <div>
      <h1>小红书广告洞察日报</h1>
      <div class="sub">{today} · {sub_head}，每条下附特殊人群 / 特殊场景 / 特殊需求拆解</div>
    </div>
  </header>

  <div class="sec-head">
    <h2>按行业环节查看</h2>
    <span>点开任一环节查看该环节的广告与三轴拆解</span>
    <span class="seg-tools">
      <button onclick="document.querySelectorAll('details.seg').forEach(d=>d.open=true)">全部展开</button>
      <button onclick="document.querySelectorAll('details.seg').forEach(d=>d.open=false)">全部收起</button>
    </span>
  </div>
  {cards}

  <footer>数据来源：小红书 App 端带「广告」标签的笔记 · 分析由大模型生成，标注的原文依据可用于人工复核。</footer>
</div></body></html>"""
    path.write_text(html_doc, encoding="utf-8")
    return path


def write_markdown(items: list[dict], stats: dict, path: Path) -> Path:
    """生成 Markdown 摘要，供 IMA 知识库等文本型知识库索引（HTML 传不进去）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    industry = (stats.get("industry") or "").strip()
    segs_all = stats.get("segments_all") or []

    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault((it.get("segment") or "").strip(), []).append(it)
    order = list(dict.fromkeys([s["name"] for s in segs_all] + list(groups)))
    desc_map = {s["name"]: s.get("desc", "") for s in segs_all}

    lines = [f"# 小红书广告洞察日报 · {today}", ""]
    if industry:
        live = len([g for g in groups if g])
        lines.append(f"- 行业：**{industry}**")
        lines.append(f"- 环节：{len(segs_all) or len(order)} 个，其中有投放 {live} 个")
    lines.append(f"- 广告数：**{len(items)} 条**（同一广告主/同一作者已去重）")
    lines.append("")

    n = 0
    for gi, seg in enumerate(order, 1):
        rows = sorted(groups.get(seg, []),
                      key=lambda x: (x.get("analysis", {}).get("score") or 0), reverse=True)
        lines.append(f"## {gi:02d} {seg}（{len(rows)} 条）")
        if desc_map.get(seg):
            lines.append(f"> {desc_map[seg]}")
            lines.append("")
        if not rows:
            lines.append("该环节在小红书未采集到广告投放。")
            lines.append("")
            continue
        for it in rows:
            n += 1
            a = it.get("analysis") or {}
            lines.append(f"### {n}. {it.get('title') or '（无标题）'}")
            meta = []
            for k, label in (("author", "作者"), ("advertiser", "广告主"), ("industry", "行业"),
                             ("liked", "点赞"), ("score", "参考分")):
                v = it.get(k) if k in ("author", "liked") else a.get(k)
                if v:
                    meta.append(f"{label}：{v}")
            if meta:
                lines.append("- " + " ｜ ".join(str(m) for m in meta))
            if (it.get("url") or "").strip():
                lines.append(f"- 链接：{it['url']}")
            for kind, label in (("audience", "特殊人群"), ("scenario", "特殊场景"), ("need", "特殊需求")):
                vals = [f"{x.get('label')}（{x.get('evidence')}）" if x.get("evidence") else str(x.get("label"))
                        for x in (a.get(kind) or []) if x.get("label")]
                if vals:
                    lines.append(f"- {label}：" + "；".join(vals))
            if a.get("hook"):
                lines.append(f"- 情绪钩子：{a['hook']}")
            if a.get("angle"):
                lines.append(f"- 可复用角度：{a['angle']}")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
