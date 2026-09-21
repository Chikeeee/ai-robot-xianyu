# -*- coding: utf-8 -*-
r"""闲鱼通道状态存储（SQLite）：会话历史、人工接管、议价计数、商品缓存、帧幂等、影子草稿、事件流水。

移植自上游 `context_manager.py`（messages / chat_bargain_counts / items 三张表），并补三张表：

| 表 | 来源 | 为什么需要 |
|---|---|---|
| `messages` | 上游 `context_manager.py:39-49` | 会话历史（每会话保留 100 条，超出裁最旧） |
| `chat_state` | 上游 `chat_bargain_counts` + **内存态接管集合**（`main.py:54`） | 上游把「人工接管」放在内存里，**重启即丢**——值守场景最危险；这里落库并带超时 |
| `items` | 上游 `context_manager.py:81-89` | 商品信息缓存（避免每条消息都打接口） |
| `seen_frames` | **新增** | 帧级幂等：重连后平台可能重放同一帧，上游没有去重（会重复回复） |
| `drafts` | **新增** | 影子模式草稿：真连真生成、只落库不发送，供人工比对（P2） |
| `events` | **新增** | 通道事件流水（连上/断开/风控/解码失败…），给值守面板与告警用（P3） |

约定：所有时间都存本地 ISO 字符串（与上游 `datetime.now().isoformat()` 一致）；
每个连接都开 WAL + busy_timeout，避免 FastAPI 线程池与后台任务互相锁。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger("airobot.xianyu.session")

DEFAULT_MAX_HISTORY = 100
DEFAULT_MANUAL_TIMEOUT = 3600  # 上游 main.py:55 MANUAL_MODE_TIMEOUT 默认 1 小时

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    user_id TEXT,
    item_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS chat_state (
    chat_id TEXT PRIMARY KEY,
    manual_mode INTEGER NOT NULL DEFAULT 0,
    manual_since TEXT,
    manual_until TEXT,
    bargain_count INTEGER NOT NULL DEFAULT 0,
    last_intent TEXT,
    last_reply TEXT,
    last_active TEXT
);

CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    title TEXT,
    price REAL,
    description TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS seen_frames (
    mid TEXT PRIMARY KEY,
    chat_id TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen_frames(ts);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    chat_id TEXT,
    item_id TEXT,
    inbound TEXT,
    reply TEXT,
    intent TEXT,
    sources TEXT,
    engine TEXT,
    mode TEXT,
    created_at TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_created ON drafts(created_at);
CREATE INDEX IF NOT EXISTS idx_drafts_chat ON drafts(chat_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    chat_id TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ChannelSessionStore:
    """闲鱼通道状态存储。线程安全由「每次操作一个连接 + WAL」保证。"""

    def __init__(self, db_path: str | Path = "data/xianyu.db",
                 max_history: int = DEFAULT_MAX_HISTORY,
                 seen_keep: int = 20000) -> None:
        self.db_path = Path(db_path)
        self.max_history = max_history
        self.seen_keep = seen_keep
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        logger.info("闲鱼通道状态库就绪: %s", self.db_path)

    # ------------------------------------------------------------------ #
    # 会话历史
    # ------------------------------------------------------------------ #
    def add_message(self, chat_id: str, user_id: str, item_id: Optional[str],
                    role: str, content: str) -> None:
        """写入一条消息并裁剪该会话的历史（对齐上游 context_manager.py:166-210）。"""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (chat_id, user_id, item_id, role, content, ts) VALUES (?,?,?,?,?,?)",
                (chat_id, user_id, item_id, role, content, _now()))
            row = conn.execute(
                "SELECT id FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT 1 OFFSET ?",
                (chat_id, self.max_history)).fetchone()
            if row:
                conn.execute("DELETE FROM messages WHERE chat_id=? AND id<?", (chat_id, row["id"]))
            conn.execute(
                """INSERT INTO chat_state (chat_id, last_active) VALUES (?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET last_active=excluded.last_active""",
                (chat_id, _now()))

    def get_context(self, chat_id: str, limit: Optional[int] = None) -> List[Dict[str, str]]:
        """取会话历史（时间正序）。与上游一致：议价次数以 system 消息形式附在末尾。"""
        limit = limit or self.max_history
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id ASC LIMIT ?",
                (chat_id, limit)).fetchall()
        messages = [{"role": r["role"], "content": r["content"]} for r in rows]
        bargain = self.get_bargain_count(chat_id)
        if bargain > 0:
            messages.append({"role": "system", "content": f"议价次数: {bargain}"})
        return messages

    def message_count(self, chat_id: str) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM messages WHERE chat_id=?",
                                (chat_id,)).fetchone()["c"]

    # ------------------------------------------------------------------ #
    # 议价计数（上游 chat_bargain_counts）
    # ------------------------------------------------------------------ #
    def increment_bargain_count(self, chat_id: str) -> int:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO chat_state (chat_id, bargain_count) VALUES (?, 1)
                   ON CONFLICT(chat_id) DO UPDATE SET bargain_count = bargain_count + 1""",
                (chat_id,))
            row = conn.execute("SELECT bargain_count FROM chat_state WHERE chat_id=?",
                               (chat_id,)).fetchone()
        return int(row["bargain_count"]) if row else 0

    def get_bargain_count(self, chat_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT bargain_count FROM chat_state WHERE chat_id=?",
                               (chat_id,)).fetchone()
        return int(row["bargain_count"]) if row else 0

    def reset_bargain_count(self, chat_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO chat_state (chat_id, bargain_count) VALUES (?, 0)
                   ON CONFLICT(chat_id) DO UPDATE SET bargain_count = 0""", (chat_id,))

    # ------------------------------------------------------------------ #
    # 人工接管（上游是内存集合，这里落库 + 超时）
    # ------------------------------------------------------------------ #
    def enter_manual_mode(self, chat_id: str, timeout_seconds: int = DEFAULT_MANUAL_TIMEOUT) -> None:
        until = (datetime.now() + timedelta(seconds=timeout_seconds)).isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO chat_state (chat_id, manual_mode, manual_since, manual_until)
                   VALUES (?, 1, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET manual_mode=1, manual_since=excluded.manual_since,
                   manual_until=excluded.manual_until""",
                (chat_id, _now(), until))

    def exit_manual_mode(self, chat_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO chat_state (chat_id, manual_mode, manual_since, manual_until)
                   VALUES (?, 0, NULL, NULL)
                   ON CONFLICT(chat_id) DO UPDATE SET manual_mode=0, manual_since=NULL, manual_until=NULL""",
                (chat_id,))

    def is_manual_mode(self, chat_id: str) -> bool:
        """是否处于人工接管；超时自动恢复自动回复（对齐上游 main.py:275-288）。"""
        with self._conn() as conn:
            row = conn.execute("SELECT manual_mode, manual_until FROM chat_state WHERE chat_id=?",
                               (chat_id,)).fetchone()
        if not row or not row["manual_mode"]:
            return False
        until = row["manual_until"]
        if until and datetime.fromisoformat(until) < datetime.now():
            logger.info("会话 %s 人工接管超时，自动恢复自动回复", chat_id)
            self.exit_manual_mode(chat_id)
            return False
        return True

    def toggle_manual_mode(self, chat_id: str, timeout_seconds: int = DEFAULT_MANUAL_TIMEOUT) -> str:
        if self.is_manual_mode(chat_id):
            self.exit_manual_mode(chat_id)
            return "auto"
        self.enter_manual_mode(chat_id, timeout_seconds)
        return "manual"

    def list_manual_chats(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT chat_id, manual_since, manual_until FROM chat_state WHERE manual_mode=1").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 商品缓存（上游 items 表）
    # ------------------------------------------------------------------ #
    def save_item_info(self, item_id: str, data: Dict[str, Any]) -> None:
        try:
            price = float(data.get("soldPrice", 0) or 0)
        except (TypeError, ValueError):
            price = 0.0
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO items (item_id, data, title, price, description, last_updated)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(item_id) DO UPDATE SET data=excluded.data, title=excluded.title,
                   price=excluded.price, description=excluded.description, last_updated=excluded.last_updated""",
                (item_id, json.dumps(data, ensure_ascii=False), data.get("title", ""),
                 price, data.get("desc", ""), _now()))

    def get_item_info(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT data FROM items WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["data"])
        except Exception:
            return None

    def item_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]

    # ------------------------------------------------------------------ #
    # 帧幂等（实现 ws.DedupeStore 的 seen/mark 协议）
    # ------------------------------------------------------------------ #
    def seen(self, key: str) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM seen_frames WHERE mid=?", (key,)).fetchone() is not None

    def mark(self, key: str, chat_id: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO seen_frames (mid, chat_id, ts) VALUES (?,?,?)",
                         (key, chat_id, _now()))
            # 只保留最近 seen_keep 条，避免无限增长
            conn.execute(
                "DELETE FROM seen_frames WHERE mid IN (SELECT mid FROM seen_frames ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                (self.seen_keep,))

    def seen_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM seen_frames").fetchone()["c"]

    # ------------------------------------------------------------------ #
    # 影子模式草稿
    # ------------------------------------------------------------------ #
    def save_draft(self, *, message_id: str, chat_id: str, item_id: Optional[str], inbound: str,
                   reply: str, intent: Optional[str] = None, sources: Optional[List[str]] = None,
                   engine: Optional[str] = None, mode: str = "shadow") -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO drafts (message_id, chat_id, item_id, inbound, reply, intent, sources,
                   engine, mode, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (message_id, chat_id, item_id, inbound, reply, intent,
                 json.dumps(sources or [], ensure_ascii=False), engine, mode, _now()))
            return int(cur.lastrowid)

    def list_drafts(self, limit: int = 20, unsent_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM drafts" + (" WHERE sent=0" if unsent_only else "") + " ORDER BY id DESC LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_draft(self, draft_id: int) -> Optional[Dict[str, Any]]:
        """按 id 取单条草稿（重发失败草稿用）。"""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def last_buyer_id(self, chat_id: str) -> Optional[str]:
        """取该会话最近一条**买家**消息的 uid（重发草稿时需要知道发给谁）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM messages WHERE chat_id=? AND role='user' "
                "ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        return row["user_id"] if row else None

    def mark_draft_sent(self, draft_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE drafts SET sent=1, sent_at=? WHERE id=?", (_now(), draft_id))

    # ------------------------------------------------------------------ #
    # 事件流水
    # ------------------------------------------------------------------ #
    def record_event(self, kind: str, chat_id: Optional[str] = None, **payload: Any) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO events (ts, kind, chat_id, payload) VALUES (?,?,?,?)",
                         (_now(), kind, chat_id, json.dumps(payload, ensure_ascii=False, default=str)))

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except Exception:
                item["payload"] = {}
            out.append(item)
        return out

    # ------------------------------------------------------------------ #
    def stats(self) -> Dict[str, Any]:
        with self._conn() as conn:
            msgs = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            chats = conn.execute("SELECT COUNT(*) AS c FROM chat_state").fetchone()["c"]
            manual = conn.execute("SELECT COUNT(*) AS c FROM chat_state WHERE manual_mode=1").fetchone()["c"]
            drafts = conn.execute("SELECT COUNT(*) AS c FROM drafts").fetchone()["c"]
            unsent = conn.execute("SELECT COUNT(*) AS c FROM drafts WHERE sent=0").fetchone()["c"]
            events = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        return {"messages": msgs, "chats": chats, "manual_chats": manual, "drafts": drafts,
                "unsent_drafts": unsent, "events": events, "items": self.item_count(),
                "seen_frames": self.seen_count(), "db_path": str(self.db_path)}
