# -*- coding: utf-8 -*-
r"""验证「第一次真实发送」是否真的到了平台侧。

本端 `texts_sent=1` 只说明帧发出去了；这里再向平台**只读**拉一次该会话的最近消息，
看里面有没有**卖家侧**发出的那条、正文是否等于草稿——有，才算真送达。

用法：python scripts/xianyu_verify_sent.py [--cid 10000000002] [--limit 5]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "xianyu.db"
API = "http://127.0.0.1:8000"
from local_identity import my_unb_or_exit  # 账号从本机凭据读（仓库里不放真实 id）

MY_UNB = my_unb_or_exit()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cid", default=default_cid())
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    print("=== 1) 本端最近一条已发送草稿 ===")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sent_rows = list(conn.execute(
        "select id, chat_id, message_id, inbound, reply, sent, sent_at from drafts "
        "where sent=1 order by id desc limit 3"))
    if not sent_rows:
        print("  没有 sent=1 的草稿（还没真发过）")
        return 2
    for row in sent_rows:
        print(" ", json.dumps(dict(row), ensure_ascii=False)[:220])
    expected = sent_rows[0]["reply"] or ""
    normalized = expected.replace(" ", "").replace("\n", "")

    print("\n=== 2) 平台侧该会话最近消息（只读）===")
    resp = httpx.post(f"{API}/api/v1/xianyu/fetch-messages",
                      json={"cid": args.cid, "limit": args.limit}, timeout=120)
    resp.raise_for_status()
    body = resp.json()
    found = False
    for msg in body.get("messages") or []:
        is_seller = str(msg.get("sender_id")) == MY_UNB
        text = (msg.get("text") or "").replace(" ", "").replace("\n", "")
        hit = is_seller and normalized and (normalized in text or text in normalized)
        found = found or hit
        print(f"  {msg.get('create_time_iso')} | {'卖家(我)' if is_seller else '买家'} | "
              f"{msg.get('sender_id')} | {msg.get('text')!r}" + ("   <== 就是我们发的那条" if hit else ""))
    print("\n判定:", "✅ 平台侧已存在我们发出的那条消息（真送达）" if found
          else "⚠️ 平台侧还没看到我们发的消息（可能被拒/还在路上）")
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
