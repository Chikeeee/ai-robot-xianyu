# -*- coding: utf-8 -*-
r"""诊断：验证解析器在「字符串键（JSON 形态）」与「整数键（真机 msgpack 形态）」下结果一致。

用法：python scripts/xianyu_key_type_check.py --mid "04c50006 0"
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels.xianyu.ws import (  # noqa: E402
    chat_message_text,
    find_message_node,
    has_key,
    is_chat_message,
    mget,
    parse_inbound,
)

DIGIT_KEYS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13")


def to_int_keys(obj, depth=0):
    """把外层消息表的数字键转成 int（模拟真机 msgpack 编码结果）。

    只转前两层——真机就是这样：外层消息表是 int 键，内层扩展表（10）是 str 键。
    """
    if isinstance(obj, dict) and depth <= 2:
        out = {}
        for k, v in obj.items():
            nk = int(k) if isinstance(k, str) and k.isdigit() else k
            out[nk] = v if depth >= 2 else to_int_keys(v, depth + 1)
        return out
    if isinstance(obj, list) and depth <= 2:
        return [to_int_keys(v, depth + 1) for v in obj]
    return obj


def show(label, payload, expire_ms):
    parent, node, path = find_message_node(payload)
    reason, msg = parse_inbound(payload, "1000000000001", message_id="diag-mid", position=0,
                                message_expire_ms=expire_ms)
    print(f"--- {label} ---")
    print(f"  find_message_node: path={path} node={'None' if node is None else 'dict'}")
    if isinstance(parent, dict):
        print(f"  parent keys(repr) = {[repr(k) for k in list(parent.keys())[:14]]}")
    print(f"  is_chat_message = {is_chat_message(payload)}")
    print(f"  reason = {reason}")
    if msg is None:
        print("  >>> 没有解析出消息")
        return False
    print(f"  chat_id = {msg.chat_id!r}  sender = {msg.sender_id!r}  text = {msg.text!r}")
    print(f"  item_id = {msg.item_id!r}  id = {msg.message_id!r}  id_source = {msg.id_source!r}")
    print(f"  shape = {msg.shape!r}  content_type = {msg.content_type}  create_time = {msg.create_time_ms}")
    return msg.chat_id != "" and msg.chat_id != "None"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dump", default=str(ROOT / ".logs" / "xianyu_frames.jsonl"))
    parser.add_argument("--mid", default="04c50006 0")
    parser.add_argument("--expire-ms", type=int, default=7200000,
                        help="消息过期阈值；诊断旧帧时给大一点（默认 2 小时）")
    args = parser.parse_args(argv)

    payload = None
    for line in Path(args.dump).read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("mid") == args.mid and entry.get("payload"):
            payload = entry["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
    if payload is None:
        print(f"诊断文件里没找到 mid={args.mid} 的帧")
        return 2

    ok_str = show("字符串键（JSON 形态，离线夹具常见）", payload, args.expire_ms)
    ok_int = show("整数键（真机 msgpack 形态）", to_int_keys(payload), args.expire_ms)
    print()
    print(f"两种键类型都能解析出正确会话: str={ok_str} int={ok_int}")
    return 0 if (ok_str and ok_int) else 1


if __name__ == "__main__":
    sys.exit(main())
