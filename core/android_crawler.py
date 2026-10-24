# -*- coding: utf-8 -*-
"""Android App 端采集：从模拟器/真机读取「带广告标签」的笔记

两种模式：
  search —— 搜索关键词定向采集（推荐，可按行业铺量，绕开信息流画像偏差）
  feed   —— 首页信息流（广告受账号画像和日频控影响，容易偏科且很快枯竭）

原理（不破解 SSL、不抓包）：
  uiautomator2 读取界面 XML。「广告」是独立 TextView：
    · 信息流（feed）：卡片是 FrameLayout，content-desc 形如「笔记 标题 来自作者 649赞」，
      广告标签坐标落在哪张卡片的 bounds 内，那张卡就是广告；
    · 搜索页（search）：卡片是 ViewGroup，没有 content-desc，
      直接以「广告」TextView 为锚点，向上找到卡片容器再提取标题/作者/点赞。
  链接：进详情页点「分享」→「复制链接」，读剪贴板拿 https://xhslink.cn/... 短链。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET

from . import config as C

PKG = "com.xingin.xhs"
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
LIKED_RE = re.compile(r"^(.*?)\s*([\d.]+[万wW]?\+?\s*赞|[\d.]+万人?观看|赞)$")
DATE_RE = re.compile(r"^\d{1,2}-\d{1,2}$")
URL_RE = re.compile(r"https?://[^\s，,、]+")
PARTIAL = C.DATA_DIR / "android_partial.json"


def parse_bounds(s) -> tuple[int, int, int, int] | None:
    if isinstance(s, dict):
        try:
            return (int(s["left"]), int(s["top"]), int(s["right"]), int(s["bottom"]))
        except Exception:
            return None
    m = BOUNDS_RE.search(str(s or ""))
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def center(box: tuple[int, int, int, int]) -> tuple[int, int]:
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


def contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    cx, cy = center(inner)
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def parse_desc(desc: str) -> dict:
    """信息流卡片 content-desc：「视频 标题 来自成龙 41万赞」"""
    left, _, right = desc.rpartition(" 来自")
    author, liked = "", ""
    m = LIKED_RE.match(right.strip())
    if m:
        author, liked = m.group(1).strip(), m.group(2).strip()
    else:
        author = right.strip()
    if liked and not re.search(r"\d", liked):
        liked = ""
    parts = left.strip().split(" ", 1)
    typ = parts[0] if parts and parts[0] in ("笔记", "视频", "直播") else ""
    title = parts[1].strip() if len(parts) > 1 else left.strip()
    return {"type": typ, "title": title, "author": author, "liked": liked}


def make_id(title: str, author: str) -> str:
    raw = f"{(title or '').strip()}|{(author or '').strip()}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


class AndroidCrawler:
    def __init__(self, cfg: C.Cfg):
        self.cfg = cfg
        self.serial = (cfg.get_path("android.serial") or "").strip() or None
        self.d = None
        self.max_scroll = int(cfg.get_path("android.max_scroll", 200))
        self.fetch_link = bool(cfg.get_path("android.fetch_link", True))
        self.fetch_detail = bool(cfg.get_path("android.fetch_detail", True))

    # ---------------------------------------------------------- 连接
    def connect(self) -> None:
        try:
            import uiautomator2 as u2
        except ImportError:
            raise RuntimeError("缺少依赖：pip install uiautomator2")
        serial = self.serial or self._pick_device()
        try:
            self.d = u2.connect(serial)
            _ = self.d.window_size()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"连不上安卓设备（{e}）。检查模拟器是否启动、ADB 调试是否打开、"
                "端口是否正确（MuMu 常见 127.0.0.1:16384）") from e
        print(f"[✓] 已连接 Android 设备: {self.d.serial}  "
              f"{self.d.window_size()}  Android {self.d.device_info.get('version')}")

    @staticmethod
    def _pick_device() -> str:
        try:
            import adbutils
        except ImportError:
            return ""
        devices = [x.serial for x in adbutils.adb.device_list()]
        if not devices:
            raise RuntimeError("没有检测到任何安卓设备，请先启动模拟器并开启 ADB 调试")
        if len(devices) == 1:
            return devices[0]
        print(f"[·] 检测到 {len(devices)} 台设备：{devices}")
        for s in devices:
            try:
                if any(p.startswith(PKG) for p in adbutils.adb.device(s).list_packages()):
                    print(f"[·] 选用已安装小红书的设备：{s}")
                    return s
            except Exception:
                continue
        return devices[0]

    # ---------------------------------------------------------- 历史去重
    @staticmethod
    def _load_history() -> tuple[set[str], set[str], set[str]]:
        """返回 (历史 note_id, 标题指纹, 分享短链) —— 短链是跨入口唯一键"""
        try:
            from . import db as _db
            conn = _db.connect()
            rows = conn.execute("SELECT note_id, title, url FROM notes").fetchall()
        except Exception as e:  # noqa: BLE001
            print(f"[!] 读取历史库失败，本次不做历史去重: {e}")
            return set(), set(), set()
        ids, titles, urls = set(), set(), set()
        for r in rows:
            if r["note_id"]:
                ids.add(r["note_id"])
            t = (r["title"] or "").strip()
            if t:
                tk = re.sub(r"[\s\W_]+", "", t.lower())[:16]
                if tk:
                    titles.add(tk)
            u = (r["url"] or "").strip()
            if "xhslink" in u:
                urls.add(u.split("?")[0])
        return ids, titles, urls

    def _merge_history(self, seen_titles: set[str]) -> tuple[set[str], set[str]]:
        ids, titles, urls = self._load_history()
        seen_titles |= titles
        if ids:
            print(f"[·] 历史库已有 {len(ids)} 条 / {len(urls)} 个短链，重复的直接跳过")
        return ids, urls

    # ---------------------------------------------------------- 广告主去重
    @staticmethod
    def _load_brands() -> tuple[list[str], set[str]]:
        """从历史分析结果里取所有广告主，返回 (品牌词表, 已采品牌集合)

        同一条投放可能被写成不同的笔记（如 Ozon 的多篇招商笔记），
        标题/正文里带着同一个品牌词，靠这个词把它们拦住。
        """
        brands: set[str] = set()
        try:
            from . import db as _db
            conn = _db.connect()
            for r in conn.execute("SELECT payload FROM analysis"):
                try:
                    adv = (json.loads(r["payload"]) or {}).get("advertiser") or ""
                except Exception:
                    continue
                a = (adv or "").strip().lower()
                a = re.sub(r"[\s（）()【】]+", "", a)
                if len(a) >= 2:
                    brands.add(a)
        except Exception:
            pass
        ordered = sorted(brands, key=len, reverse=True)
        return ordered, set(brands)

    @staticmethod
    def _author_key(name: str) -> str:
        """作者名归一：去空格/括号/表情，转小写"""
        a = re.sub(r"[\s（）()【】《》「」·\-_]+", "", (name or "").lower())
        a = re.sub(r"[^\w\u4e00-\u9fa5]", "", a)
        return a[:20]

    @staticmethod
    def _load_authors() -> set[str]:
        """历史采过的作者账号：同一个号发的多条广告，只留第一条"""
        out: set[str] = set()
        try:
            from . import db as _db
            conn = _db.connect()
            for r in conn.execute("SELECT DISTINCT author FROM notes"):
                k = AndroidCrawler._author_key(r["author"])
                if len(k) >= 2:
                    out.add(k)
        except Exception:
            pass
        return out

    @staticmethod
    def _brand_hit(text: str, brands: list[str]) -> str:
        """返回文本中命中的最长品牌词"""
        low = (text or "").lower()
        for b in brands:
            if b and b in low:
                return b
        return ""

    # ---------------------------------------------------------- 存活检测
    def _alive(self) -> bool:
        try:
            return (self.d.app_current() or {}).get("package") == PKG
        except Exception:
            return False

    def _is_dead_err(self, e: Exception) -> bool:
        m = str(e).lower()
        return any(k in m for k in ("not found", "not online", "device offline", "connect"))

    def _reconnect(self) -> bool:
        """adb 设备名会变（emulator-5554 / 127.0.0.1:16384 之间切换），
        旧的 u2 连接会报 device not found，必须重新解析设备再连。"""
        try:
            from . import emulator as emu
            import uiautomator2 as u2

            serial = emu.find_device(int(self.cfg.get_path("emulator.adb_port", 16384)))
            if not serial:
                print("    [!] 找不到可用设备")
                return False
            print(f"    [·] 重新连接设备：{serial}")
            self.d = u2.connect(serial)
            _ = self.d.window_size()
            return True
        except Exception as e:  # noqa: BLE001
            print(f"    [!] 重连失败：{str(e)[:60]}")
            return False

    def _recover(self, kw: str | None = None) -> bool:
        """小红书被系统杀掉后自动重开，并重新进入原搜索词"""
        print("    [!] 检测到小红书已退出，正在重新打开…")
        self._reconnect()
        for _ in range(2):
            try:
                self.d.app_start(PKG)
                time.sleep(6.5)
            except Exception:
                time.sleep(2)
                continue
            if not self._alive():
                continue
            print("    [✓] 已重新打开")
            if kw:
                return self._open_search(kw)
            return True
        print("    [x] 重开失败")
        return False

    # ---------------------------------------------------------- 高速交互
    # uiautomator 的输入注入在本机模拟器上退化到 ~10s/次，改用 adb shell input（~0.05s）
    def _tap(self, x: int, y: int) -> None:
        self.d.shell(f"input tap {int(x)} {int(y)}")

    def _back(self) -> None:
        self.d.shell("input keyevent 4")

    def _swipe_up(self) -> None:
        w, _ = self.d.window_size()
        self.d.shell(f"input swipe {w // 2} 1560 {w // 2} 620 260")

    def _find_tap(self, keyword: str, contains: bool = True) -> bool:
        """在界面文字里找关键词并点击其中心（避开 uiautomator 注入）"""
        for n in self._nodes():
            t = (n.get("text") or "") or (n.get("content-desc") or "")
            if not t:
                continue
            hit = (keyword in t) if contains else (t.strip() == keyword)
            if not hit:
                continue
            b = parse_bounds(n.get("bounds"))
            if not b:
                continue
            self._tap(*center(b))
            return True
        return False

    # ---------------------------------------------------------- 增量落盘
    def _save_partial(self, found: dict) -> None:
        try:
            PARTIAL.write_text(
                json.dumps(list(found.values()), ensure_ascii=False, default=str),
                encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _load_partial() -> dict:
        try:
            if PARTIAL.exists():
                return {c["note_id"]: c for c in
                        json.loads(PARTIAL.read_text(encoding="utf-8"))}
        except Exception:
            pass
        return {}

    # ---------------------------------------------------------- 页面解析
    def _nodes(self) -> list:
        try:
            xml = self.d.dump_hierarchy(compressed=False)
            return list(ET.fromstring(xml).iter("node"))
        except Exception:
            return []

    def _parse_feed(self, nodes: list) -> list[dict]:
        ad_boxes = [b for b in (parse_bounds(n.get("bounds")) for n in nodes
                                if (n.get("text") or "").strip() == "广告") if b]
        out = []
        for n in nodes:
            desc = n.get("content-desc") or ""
            if "来自" not in desc:
                continue
            box = parse_bounds(n.get("bounds"))
            if not box:
                continue
            info = parse_desc(desc)
            if not info["title"]:
                continue
            info.update(box=box, is_ad=any(contains(box, ab) for ab in ad_boxes),
                        note_id=make_id(info["title"], info["author"]), source="feed")
            out.append(info)
        return out

    def _parse_search(self, nodes: list) -> list[dict]:
        """搜索页：以「广告」为锚点，向上找容器再提取字段"""
        parent = {c: p for p in nodes for c in p} if nodes else {}
        out = []
        for ad in nodes:
            if (ad.get("text") or "").strip() != "广告":
                continue
            cont = parent.get(ad)
            while cont is not None:
                cb = parse_bounds(cont.get("bounds"))
                if cb and cb[2] - cb[0] > 300 and cb[3] - cb[1] > 300:
                    break
                cont = parent.get(cont)
            cb = parse_bounds(cont.get("bounds")) if cont is not None else None
            if not cb:
                continue

            texts = [((n.get("text") or "").strip(), parse_bounds(n.get("bounds")))
                     for n in cont.iter("node") if (n.get("text") or "").strip()]
            texts = [(t, b) for t, b in texts if b and t != "广告"]
            if not texts:
                continue
            raw_title, tbox = max(texts, key=lambda x: (len(x[0]), x[0]))
            # 搜索卡片把标题和正文开头连在一起，截到第一段
            title = raw_title.split("\n")[0].strip()[:44]
            liked = author = date = ""
            for t, b in texts:
                if t == title:
                    continue
                if DATE_RE.match(t):
                    date = t
                elif re.fullmatch(r"\d+(?:\.\d+)?[万wW]?", t):
                    liked = t
                elif b[1] >= tbox[3] and len(t) <= 20 and not author:
                    author = t
            try:
                out.append({
                    "type": "笔记", "title": title, "author": author, "liked": liked,
                    "date": date, "box": cb, "is_ad": True, "source": "search",
                    "note_id": make_id(title, author),
                })
            except Exception:
                continue
        return out

    def _parse(self, mode: str) -> list[dict]:
        nodes = self._nodes()
        return self._parse_search(nodes) if mode == "search" else self._parse_feed(nodes)

    # ---------------------------------------------------------- 操作
    def _ensure_app(self) -> None:
        if self.d.app_current().get("package") != PKG:
            print("[·] 启动小红书 App")
            self.d.app_start(PKG)
            time.sleep(7)

    def _safe_swipe(self) -> bool:
        for _ in range(2):
            try:
                self._swipe_up()
                return True
            except Exception as e:  # noqa: BLE001
                print(f"    [!] 滑动异常: {str(e)[:60]}")
                try:
                    self._back()
                    time.sleep(0.8)
                except Exception:
                    pass
        return False

    def _reset_to_home(self) -> None:
        """确保退出详情/搜索页，回到可操作的主界面"""
        d = self.d
        for _ in range(5):
            try:
                act = (d.app_current() or {}).get("activity", "") or ""
            except Exception:
                return
            if "Detail" in act or "Search" in act or "Login" in act:
                self._back()
                time.sleep(1.0)
            else:
                return
        d.app_start(PKG)
        time.sleep(6)

    def _open_search(self, kw: str) -> bool:
        d = self.d
        self._reset_to_home()
        try:
            self._tap(1032, 81)        # 顶部搜索入口
            time.sleep(1.8)
        except Exception:
            pass
        try:
            box = d(className="android.widget.EditText")
            if box.exists(timeout=4):
                b = parse_bounds(box.info.get("bounds", "")) 
                if b:
                    self._tap(*center(b))
                box.set_text(kw)
            else:
                self._tap(400, 87)
                time.sleep(0.5)
                d(focused=True).set_text(kw)
            time.sleep(0.5)
            self._tap(998, 87)         # 「搜索」按钮
            time.sleep(3.0)
            return True
        except Exception as e:  # noqa: BLE001
            if self._is_dead_err(e) and self._reconnect():
                try:
                    d = self.d
                    self._reset_to_home()
                    self._tap(1032, 81)
                    time.sleep(1.8)
                    box = d(className="android.widget.EditText")
                    if box.exists(timeout=4):
                        b = parse_bounds(box.info.get("bounds", ""))
                        if b:
                            self._tap(*center(b))
                        box.set_text(kw)
                    time.sleep(0.5)
                    self._tap(998, 87)
                    time.sleep(3.0)
                    return True
                except Exception as e2:  # noqa: BLE001
                    print(f"    [!] 重连后仍然失败: {str(e2)[:60]}")
            print(f"    [!] 搜索「{kw}」失败: {str(e)[:80]}")
            return False

    def _enrich(self, card: dict) -> dict:
        """进详情页：取正文/话题，并点分享→复制链接拿真实短链"""
        d = self.d
        x1, y1, x2, y2 = card["box"]
        try:
            self._tap((x1 + x2) // 2, y1 + (y2 - y1) // 3)
            time.sleep(2.6)

            if self.fetch_detail:
                texts = [(n.get("text") or "").strip() for n in self._nodes()]
                texts = [t for t in texts if t]
                body = [t for t in texts if len(t) >= 30 and not DATE_RE.match(t)]
                if body:
                    card["desc"] = max(body, key=len)[:1500]
                tags = [t for t in texts if t.startswith("#")][:12]
                if tags:
                    card["tags"] = tags
                card["detail_has_ad"] = any(t == "广告" for t in texts)

            if self.fetch_link:
                # 分享入口固定在右上角 (~1030,90)，部分页面没有 content-desc，用坐标兜底
                for attempt in range(2):
                    try:
                        if attempt == 0:
                            self._tap(1030, 90)
                        else:
                            self._find_tap("分享")
                        time.sleep(1.5)
                        if self._find_tap("复制链接"):
                            time.sleep(1.0)
                            m = URL_RE.search(d.clipboard or "")
                            if m:
                                card["url"] = m.group(0)
                            break
                    except Exception:
                        continue
                self._find_tap("关闭", contains=False)
        except Exception as e:  # noqa: BLE001
            print(f"    [!] 详情失败: {e}")
        finally:
            for _ in range(3):
                try:
                    self._back()
                    time.sleep(1.0)
                    act = (d.app_current() or {}).get("activity", "") or ""
                    if "Detail" not in act:
                        break
                except Exception:
                    break
        if not self._alive():
            self._recover(None)
        return card

    # ---------------------------------------------------------- 主流程
    def _collect_loop(self, mode: str, cap: int, found: dict,
                      seen_titles: set | None = None,
                      segment: str | None = None,
                      segment_idx: int = 0,
                      known_ids: set[str] | None = None,
                      known_urls: set[str] | None = None,
                      brands: list[str] | None = None,
                      brand_seen: set[str] | None = None,
                      author_seen: set[str] | None = None,
                      kw: str | None = None) -> None:
        stall_limit = int(self.cfg.get_path("android.ad_stall_screens", 40))
        wait = float(self.cfg.get_path("android.scroll_wait", 2.0))
        ad_stall = 0
        empty_screens = 0
        same_screens = 0
        last_sig = None

        for i in range(self.max_scroll):
            if not self._alive():
                if not self._recover(kw):
                    print("  [!] 应用无法恢复，跳过该关键词")
                    break

            before = len(found)
            cards = self._parse(mode)
            empty_screens = empty_screens + 1 if not cards else 0

            # 同一批卡片反复出现 = 页面没滚动，别空转
            sig = tuple(sorted(c["note_id"] for c in cards))
            same_screens = same_screens + 1 if (cards and sig == last_sig) else 0
            last_sig = sig or last_sig
            for c in cards:
                if len(found) >= cap:
                    break
                if c["note_id"] in found:
                    continue
                if known_ids and c["note_id"] in known_ids:
                    continue
                tk = re.sub(r"[\s\W_]+", "", (c["title"] or "").lower())[:16]
                if seen_titles is not None and tk and tk in seen_titles:
                    continue

                # 同一作者发的多条广告：账号是最硬的信号，直接跳过
                if author_seen:
                    ak = self._author_key(c.get("author", ""))
                    if ak and ak in author_seen:
                        print(f"    - 同一作者的其它笔记，跳过：{c.get('author','')[:12]}")
                        continue

                # 同一广告主的其它笔记：标题里就带品牌词，直接跳过，不进详情
                if brands and brand_seen:
                    hit = self._brand_hit(c.get("title", ""), brands)
                    if hit and hit in brand_seen:
                        print(f"    - 同一广告主的其它笔记，跳过：{c['title'][:20]}")
                        continue
                if self.fetch_link or self.fetch_detail:
                    c = self._enrich(c)

                # 进详情后正文里再判一次品牌（标题不带但正文带的漏网）
                if brands and brand_seen:
                    hit = self._brand_hit((c.get("title") or "") + (c.get("desc") or ""), brands)
                    if hit and hit in brand_seen and hit not in self._brand_hit(c.get("title", ""), brands):
                        print(f"    - 正文命中已采品牌，跳过：{c['title'][:20]}")
                        continue
                    if hit:
                        brand_seen.add(hit)

                # 短链唯一：拿到链接后再判一次，拦住跨入口的同一篇笔记
                if known_urls and c.get("url"):
                    key = c["url"].split("?")[0]
                    if key in known_urls:
                        print(f"    - 已采集过，跳过：{c['title'][:22]}")
                        continue
                    known_urls.add(key)

                if author_seen:
                    ak = self._author_key(c.get("author", ""))
                    if ak:
                        author_seen.add(ak)
                if segment:
                    c["segment"] = segment
                    c["segment_idx"] = segment_idx
                found[c["note_id"]] = c
                if seen_titles is not None and tk:
                    seen_titles.add(tk)
                self._save_partial(found)
                print(f"    + [{len(found)}] {c['title'][:26]} | "
                      f"{'有链接' if c.get('url') else '无链接'}")

            print(f"  {mode} 屏 {i + 1:>3} | 本屏广告 {len(cards)} | 累计 {len(found)}")
            if len(found) >= cap:
                break

            if empty_screens >= 2:
                print("  [·] 连续 3 屏无广告卡片，该关键词结果到底")
                break
            if same_screens >= 2:
                print("  [·] 连续 3 屏内容未变化，页面卡住或已到底")
                break

            ad_stall = ad_stall + 1 if len(found) == before else 0
            if ad_stall >= stall_limit:
                print(f"  [!] 连续 {stall_limit} 屏无新增广告，提前结束")
                break

            if not self._safe_swipe():
                print("  [!] 滑动持续失败，跳过该关键词")
                break
            time.sleep(wait)

    def collect_by_industry(self, plan: dict, per_keyword: int = 5) -> list[dict]:
        """按行业链路采集：每个关键词目标新增 per_keyword 条，总数不限

        同一广告主只保留一条：标题/正文里带已采品牌词的笔记直接跳过。
        """
        self._ensure_app()
        self._reset_to_home()

        resume = bool(self.cfg.get_path("android.resume", True))
        found: dict[str, dict] = self._load_partial() if resume else {}
        seen_titles: set[str] = set()
        for c in found.values():
            tk = re.sub(r"[\s\W_]+", "", (c.get("title") or "").lower())[:16]
            if tk:
                seen_titles.add(tk)
        if found:
            print(f"[·] 断点续采：已有 {len(found)} 条")
        known_ids, known_urls = self._merge_history(seen_titles)
        brands, brand_seen = self._load_brands()
        author_seen = self._load_authors()
        print(f"[·] 已知广告主 {len(brands)} 个 / 作者 {len(author_seen)} 个，"
              f"同一广告主、同一作者的其它笔记都会被跳过")

        segs = plan.get("segments") or []
        print(f"[·] 行业：{plan.get('industry')} · {len(segs)} 个环节 · 每词 {per_keyword} 条")

        for idx, seg in enumerate(segs):
            name = seg["name"]
            print(f"\n[{idx + 1}/{len(segs)}] {name} · {seg.get('desc', '')}")

            for kw in seg["keywords"]:
                cap = len(found) + per_keyword
                print(f"  [搜索] {kw}（目标新增 {per_keyword} 条）")
                if not self._open_search(kw):
                    continue
                self._collect_loop("search", cap, found, seen_titles, name, idx,
                                   known_ids=known_ids, known_urls=known_urls,
                                   brands=brands, brand_seen=brand_seen,
                                   author_seen=author_seen, kw=kw)
                got = sum(1 for c in found.values() if c.get("segment") == name)
                print(f"    → {name} 累计 {got} 条")

        items = list(found.values())
        print(f"\n[✓] 共采到 {len(items)} 条广告，覆盖 "
              f"{len({c.get('segment') for c in items if c.get('segment')})} 个环节，"
              f"带链接 {sum(1 for c in items if c.get('url'))} 条")
        return [{
            "note_id": c["note_id"],
            "title": c["title"],
            "desc": c.get("desc", ""),
            "tags": c.get("tags", []),
            "author": c.get("author", ""),
            "liked": c.get("liked", ""),
            "url": c.get("url", ""),
            "is_ad": True,
            "ad_confidence": 0.97,
            "ad_signals": ["App广告标签"] + (["详情页也见广告"] if c.get("detail_has_ad") else []),
            "keyword": c.get("segment") or c.get("source", "App"),
            "segment": c.get("segment", ""),
            "segment_idx": c.get("segment_idx", 0),
            "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        } for c in items]

    def collect(self, target: int = 100) -> list[dict]:
        self._ensure_app()
        self._reset_to_home()
        mode = (self.cfg.get_path("android.mode", "search") or "search").lower()
        resume = bool(self.cfg.get_path("android.resume", True))
        found: dict[str, dict] = self._load_partial() if resume else {}
        seen_titles: set[str] = set()
        if found:
            print(f"[·] 断点续采：已有 {len(found)} 条")
            for c in found.values():
                tk = re.sub(r"[\s\W_]+", "", (c.get("title") or "").lower())[:16]
                if tk:
                    seen_titles.add(tk)
        known_ids, known_urls = self._merge_history(seen_titles)
        brands, brand_seen = self._load_brands()
        author_seen = self._load_authors()
        print(f"[·] 已知广告主 {len(brands)} 个 / 作者 {len(author_seen)} 个，"
              f"同一广告主、同一作者的其它笔记都会被跳过")

        if mode in ("search", "both"):
            keywords = self.cfg.get_path("android.search_keywords", []) or []
            if not keywords:
                raise RuntimeError("android.search_keywords 为空，无法搜索采集")
            print(f"[·] 搜索模式 · {len(keywords)} 个关键词 · 目标 {target} 条")
            for kw in keywords:
                if len(found) >= target:
                    break
                print(f"\n[·] 搜索：{kw}")
                if not self._open_search(kw):
                    continue
                self._collect_loop("search", target, found, seen_titles,
                                   known_ids=known_ids, known_urls=known_urls,
                                   brands=brands, brand_seen=brand_seen,
                                   author_seen=author_seen, kw=kw)

        if mode == "feed":
            print("[·] 信息流模式")
            self._collect_loop("feed", target, found, seen_titles)

        from urllib.parse import quote
        for c in found.values():
            if not c.get("url") and c.get("title"):
                c["url"] = ("https://www.xiaohongshu.com/search_result?keyword="
                            + quote(c["title"][:30]))

        items = list(found.values())[:target]
        print(f"\n[✓] 共采到 {len(items)} 条广告，其中带链接 "
              f"{sum(1 for c in items if c.get('url'))} 条")
        return [{
            "note_id": c["note_id"],
            "title": c["title"],
            "desc": c.get("desc", ""),
            "tags": c.get("tags", []),
            "author": c.get("author", ""),
            "liked": c.get("liked", ""),
            "url": c.get("url", ""),
            "is_ad": True,
            "ad_confidence": 0.97,
            "ad_signals": ["App广告标签"] + (["详情页也见广告"] if c.get("detail_has_ad") else []),
            "keyword": c.get("source", "App"),
            "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        } for c in items]
