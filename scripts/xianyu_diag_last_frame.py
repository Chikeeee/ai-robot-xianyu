# -*- coding: utf-8 -*-
r"""诊断：打印最近一条真机 sync_ok 帧的负载结构，并用真实解析器给出判定。

用法：python scripts/xianyu_diag_last_frame.py [--minutes 15]
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels.xianyu.ws import find_message_node, parse_inbound  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", default=str(ROOT / ".logs" / "xianyu_frames.jsonl"))
    parser.add_argument("--minutes", type=float, default=15)
    parser.add_argument("--kind", default="sync_ok")
    args = parser.parse_args(argv)

    rows = []
    for line in Path(args.dump).read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    cutoff = time.time() - args.minutes * 60
    picked = [d for d in rows if d.get("kind") == args.kind and (d.get("ts") or 0) >= cutoff
              and d.get("payload") is not None]
    if not picked:
        print(f"最近 {args.minutes} 分钟内没有 {args.kind} 帧")
        return 2

    for entry in picked[-3:]:
        payload = entry.get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        print("=" * 78)
        print("ts =", time.strftime("%H:%M:%S", time.localtime(entry["ts"])),
              "| mid =", entry.get("mid"), "| index =", entry.get("index"),
              "| entries =", entry.get("entries"))
        print("top-level keys =", sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)
        parent, node, path = find_message_node(payload)
        print("命中路径 =", path)
        print("parent keys =", sorted(parent.keys()) if isinstance(parent, dict) else type(parent).__name__)
        if isinstance(parent, dict):
            print("  parent[2](cid) =", parent.get("2"))
            print("  parent[3](msgId) =", repr(parent.get("3"))[:40])
            print("  parent[5](createdAt) =", parent.get("5"))
        if isinstance(node, dict):
            print("  node keys(前12) =", sorted(node.keys())[:12])
            print("  node[reminderContent] =", repr(node.get("reminderContent"))[:40])
        reason, msg = parse_inbound(payload, "1000000000001", message_id=entry.get("mid"), position=entry.get("index") or 0)
        if msg is None:
            print("解析结果:", reason)
        else:
            print("解析结果:", reason, "| text =", repr(msg.text)[:30], "| chat =", msg.chat_id,
                  "| sender =", msg.sender_id, "| id =", msg.message_id,
                  "| id_source =", msg.id_source, "| shape =", msg.shape,
                  "| content_type =", msg.content_type, "| create_time =", msg.create_time_ms)
    return 0


if __name__ == "__main__":
    sys.exit(main())
