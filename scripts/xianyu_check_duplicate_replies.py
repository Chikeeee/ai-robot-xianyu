# -*- coding: utf-8 -*-
r"""诊断：有没有「同一条消息被答了两次」（真发送下这是要命的，会给买家重复回复）。"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "xianyu.db"


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "select message_id, count(*) as n, group_concat(id) as ids, group_concat(sent) as sent_flags "
        "from drafts group by message_id having n > 1 order by n desc"))
    print(f"重复 message_id 的草稿组数 = {len(rows)}")
    for row in rows[:10]:
        print("  ", dict(row))
    total = list(conn.execute("select count(*) from drafts"))[0][0]
    print(f"草稿总数 = {total}")
    print("\n结论:", "存在重复答案（需要加消息级幂等）" if rows else "暂未出现重复答案")
    return 0 if not rows else 1


if __name__ == "__main__":
    sys.exit(main())
