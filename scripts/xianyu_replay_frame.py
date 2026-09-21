# -*- coding: utf-8 -*-
r"""诊断脚本：把真机抓到的帧喂回**真实收帧路径**，看它被怎么处理。

用途：定位「平台明明推了消息、链路里却没消息」这类问题——把 .logs/xianyu_frames.jsonl 里
某一条真实帧按原样（外层 JSON + body.syncPushPackage.data[].data = base64(msgpack)）重放，
打印 parse_inbound 的判定、丢弃计数与 delivered 结果。

用法：
    python scripts/xianyu_replay_frame.py --mid "0ddd0007 0"
    python scripts/xianyu_replay_frame.py --kind sync_not_chat --last 3
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.channels.xianyu.ws import XianyuWSClient, _structure  # noqa: E402


def pack(obj):
    """最小 MessagePack 打包（与自检套件同一份实现）。"""
    if obj is None:
        return b"\xc0"
    if obj is True:
        return b"\xc3"
    if obj is False:
        return b"\xc2"
    if isinstance(obj, int):
        if 0 <= obj <= 0x7F:
            return bytes([obj])
        if -32 <= obj < 0:
            return bytes([obj & 0xFF])
        if 0 <= obj <= 0xFF:
            return b"\xcc" + struct.pack(">B", obj)
        if 0 <= obj <= 0xFFFF:
            return b"\xcd" + struct.pack(">H", obj)
        if 0 <= obj <= 0xFFFFFFFF:
            return b"\xce" + struct.pack(">I", obj)
        if obj >= 0:
            return b"\xcf" + struct.pack(">Q", obj)
        return b"\xd3" + struct.pack(">q", obj)
    if isinstance(obj, str):
        raw = obj.encode("utf-8")
        if len(raw) <= 31:
            return bytes([0xA0 | len(raw)]) + raw
        if len(raw) <= 0xFF:
            return b"\xd9" + struct.pack(">B", len(raw)) + raw
        return b"\xda" + struct.pack(">H", len(raw)) + raw
    if isinstance(obj, dict):
        head = bytes([0x80 | len(obj)]) if len(obj) <= 15 else b"\xde" + struct.pack(">H", len(obj))
        return head + b"".join(pack(k) + pack(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        head = bytes([0x90 | len(obj)]) if len(obj) <= 15 else b"\xdc" + struct.pack(">H", len(obj))
        return head + b"".join(pack(v) for v in obj)
    raise TypeError(type(obj))


class FakeApi:
    unb = "1000000000001"
    user_agent = "diag-ua"


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)

    async def close(self):
        pass


def load_entries(path: Path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


async def inspect(entry, expire_ms):
    payload = entry.get("payload")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if payload is None:
        return {"error": "该条诊断记录没存全文（需 XIANYU_FRAME_DUMP_FULL=true）"}

    outer = {"headers": {"mid": entry.get("mid") or "diag-mid", "sid": "diag"},
             "body": {"syncPushPackage": {"data": [
                 {"data": base64.b64encode(pack(payload)).decode()}]}}}

    client = XianyuWSClient(FakeApi(), "DIAG-DEVICE")
    client.message_expire_ms = expire_ms
    delivered = []

    async def on_message(msg):
        delivered.append(msg)

    client.on_message = on_message
    await client._handle_frame_inner(outer, FakeWS())
    return {"delivered": [{"chat_id": m.chat_id, "sender_id": m.sender_id, "text": m.text,
                           "item_id": m.item_id} for m in delivered],
            "dropped": client.stats["dropped"], "messages": client.stats["messages"],
            "duplicates": client.stats["duplicates"], "frame_errors": client.stats["frame_errors"],
            "decode_errors": client.stats["decode_errors"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把真机抓到的帧喂回真实收帧路径做诊断")
    parser.add_argument("--dump", default=str(ROOT / ".logs" / "xianyu_frames.jsonl"))
    parser.add_argument("--mid", default=None, help="按外层帧 mid 精确挑一条（如 \"0ddd0007 0\"）")
    parser.add_argument("--kind", default=None, help="按诊断类别挑（如 sync_not_chat）")
    parser.add_argument("--last", type=int, default=1, help="挑最后 N 条")
    parser.add_argument("--expire-ms", type=int, default=300000, help="消息过期阈值（默认 5 分钟）")
    args = parser.parse_args(argv)

    entries = load_entries(Path(args.dump))
    picked = [e for e in entries if e.get("payload") is not None]
    if args.mid:
        picked = [e for e in picked if e.get("mid") == args.mid]
    if args.kind:
        picked = [e for e in picked if e.get("kind") == args.kind]
    picked = picked[-args.last:]

    if not picked:
        print("没找到符合条件的帧（可用 --kind/--mid 缩小范围）")
        return 2

    for entry in picked:
        print("=" * 78)
        print(f"kind={entry.get('kind')} mid={entry.get('mid')} index={entry.get('index')} "
              f"entries={entry.get('entries')}")
        print("payload_structure:", json.dumps(entry.get("payload_structure"), ensure_ascii=False)[:400])
        result = asyncio.run(inspect(entry, args.expire_ms))
        print("结果:", json.dumps(result, ensure_ascii=False)[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
