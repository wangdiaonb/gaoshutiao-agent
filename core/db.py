# -*- coding: utf-8 -*-
"""SQLite 存储：笔记去重 + 分析结果缓存 + 推送日志"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    note_id      TEXT PRIMARY KEY,
    title        TEXT,
    desc_text    TEXT,
    tags         TEXT,
    author       TEXT,
    liked        TEXT,
    url          TEXT,
    is_ad        INTEGER DEFAULT 0,
    ad_confidence REAL DEFAULT 0,
    ad_signals   TEXT,
    keyword      TEXT,
    segment      TEXT,
    crawled_at   TEXT
);
CREATE TABLE IF NOT EXISTS analysis (
    note_id      TEXT PRIMARY KEY,
    payload      TEXT,
    model        TEXT,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_crawled ON notes(crawled_at);
"""


def connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 老库补列
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
    if "segment" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN segment TEXT")
        conn.commit()
    return conn


def exists(conn: sqlite3.Connection, note_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM notes WHERE note_id = ?", (note_id,))
    return cur.fetchone() is not None


def upsert_note(conn: sqlite3.Connection, note: dict) -> None:
    conn.execute(
        """
        INSERT INTO notes (note_id, title, desc_text, tags, author, liked, url,
                           is_ad, ad_confidence, ad_signals, keyword, segment, crawled_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(note_id) DO UPDATE SET
            title=excluded.title, desc_text=excluded.desc_text, tags=excluded.tags,
            liked=excluded.liked, is_ad=excluded.is_ad,
            ad_confidence=excluded.ad_confidence, ad_signals=excluded.ad_signals,
            crawled_at=excluded.crawled_at
        """,
        (
            note.get("note_id"),
            note.get("title", ""),
            note.get("desc", ""),
            json.dumps(note.get("tags", []), ensure_ascii=False),
            note.get("author", ""),
            note.get("liked", ""),
            note.get("url", ""),
            1 if note.get("is_ad") else 0,
            float(note.get("ad_confidence", 0)),
            json.dumps(note.get("ad_signals", []), ensure_ascii=False),
            note.get("keyword", ""),
            note.get("segment", ""),
            note.get("crawled_at") or datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def get_cached_analysis(conn: sqlite3.Connection, note_id: str) -> dict | None:
    cur = conn.execute("SELECT payload FROM analysis WHERE note_id = ?", (note_id,))
    row = cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def save_analysis(conn: sqlite3.Connection, note_id: str, payload: dict, model: str) -> None:
    conn.execute(
        """INSERT INTO analysis (note_id, payload, model, created_at) VALUES (?,?,?,?)
           ON CONFLICT(note_id) DO UPDATE SET payload=excluded.payload,
           model=excluded.model, created_at=excluded.created_at""",
        (
            note_id,
            json.dumps(payload, ensure_ascii=False),
            model,
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def all_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """历史上采过的所有笔记（用于跨运行永久去重）"""
    return conn.execute("SELECT note_id, title FROM notes").fetchall()


def recent_note_ids(conn: sqlite3.Connection, days: int = 7) -> set[str]:
    """近 N 天已推送过的笔记，避免重复推送"""
    cur = conn.execute(
        "SELECT note_id FROM notes WHERE julianday('now') - julianday(crawled_at) <= ?",
        (days,),
    )
    return {r["note_id"] for r in cur.fetchall()}


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
    ads = conn.execute("SELECT COUNT(*) c FROM notes WHERE is_ad = 1").fetchone()["c"]
    today = conn.execute(
        "SELECT COUNT(*) c FROM notes WHERE date(crawled_at) = date('now','localtime')"
    ).fetchone()["c"]
    return {"total": total, "ads": ads, "today": today}
