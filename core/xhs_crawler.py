# -*- coding: utf-8 -*-
"""小红书抓取（Playwright 持久化登录态）

策略：
1. 用持久化 user_data_dir 复用登录态，只需扫一次码。
2. 搜索结果优先解析页面自身接口 JSON（字段最全），失败则回退 DOM 提取。
3. 广告判定融合多路信号，输出置信度与判定依据，便于人工复核。
4. 首次运行会把原始 HTML / JSON 落到 data/raw/ 下，方便按真实结构调规则。
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from playwright.sync_api import BrowserContext, Page, sync_playwright

from . import config as C
from .config import RAW_DIR, USER_DATA_DIR

NOTE_ID_RE = re.compile(r"/(?:explore|search_result|discovery/item)/([0-9a-f]{24})")
STRONG = ("赞助", "品牌合作", "品牌赞助", "商业推广", "合作笔记")

# 页面级噪音：这些字样出现在卡片文本里不代表是广告
NOISE_MARKERS = ("广告屏蔽", "插件白名单", "网信算备", "个性化推荐算法")

_NEG_AD = re.compile(r"(没有|無|无|不是|非|拒绝|别再|不带|零)\s*广告")


def _clean_text(text: str) -> str:
    """去掉页面噪音与「没有广告」这类否定表达，避免误判"""
    for n in NOISE_MARKERS:
        text = text.replace(n, "")
    return _NEG_AD.sub("", text)

IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || { runtime: {} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(p)
  );
}
"""

# ---------------------------------------------------------------- DOM 提取
DOM_EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const ADW = ['赞助', '广告', '品牌合作', '商业推广'];
  const collect = (box) => {
    if (!box) return;
    const a = box.querySelector('a[href]');
    if (!a) return;
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/(?:explore|search_result|discovery\/item)\/([0-9a-f]{24})/);
    if (!m) return;
    const id = m[1];
    if (seen.has(id)) return;
    const text = (box.innerText || '').trim();
    if (!text) return;
    seen.add(id);

    let title = '';
    const tEl = box.querySelector('.title');
    if (tEl) title = (tEl.innerText || '').trim();
    if (!title) {
      let best = '';
      box.querySelectorAll('span,div,p').forEach(el => {
        const t = (el.innerText || '').trim();
        if (t.length > best.length && t.length < 80 && t.indexOf('\n') === -1) best = t;
      });
      title = best;
    }

    let liked = '';
    const lk = box.querySelector('.like-wrapper .count, .count');
    if (lk) liked = (lk.innerText || '').trim();

    const labels = [];
    box.querySelectorAll('*').forEach(el => {
      if (el.children.length) return;
      const cls = (el.className || '').toString().toLowerCase();
      if (/(reds-alert|corp-info|icp-text)/.test(cls)) return;
      const own = (el.innerText || '').trim();
      if (!own || own.length > 8) return;
      if (ADW.includes(own)) { labels.push(own); return; }
      if (/(sponsor|brand-tag|ad-tag|commercial)/.test(cls)) labels.push(own);
    });

    const commercial = [];
    box.querySelectorAll('[class]').forEach(el => {
      const cls = (el.className || '').toString().toLowerCase();
      if (!/(goods|product|price|shop|brand|sponsor|promo|tag-)/.test(cls)) return;
      const own = (el.innerText || '').trim();
      if (own && own.length <= 12) commercial.push(own);
    });

    out.push({
      note_id: id,
      href: href.startsWith('http') ? href : location.origin + href,
      title: title,
      liked: liked,
      text: text.slice(0, 400),
      labels: Array.from(new Set(labels)),
      commercial: Array.from(new Set(commercial))
    });
  };

  document.querySelectorAll('section.note-item, div.note-item').forEach(collect);

  if (out.length === 0) {
    document.querySelectorAll('a[href]').forEach(a => {
      const href = a.getAttribute('href') || '';
      if (!/\/(explore|search_result|discovery\/item)\/[0-9a-f]{24}/.test(href)) return;
      let box = a;
      for (let i = 0; i < 5 && box.parentElement; i++) {
        if ((box.parentElement.innerText || '').length > 900) break;
        box = box.parentElement;
        if ((box.innerText || '').length > 15) break;
      }
      collect(box);
    });
  }
  return out;
}
"""

DETAIL_EXTRACT_JS = r"""
() => {
  const pick = (sels) => {
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el && el.innerText && el.innerText.trim()) return el.innerText.trim();
    }
    return '';
  };
  const title = pick(['#detail-title', '.note-content .title', 'h1']);
  const desc = pick(['#detail-desc .note-text', '#detail-desc', '.note-content .desc', '.note-scroller']);
  const author = pick(['.author-container .username', '.author-wrapper .username', '.username']);
  const tags = [...document.querySelectorAll('#detail-desc a, .note-content a.tag, a[href*="search_result"]')]
    .map(a => (a.innerText || '').trim())
    .filter(t => t.startsWith('#'));
  const bodyText = (document.body.innerText || '').slice(0, 4000);
  const labels = ['赞助', '广告', '品牌合作', '商业推广'].filter(w => bodyText.includes(w));
  return { title, desc, author, tags, labels, bodyText };
}
"""


def _looks_like_ad(value: str) -> bool:
    return any(w in value for w in STRONG)


def _deep_scan_ad(obj, depth: int = 0, path: str = "") -> list[str]:
    """在接口 JSON 里递归找广告痕迹"""
    hits: list[str] = []
    if depth > 6:
        return hits
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{path}.{k}" if path else k
            kl = k.lower()
            if isinstance(v, (str, int, bool, float)) and v not in ("", None, 0, False):
                if re.search(r"(is_?ad|ad_?info|sponsor|corp|brand_?coop|commercial|business_?note)", kl):
                    hits.append(f"{kp}={v}")
                elif isinstance(v, str) and _looks_like_ad(v) and len(v) < 40:
                    hits.append(f"{kp}={v}")
            else:
                hits.extend(_deep_scan_ad(v, depth + 1, kp))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            hits.extend(_deep_scan_ad(v, depth + 1, f"{path}[{i}]"))
    return hits


def _count_to_int(s: str) -> int:
    if not s:
        return 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*([万wW]?)", s)
    if not m:
        return 0
    n = float(m.group(1))
    return int(n * 10000) if m.group(2) else int(n)


def _title_key(title: str) -> str:
    """标题归一化：去掉标点/emoji/空白，用于识别「同内容多账号」的重复笔记"""
    t = re.sub(r"[\s\W_]+", "", (title or "").lower(), flags=re.UNICODE)
    return t[:24]


class IpRiskError(RuntimeError):
    """小红书判定 IP 风险，整个抓取任务应中止"""


class XhsCrawler:
    def __init__(self, cfg: C.Cfg):
        self.cfg = cfg
        self.pw = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None
        self._responses: list = []
        self._debug_dumped = False
        self.last_block: str | None = None
        self.last_api_matched = 0
        self._channel = "launch"

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.pw = sync_playwright().start()
        headless = bool(self.cfg.get_path("crawl.headless", False))
        channel = self.cfg.get_path("crawl.channel") or None

        # 方案 B：连接你自己手动打开的 Chrome（最抗风控）
        cdp_url = (self.cfg.get_path("crawl.cdp_url") or "").strip()
        if cdp_url:
            browser = self.pw.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                raise RuntimeError("CDP 连上了但没有可用上下文，检查 Chrome 是否正常启动")
            self.ctx = browser.contexts[0]
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            self.page.set_default_timeout(30000)
            self.page.on("response", self._on_response)
            self._channel = "cdp"
            print(f"[·] 已连接现有 Chrome：{cdp_url}")
            return

        mobile = bool(self.cfg.get_path("crawl.mobile", False))
        if mobile:
            # 模拟 iPhone：小红书会下发移动端渲染，卡片形态更接近 App
            kwargs = dict(
                user_data_dir=str(USER_DATA_DIR) + "-mobile",
                headless=headless,
                viewport={"width": 390, "height": 844},
                device_scale_factor=3,
                is_mobile=True,
                has_touch=True,
                user_agent=IPHONE_UA,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=["--disable-blink-features=AutomationControlled"],
            )
            print("[·] 移动端模拟模式（iPhone UA + 390x844）")
        else:
            kwargs = dict(
                user_data_dir=str(USER_DATA_DIR),
                headless=headless,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=["--disable-blink-features=AutomationControlled"],
            )
        try:
            self.ctx = self.pw.chromium.launch_persistent_context(channel=channel, **kwargs)
        except Exception as e:
            print(f"[!] 启动 {channel} 失败（{e}），改用 Playwright 内置 Chromium")
            channel = None
            self.ctx = self.pw.chromium.launch_persistent_context(channel=channel, **kwargs)
        self.ctx.add_init_script(STEALTH_JS)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(30000)
        self.page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        """只记录，不在这里解析（同步 API 不允许在 handler 内部调用阻塞方法）"""
        url = response.url
        if any(k in url for k in ("/api/sns/web/v1/search/notes", "/api/sns/web/v1/feed",
                                  "/api/sns/web/v2/comment/page", "/api/sns/web/v1/comment/page")):
            self._responses.append((url, response))

    def _drain_responses(self, match: str) -> list[dict]:
        """把 handler 里攒下的响应取出来解析

        last_api_matched 记录匹配到的响应数：>0 但 payloads 为空说明请求发生了却取不到 body。
        """
        payloads, keep, matched = [], [], 0
        for url, resp in self._responses:
            if match in url:
                matched += 1
                try:
                    payloads.append(resp.json())
                except Exception:
                    pass
            else:
                keep.append((url, resp))
        self._responses = keep
        self.last_api_matched = matched
        return payloads

    def close(self) -> None:
        try:
            # CDP 模式下不要关掉用户自己的浏览器
            if self.ctx and self._channel != "cdp":
                self.ctx.close()
        finally:
            if self.pw:
                self.pw.stop()

    # ------------------------------------------------------------ 工具
    def _scroll_y(self) -> float:
        try:
            return float(self.page.evaluate(
                "() => window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0"))
        except Exception:
            return -1.0

    def _human_scroll(self, delta: int | None = None) -> float:
        """滚动并返回真实位移。位移为 0 说明滚错了区域，逐级换方案。

        关键点：mouse.wheel 作用于鼠标当前位置，默认在 (0,0) 的导航栏上，
        所以必须先 mouse.move 到内容区，否则页面纹丝不动。
        """
        assert self.page
        delta = delta or random.randint(1200, 2000)
        before = self._scroll_y()

        try:
            self.page.mouse.move(random.randint(480, 900), random.randint(280, 680))
            self.page.mouse.wheel(0, delta)
            self.page.wait_for_timeout(450)
            now = self._scroll_y()
            if now > before + 50:
                return now - before
        except Exception:
            pass

        try:
            self.page.evaluate(f"() => window.scrollBy(0, {delta})")
            self.page.wait_for_timeout(450)
            now = self._scroll_y()
            if now > before + 50:
                return now - before
        except Exception:
            pass

        try:
            moved = self.page.evaluate("""(d) => {
                const els = Array.from(document.querySelectorAll('div,main,section'));
                for (const el of els) {
                    const st = getComputedStyle(el);
                    if ((st.overflowY === 'auto' || st.overflowY === 'scroll')
                        && el.scrollHeight > el.clientHeight + 200) {
                        const b = el.scrollTop;
                        el.scrollTop = b + d;
                        if (el.scrollTop > b) return el.scrollTop - b;
                    }
                }
                return 0;
            }""", delta)
            self.page.wait_for_timeout(450)
            return float(moved or 0)
        except Exception:
            return 0.0

    def _sleep(self) -> None:
        lo = float(self.cfg.get_path("crawl.min_delay", 2.0))
        hi = float(self.cfg.get_path("crawl.max_delay", 5.0))
        time.sleep(random.uniform(lo, hi))

    def _dump(self, name: str, content: str) -> None:
        try:
            path = RAW_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
            path.write_text(content, encoding="utf-8")
        except Exception:
            pass

    def _block_reason(self) -> str | None:
        """判断当前页面处于什么状态：None=正常 / login=需要登录 / ip_risk=IP被风控"""
        try:
            url = self.page.url
            txt = self.page.inner_text("body")[:1500]
        except Exception:
            return None
        if "IP存在风险" in txt or "error_code=300012" in url or "300012" in url:
            return "ip_risk"
        if "/login" in url or "website-login" in url:
            return "login"
        if "扫码登录" in txt and "手机号登录" in txt:
            return "login"
        return None

    def _is_login_wall(self) -> bool:
        return self._block_reason() is not None

    def ensure_login(self) -> bool:
        assert self.page
        self.page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
        self._sleep()

        reason = self._block_reason()
        if reason == "ip_risk":
            self.last_block = "ip_risk"
            self._warn_ip_risk()
            return False
        if reason != "login":
            self._mark_login_ok()
            print("[✓] 已复用历史登录态")
            return True

        print("\n" + "=" * 62)
        print("需要登录：请在弹出的浏览器里用小红书 App 扫码")
        print("扫码完成后不用管，程序会自动继续（最多等 180 秒）")
        print("=" * 62)
        for _ in range(90):
            time.sleep(2)
            r = self._block_reason()
            if r == "ip_risk":
                self.last_block = "ip_risk"
                self._warn_ip_risk()
                return False
            if r is None:
                self._mark_login_ok()
                print("[✓] 登录成功，登录态已保存，下次不用再扫")
                time.sleep(2)
                return True
        self.last_block = "login"
        print("[x] 等待登录超时")
        return False

    def _mark_login_ok(self) -> None:
        """记录一次确认有效的登录，供 doctor.py 判断"""
        try:
            (C.BROWSER_DIR / "login_ok.json").write_text(
                json.dumps({"at": datetime.now().isoformat(timespec="seconds")},
                           ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _warn_ip_risk() -> None:
        print("\n" + "!" * 62)
        print("[x] 小红书判定当前网络 IP 存在风险（错误码 300012），直接拒绝访问")
        print("    常见原因：机房/云服务器/代理 IP、频繁切换网络、自动化痕迹过重")
        print("    处理办法：")
        print("      1. 换到你自己的家庭/办公宽带（不要挂 VPN 或代理）")
        print("      2. 把 crawl.headless 设为 false，让真实 Chrome 窗口跑")
        print("      3. 同一网络下先手动用 Chrome 正常浏览几分钟再跑")
        print("      4. 仍不行就看 README「方案 B：连接已登录的 Chrome」")
        print("!" * 62 + "\n")

    # ------------------------------------------------------------ 搜索
    def search(self, keyword: str) -> list[dict]:
        """返回该关键词下抓到的笔记列表（含广告判定）"""
        assert self.page
        notes: dict[str, dict] = {}
        url = (
            "https://www.xiaohongshu.com/search_result"
            f"?keyword={keyword}&source=web_explore_feed&type=51"
        )
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception as e:
            print(f"    [!] 打开搜索页失败: {e}")
            return []
        self._sleep()

        if self._block_reason() == "ip_risk":
            raise IpRiskError("搜索页命中 IP 风控（300012）")
        if self._is_login_wall():
            print("    [!] 命中登录墙，跳过该关键词")
            return []

        max_scroll = int(self.cfg.get_path("crawl.max_scroll_per_keyword", 8))
        stuck = 0
        for i in range(max_scroll):
            if self._human_scroll() <= 0:
                stuck += 1
            self.page.wait_for_timeout(int(random.uniform(1000, 2000)))
            if stuck >= 3:
                print("    [!] 连续滚动无位移，停止该关键词")
                break

            # 1) 接口数据
            for payload in self._drain_responses("/api/sns/web/v1/search/notes"):
                for n in self._parse_search_payload(payload, keyword):
                    notes.setdefault(n["note_id"], n)
            # 2) DOM 兜底
            try:
                for d in self.page.evaluate(DOM_EXTRACT_JS):
                    if d["note_id"] in notes:
                        continue
                    notes[d["note_id"]] = self._build_from_dom(d, keyword)
            except Exception:
                pass

            if not self._debug_dumped:
                self._dump("search_page.html", self.page.content())
                self._debug_dumped = True

            if len(notes) >= 40:
                break

        print(f"    {keyword}: 抓到 {len(notes)} 条")
        return list(notes.values())

    def _parse_search_payload(self, payload: dict, keyword: str) -> list[dict]:
        out = []
        try:
            items = (payload.get("data") or {}).get("items") or []
        except AttributeError:
            return out
        for it in items:
            if not isinstance(it, dict):
                continue
            card = it.get("note_card") or {}
            if not card:
                continue
            nid = it.get("id") or card.get("note_id")
            if not nid or not re.fullmatch(r"[0-9a-f]{24}", str(nid)):
                continue
            token = card.get("xsec_token") or it.get("xsec_token") or ""
            title = card.get("display_title") or card.get("title") or ""
            user = (card.get("user") or {}).get("nickname") or (card.get("user") or {}).get("nick_name") or ""
            liked = str((card.get("interact_info") or {}).get("liked_count") or "")
            tags = [t.get("name") for t in (card.get("tag_list") or []) if isinstance(t, dict)]
            signals = _deep_scan_ad(card)
            is_ad, conf = self._judge(title, " ".join(tags), signals)
            out.append({
                "note_id": str(nid),
                "title": title,
                "desc": card.get("desc") or "",
                "tags": tags,
                "author": user,
                "liked": liked,
                "url": f"https://www.xiaohongshu.com/explore/{nid}" + (f"?xsec_token={token}&xsec_source=pc_search" if token else ""),
                "xsec_token": token,
                "is_ad": is_ad,
                "ad_confidence": conf,
                "ad_signals": signals,
                "keyword": keyword,
            })
        return out

    def _build_from_dom(self, d: dict, keyword: str) -> dict:
        signals = [f"页面标识:{l}" for l in d.get("labels", [])]
        is_ad, conf = self._judge(d.get("title", ""), d.get("text", ""), signals)
        return {
            "note_id": d["note_id"],
            "title": d.get("title", ""),
            "desc": "",
            "tags": [t for t in re.findall(r"#[^\s#]+", d.get("text", ""))],
            "author": "",
            "liked": d.get("liked", ""),
            "url": d["href"],
            "xsec_token": "",
            "is_ad": is_ad,
            "ad_confidence": conf,
            "ad_signals": signals,
            "keyword": keyword,
        }

    # ------------------------------------------------------------ 广告判定
    def _judge(self, title: str, body: str, signals: list[str]) -> tuple[bool, float]:
        strong = self.cfg.get_path("crawl.ad_rules.strong_keywords", list(STRONG))
        weak = self.cfg.get_path("crawl.ad_rules.weak_keywords", [])

        text = _clean_text(f"{title} {body}")
        noisy = any(n in f"{title} {body}" for n in NOISE_MARKERS)
        # 明确标识：只认真正的广告标识，排除页面级噪音
        # 若整页出现「广告屏蔽插件」这类噪音，连孤立的「广告」标识也一并丢弃
        label_hits = [s for s in signals
                      if ("=" in s or "页面标识" in s or "详情页标识" in s)
                      and not any(n in s for n in NOISE_MARKERS)
                      and not (noisy and s.rstrip().endswith(":广告"))]
        # 文本里出现"赞助/品牌合作"这类才叫强证据；"广告"单独出现不算
        text_strong = [w for w in strong if w in text]
        text_weak = [w for w in weak if w in text]

        if label_hits:
            return True, 0.93
        if text_strong:
            return True, 0.78
        if len(text_weak) >= 2:
            return True, 0.62
        if len(text_weak) == 1:
            return False, 0.45   # 疑似，先不入库为广告
        return False, 0.15

    # ------------------------------------------------------------ 首页推荐流
    def homefeed(self, scrolls: int = 12) -> list[dict]:
        """首页信息流：小红书的信息流广告（赞助笔记）密度远高于搜索结果"""
        assert self.page
        notes: dict[str, dict] = {}
        try:
            self.page.goto("https://www.xiaohongshu.com/explore",
                           wait_until="domcontentloaded")
        except Exception as e:
            print(f"    [!] 打开首页失败: {e}")
            return []
        self._sleep()
        if self._block_reason() == "ip_risk":
            raise IpRiskError("首页命中 IP 风控（300012）")

        stuck = 0
        for i in range(scrolls):
            moved = self._human_scroll()
            if moved <= 0:
                stuck += 1
            self.page.wait_for_timeout(int(random.uniform(1200, 2200)))
            if stuck >= 3:
                print("    [!] 连续 3 次滚动无位移，可能已到底或被限制")
                break

            for payload in self._drain_responses("/api/sns/web/v1/homefeed"):
                for n in self._parse_search_payload(payload, "首页推荐"):
                    notes.setdefault(n["note_id"], n)
            try:
                for d in self.page.evaluate(DOM_EXTRACT_JS):
                    notes.setdefault(d["note_id"], self._build_from_dom(d, "首页推荐"))
            except Exception:
                pass

            if not self._debug_dumped:
                self._dump("homefeed_page.html", self.page.content())
                self._debug_dumped = True
            if len(notes) >= 60:
                break

        ads = [n for n in notes.values() if n["is_ad"]]
        print(f"    首页推荐流: {len(notes)} 条，其中广告 {len(ads)} 条")
        return list(notes.values())

    # ------------------------------------------------------------ 详情
    def fetch_detail(self, note: dict) -> dict:
        assert self.page
        url = note["url"]
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except Exception:
            return note
        self.page.wait_for_timeout(int(random.uniform(1500, 2600)))

        # 接口优先
        feed = self._drain_responses("/api/sns/web/v1/feed")
        for payload in feed:
            try:
                items = (payload.get("data") or {}).get("items") or []
                n = (items[0] or {}).get("note") or {}
                if n:
                    if n.get("desc"):
                        note["desc"] = n["desc"]
                    if n.get("title"):
                        note["title"] = n["title"]
                    tl = [t.get("name") for t in (n.get("tag_list") or []) if isinstance(t, dict)]
                    if tl:
                        note["tags"] = tl
                    au = (n.get("user") or {}).get("nickname")
                    if au:
                        note["author"] = au
                    li = (n.get("interact_info") or {}).get("liked_count")
                    if li:
                        note["liked"] = str(li)
                    extra = _deep_scan_ad(n)
                    if extra:
                        note["ad_signals"] = list(dict.fromkeys(note.get("ad_signals", []) + extra))
                        note["is_ad"] = True
                        note["ad_confidence"] = max(float(note.get("ad_confidence", 0)), 0.93)
            except Exception:
                continue

        # DOM 兜底
        try:
            dom = self.page.evaluate(DETAIL_EXTRACT_JS)
            if not note.get("desc") and dom.get("desc"):
                note["desc"] = dom["desc"][:2000]
            if not note.get("title") and dom.get("title"):
                note["title"] = dom["title"]
            if not note.get("tags") and dom.get("tags"):
                note["tags"] = dom["tags"][:15]
            if not note.get("author") and dom.get("author"):
                note["author"] = dom["author"]
            if dom.get("labels"):
                note["ad_signals"] = list(dict.fromkeys(
                    note.get("ad_signals", []) + [f"详情页标识:{x}" for x in dom["labels"]]))
                note["is_ad"] = True
                note["ad_confidence"] = max(float(note.get("ad_confidence", 0)), 0.9)
            note["_body_text"] = dom.get("bodyText", "")[:1500]
        except Exception:
            pass

        # 热评
        comments: list[str] = []
        if int(self.cfg.get_path("crawl.fetch_comments", 0) or 0) > 0:
            for payload in self._drain_responses("/api/sns/web/v2/comment/page") + \
                             self._drain_responses("/api/sns/web/v1/comment/page"):
                try:
                    for c in (payload.get("data") or {}).get("comments") or []:
                        txt = c.get("content") or ""
                        if txt:
                            comments.append(txt)
                except Exception:
                    continue
        note["comments"] = comments[: int(self.cfg.get_path("crawl.fetch_comments", 0) or 0)]

        # 详情页也可能暴露出更多证据，重新判一次
        if not note.get("is_ad"):
            is_ad, conf = self._judge(note.get("title", ""), note.get("desc", ""),
                                      note.get("ad_signals", []))
            note["is_ad"], note["ad_confidence"] = is_ad, conf
        return note

    # ------------------------------------------------------------ 主流程
    def collect(self, keywords: Iterable[str], target: int) -> list[dict]:
        """抓够 target 条广告笔记就停"""
        picked: dict[str, dict] = {}
        seen_titles: set[str] = set()
        fetch_detail = bool(self.cfg.get_path("crawl.fetch_detail", True))
        sources = self.cfg.get_path("crawl.sources", ["search"]) or ["search"]

        # 先扫首页信息流，广告密度最高
        if "homefeed" in sources and len(picked) < target:
            try:
                for n in self.homefeed(int(self.cfg.get_path("crawl.homefeed_scroll", 12))):
                    if len(picked) >= target:
                        break
                    if not n["is_ad"]:
                        continue
                    tk = _title_key(n.get("title", ""))
                    if tk and tk in seen_titles:
                        continue
                    if fetch_detail:
                        n = self.fetch_detail(n)
                        self._sleep()
                    if n.get("is_ad"):
                        picked[n["note_id"]] = n
                        if tk:
                            seen_titles.add(tk)
            except IpRiskError as e:
                print(f"[x] {e}，中止本轮抓取")
                self._warn_ip_risk()
            print(f"[·] 首页流累计 {len(picked)} 条")

        for kw in keywords:
            if len(picked) >= target:
                break
            try:
                notes = self.search(kw)
            except IpRiskError as e:
                print(f"[x] {e}，中止本轮抓取")
                self._warn_ip_risk()
                break
            ads = [n for n in notes if n["is_ad"]]
            ads.sort(key=lambda n: _count_to_int(n.get("liked", "")), reverse=True)
            print(f"    -> 其中广告/疑似广告 {len(ads)} 条")

            for n in ads:
                if len(picked) >= target:
                    break
                if n["note_id"] in picked:
                    continue
                tk = _title_key(n.get("title", ""))
                if tk and tk in seen_titles:
                    continue
                if fetch_detail:
                    n = self.fetch_detail(n)
                    self._sleep()
                if n.get("is_ad"):
                    picked[n["note_id"]] = n
                    if tk:
                        seen_titles.add(tk)
            self._sleep()

        print(f"[✓] 本轮共采集 {len(picked)} 条")
        return list(picked.values())


def login_only(cfg: C.Cfg) -> None:
    crawler = XhsCrawler(cfg)
    crawler.start()
    try:
        ok = crawler.ensure_login()
        print("[✓] 登录态已保存" if ok else "[x] 未完成登录")
    finally:
        crawler.close()
