# -*- coding: utf-8 -*-
r"""修复诊断探针留下的脏数据：chat_id 为空的行。

背景：真机同步负载的**外层键是整数**，旧解析器取不到会话 id，于是写进了一批
`chat_id=''` 的草稿/消息——所有买家被并成同一个空会话（上下文、接管、限速全乱）。

现在按来源分两类处理，**绝不一把梭删除**：

1. `created_at < 21:54`：**真实买家消息**（键类型 bug 期间的线上流量，如 21:33「你还」、
   21:46「你好」、21:52「几块钱」）→ 只把 `chat_id` 修正为真实会话 id，保留证据。
2. `created_at >= 21:54`：**诊断探针的回放产物**（我用 `XIANYU_SYNC_PTS=zero` 让平台重放
   历史，同一条消息已用正确的 message_id 记录过）→ 这些是重复行，删掉以免污染影子复盘。

用法：python scripts/xianyu_fix_bad_chat_rows.py [--apply]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "xianyu.db"

REAL_CHAT_ID = default_cid()      # 买家小号所在会话（真机抓包确认）
PROBE_CUTOFF = "2026-09-20T21:54"  # 这个时间之后、且 chat_id 为空的行 = 探针回放产物


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--chat-id", default=REAL_CHAT_ID)
    parser.add_argument("--cutoff", default=PROBE_CUTOFF)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    to_fix = list(conn.execute(
        "select id, message_id, inbound, created_at from drafts "
        "where chat_id='' and created_at < ? order by id", (args.cutoff,)))
    to_drop = list(conn.execute(
        "select id, message_id, inbound, created_at from drafts "
        "where chat_id='' and created_at >= ? order by id", (args.cutoff,)))
    msg_fix = list(conn.execute(
        "select count(*) from messages where chat_id='' and ts < ?", (args.cutoff,)))[0][0]
    msg_drop = list(conn.execute(
        "select count(*) from messages where chat_id='' and ts >= ?", (args.cutoff,)))[0][0]

    print(f"真实买家消息（修正 chat_id → {args.chat_id}）：草稿 {len(to_fix)} 条、消息 {msg_fix} 条")
    for row in to_fix:
        print(f"   修正 id={row['id']} {row['created_at']} {row['inbound']!r}")
    print(f"探针回放产物（删除）：草稿 {len(to_drop)} 条、消息 {msg_drop} 条")

    if not args.apply:
        print("\n这是预览。确认无误后加 --apply 执行。")
        return 0

    conn.execute("update drafts set chat_id=? where chat_id='' and created_at < ?",
                 (args.chat_id, args.cutoff))
    conn.execute("update messages set chat_id=? where chat_id='' and ts < ?",
                 (args.chat_id, args.cutoff))
    conn.execute("delete from drafts where chat_id='' and created_at >= ?", (args.cutoff,))
    conn.execute("delete from messages where chat_id='' and ts >= ?", (args.cutoff,))
    conn.commit()

    bad = list(conn.execute("select count(*) from drafts where chat_id=''"))[0][0]
    bad_msg = list(conn.execute("select count(*) from messages where chat_id=''"))[0][0]
    total = list(conn.execute("select count(*) from drafts"))[0][0]
    print(f"\n完成：剩余草稿 {total} 条；空会话草稿 {bad} 条、空会话消息 {bad_msg} 条（都应为 0）")
    print("会话分布：")
    for row in conn.execute("select chat_id, count(*) as n from drafts group by chat_id"):
        print(f"   {row['chat_id']!r}: {row['n']} 条草稿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
