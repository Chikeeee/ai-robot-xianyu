# -*- coding: utf-8 -*-
r"""开自动发送前的最后核对：确认最近一条真实消息的会话 id，并打印将要发出的帧。

用法：python scripts/xianyu_preflight_send.py
只读：不发送任何东西，只把 `build_text_frame` 的结果打印出来供人工核对。
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.channels.xianyu.ws import build_text_frame  # noqa: E402

DB = ROOT / "data" / "xianyu.db"
from local_identity import my_unb_or_exit  # 账号从本机凭据读（仓库里不放真实 id）

MY_UNB = my_unb_or_exit()


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    print("=== 最近 3 条草稿 ===")
    for row in conn.execute("select id, chat_id, message_id, inbound, reply, intent, mode, sent "
                            "from drafts order by id desc limit 3"):
        print(" ", json.dumps(dict(row), ensure_ascii=False)[:280])

    print("\n=== 最近一条消息记录的会话/买家 ===")
    latest = list(conn.execute(
        "select chat_id, user_id, role from messages where chat_id <> '' order by id desc limit 1"))
    if not latest:
        print("  没有可用记录")
        return 2
    chat_id, user_id = latest[0]["chat_id"], latest[0]["user_id"]
    print(f"  cid={chat_id}  uid={user_id}  role={latest[0]['role']}")

    print("\n=== 将要发出的帧（build_text_frame）===")
    frame = build_text_frame(chat_id, user_id, MY_UNB, "开自动发送前的核对用文本（不会真的发）")
    body = frame["body"]
    head = body[0]
    receivers = (body[1] or {}).get("actualReceivers") or []
    print("  lwp     =", frame["lwp"])
    print("  headers =", json.dumps(frame["headers"], ensure_ascii=False)[:160])
    print("  uuid    =", head.get("uuid"))
    print("  cid     =", head.get("cid"), "  <- 必须是 <cid>@goofish")
    print("  contentType =", (head.get("content") or {}).get("contentType"))
    print("  custom.type =", ((head.get("content") or {}).get("custom") or {}).get("type"))
    print("  receivers =", receivers, "  <- 应含 <买家uid>@goofish 与 <自己uid>@goofish")
    import base64
    data = ((head.get("content") or {}).get("custom") or {}).get("data")
    if data:
        try:
            print("  负载    =", base64.b64decode(data).decode("utf-8"))
        except Exception:
            print("  负载    = <解不开>")

    ok = (str(head.get("cid", "")) == f"{chat_id}@goofish"
          and f"{user_id}@goofish" in receivers
          and f"{MY_UNB}@goofish" in receivers
          and data and bool(chat_id) and bool(user_id))
    print("\n核对结果:", "通过（会话与收件人都是真实 id）" if ok else "不通过，先别开自动发送")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
