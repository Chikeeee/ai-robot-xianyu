# -*- coding: utf-8 -*-
r"""诊断：最新一条草稿到底发出去了没有（真发送上线后最常问的问题）。

看三处证据：草稿的 sent/sent_at、事件里的 sent/send_blocked/send_failed/send_skipped、
以及平台侧只读回捞是否出现卖家那条。

用法：python scripts/xianyu_last_send_check.py
"""
import json
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "xianyu.db"
API = "http://127.0.0.1:8000"
from local_identity import default_cid, my_unb_or_exit  # 账号/会话从本机读

MY_UNB = my_unb_or_exit()


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=== 最近 3 条草稿（看 sent / sent_at）===")
    for row in conn.execute("select id, chat_id, message_id, inbound, substr(reply,1,40) as reply, "
                            "mode, sent, sent_at, created_at from drafts order by id desc limit 3"):
        print("  ", json.dumps(dict(row), ensure_ascii=False))

    print("\n=== 发送相关事件（最近 20 条）===")
    found = False
    for row in conn.execute("select kind, ts, payload from events order by id desc limit 60"):
        kind = row["kind"]
        if any(k in kind for k in ("sent", "send_", "draft_saved", "duplicate")):
            found = True
            print("  ", row["ts"], "|", kind, "|", str(row["payload"])[:220])
            if row["ts"] < "2026-09-20T22:27":
                break
    if not found:
        print("   没有发送相关事件")

    print("\n=== 当前通道状态 ===")
    try:
        health = httpx.get(f"{API}/api/v1/xianyu/health", timeout=20).json()
        ws = health.get("ws") or {}
        print("   shadow_mode =", health.get("shadow_mode"), " connected =", ws.get("connected"),
              " texts_sent =", ws.get("texts_sent"), " messages =", ws.get("messages"))
        print("   outbound =", json.dumps(health.get("outbound") or {}, ensure_ascii=False)[:240])
    except Exception as exc:
        print("   接口不可用:", exc)

    print("\n=== 平台侧回捞（最近 6 条）===")
    try:
        body = httpx.post(f"{API}/api/v1/xianyu/fetch-messages",
                          json={"cid": default_cid(), "limit": 6}, timeout=120).json()
        for msg in body.get("messages") or []:
            who = "卖家(我)" if str(msg.get("sender_id")) == MY_UNB else "买家"
            print(f"   {msg.get('create_time_iso')} | {who} | {msg.get('text')!r}")
    except Exception as exc:
        print("   查询失败:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
