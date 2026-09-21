# -*- coding: utf-8 -*-
r"""闲鱼 WSS 客户端离线自检：本地起一个假 goofish 服务器，跑真实 WebSocket 协议。

**不连闲鱼、不发任何真实消息**——只为验证连接层行为：
注册帧字段、心跳 mid 精确对账、单次 ACK、消息幂等、5 类帧分类、指数退避重连、心跳超时判死、
以及出站发送帧结构。

用法：python scripts/test_xianyu_ws.py
"""
import asyncio
import base64
import json
import os
import struct
import sys
import time
from pathlib import Path

import httpx
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

from app.channels.xianyu.api import IM_APP_KEY, MTOP_TOKEN_URL, XianyuApi  # noqa: E402
from app.channels.xianyu.protocol import parse_cookie_str  # noqa: E402
from app.channels.xianyu.ws import (  # noqa: E402
    InboundMessage,
    XianyuWSClient,
    auth_error,
    build_text_frame,
    history_text,
    is_sync_package,
    order_status,
    parse_history_message,
    parse_inbound,
)

RESULTS = []
REAL_CRED = ROOT / "secrets" / "xianyu_credentials.json"
FAKE_TOKEN = "FAKE-ACCESS-TOKEN"
DEVICE_ID = "C882C442-D8C9-4A69-A9B0-A59EFDD765C8-1000000000001"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


# ---------------- 测试用 MessagePack 打包器（与 protocol 自检里同一份逻辑） ----------------
def pack(obj):
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
    if isinstance(obj, float):
        return b"\xcb" + struct.pack(">d", obj)
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


def chat_frame(mid, sender_id, text, item_id="888888", age_ms=0, chat_id="C1"):
    now_ms = int(time.time() * 1000) - age_ms
    payload = {
        "1": {
            "2": f"{chat_id}@goofish",
            "5": now_ms,
            "10": {
                "senderUserId": sender_id,
                "reminderTitle": "买家A",
                "reminderContent": text,
                "reminderUrl": f"https://www.goofish.com/item?itemId={item_id}&x=1",
            },
        }
    }
    b64 = base64.b64encode(pack(payload)).decode()
    return {"headers": {"mid": mid, "sid": "sid-1"},
            "body": {"syncPushPackage": {"data": [{"data": b64}]}}}


def multi_chat_frame(mid, messages, chat_id="C1", item_id="888888"):
    """一条同步包塞**多条**消息（真机实测：13 条数据里 7 条消息，全部共用同一个外层 mid）。

    这些消息在紧凑形里没有各自的消息 id，若拿外层 mid 当幂等键，第 2 条起会被误判成重复丢弃。
    """
    data = []
    for sender_id, text, created in messages:
        payload = {"1": {"2": f"{chat_id}@goofish", "5": created,
                         "10": {"senderUserId": sender_id, "reminderTitle": "买家A",
                                "reminderContent": text,
                                "reminderUrl": f"https://www.goofish.com/item?itemId={item_id}&x=1"}}}
        data.append({"data": base64.b64encode(pack(payload)).decode()})
    return {"headers": {"mid": mid, "sid": "sid-1"}, "body": {"syncPushPackage": {"data": data}}}


def typing_frame(mid):
    """真实的「正在输入」帧：提示内容在 MessagePack 负载里，形状是 {"1": [{"1": "...@goofish"}]}。"""
    payload = {"1": [{"1": "someone@goofish"}]}
    b64 = base64.b64encode(pack(payload)).decode()
    return {"headers": {"mid": mid}, "body": {"syncPushPackage": {"data": [{"data": b64}]}}}


def order_frame(mid, status="等待买家付款"):
    payload = {"1": "buyer@goofish", "3": {"redReminder": status}}
    b64 = base64.b64encode(pack(payload)).decode()
    return {"headers": {"mid": mid}, "body": {"syncPushPackage": {"data": [{"data": b64}]}}}


class FakeGoofish:
    """假 goofish 服务端：记录收到的帧，按场景回应。"""

    def __init__(self, *, ack_heartbeat=True, push_frames=True, close_after=None,
                 session_invalid_on_request=False, custom_frames=None):
        self.ack_heartbeat = ack_heartbeat
        self.push_frames = push_frames
        self.close_after = close_after
        self.session_invalid_on_request = session_invalid_on_request
        self.custom_frames = custom_frames or []
        self.received = []
        self.reg_frames = []
        self.ackdiff_frames = []
        self.heartbeat_mids = []
        self.acks_from_client = []
        self.reg_count = 0
        self.connections = 0
        self.pushed_with_mid = 0
        self._server = None
        self.port = None
        self._pushed = False

    async def _push(self, ws, frame):
        """向客户端推一帧并记账（用于核对客户端 ACK 次数）。"""
        if (frame.get("headers") or {}).get("mid"):
            self.pushed_with_mid += 1
        await ws.send(json.dumps(frame))

    async def start(self):
        # 真机重放是**整帧一模一样**（含负载里的 createAt），跨连接也必须推同一份负载；
        # 若每次连接都重新生成时间戳，就等于在模拟"平台发了内容不同的另一条消息"，
        # 跨重连去重（以及 buyer 连发两句）根本测不出来——本机自测踩过这个坑。
        self._replay_frame = chat_frame("m-1", "11112222", "这个还在吗？")
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws):
        self.connections += 1
        first = True
        try:
            async for raw in ws:
                frame = json.loads(raw)
                self.received.append(frame)
                lwp = frame.get("lwp")
                if lwp == "/reg":
                    self.reg_count += 1
                    self.reg_frames.append(frame)
                elif lwp == "/r/SyncStatus/ackDiff":
                    self.ackdiff_frames.append(frame)
                elif lwp == "/!":
                    mid = (frame.get("headers") or {}).get("mid")
                    self.heartbeat_mids.append(mid)
                    if self.ack_heartbeat and mid:
                        await ws.send(json.dumps({"code": 200, "headers": {"mid": mid}}))
                elif frame.get("code") == 200:
                    self.acks_from_client.append(frame)
                elif is_sync_package(frame):
                    pass
                elif self.session_invalid_on_request and lwp:
                    # 真机形状：平台**不关连接**，只对请求回 401 + token is not found
                    await ws.send(json.dumps({
                        "code": 401, "headers": {"mid": (frame.get("headers") or {}).get("mid"), "dt": "j"},
                        "body": {"reason": "token is not found", "code": "4000001", "scope": "reg"}}))

                if first and self.push_frames:
                    first = False
                    await asyncio.sleep(0.3)
                    # 同 mid、同负载推两次（跨连接也用同一份 self._replay_frame）
                    await self._push(ws, self._replay_frame)
                    await asyncio.sleep(0.2)
                    await self._push(ws, self._replay_frame)  # 重放
                    await asyncio.sleep(0.2)
                    await self._push(ws, typing_frame("m-typing"))
                    await asyncio.sleep(0.2)
                    await self._push(ws, order_frame("m-order"))
                    if self.close_after is not None:
                        await asyncio.sleep(self.close_after)
                        await ws.close()
                elif first and self.custom_frames:
                    first = False
                    await asyncio.sleep(0.3)
                    for extra in self.custom_frames:
                        await self._push(ws, extra)
                        await asyncio.sleep(0.3)
                    if self.close_after is not None:
                        await asyncio.sleep(self.close_after)
                        await ws.close()
        except Exception:
            pass


async def make_api(port):
    """用 MockTransport 提供 token 接口，其余走真实 XianyuApi 代码路径。"""
    cookies = json.loads(REAL_CRED.read_text(encoding="utf-8"))
    token_body = {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": FAKE_TOKEN}}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(MTOP_TOKEN_URL):
            return httpx.Response(200, json=token_body)
        return httpx.Response(200, json={"content": {"success": True}})

    return XianyuApi(parse_cookie_str(cookies["cookies_str"]),
                     transport=httpx.MockTransport(handler), credential_path=None)


async def scenario_normal():
    server = await FakeGoofish(ack_heartbeat=True, push_frames=True, close_after=1.5).start()
    api = await make_api(server.port)
    got: list[InboundMessage] = []
    events = []

    async def on_message(msg):
        got.append(msg)

    client = XianyuWSClient(api, DEVICE_ID, url=f"ws://127.0.0.1:{server.port}/",
                            on_message=on_message, on_event=lambda k, p: events.append((k, p)),
                            heartbeat_interval=1, heartbeat_timeout=1,
                            reconnect_initial=0.2, reconnect_max=0.5, max_reconnects=3)
    task = asyncio.create_task(client.run())
    deadline = time.time() + 25
    while time.time() < deadline:
        if server.reg_count >= 2 and len(got) >= 1:
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(1.0)  # 留出心跳/重连窗口
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await server.stop()
    await api.aclose()

    reg = server.reg_frames[0] if server.reg_frames else {}
    headers = reg.get("headers", {})
    check("注册帧 /reg 字段与上游一致（app-key/token/ua/did/wv/sync/dt/mid）",
          headers.get("app-key") == IM_APP_KEY and headers.get("token") == FAKE_TOKEN
          and headers.get("did") == DEVICE_ID and headers.get("wv") == "im:3,au:3,sy:6"
          and headers.get("sync") == "0,0;0;0;" and headers.get("dt") == "j"
          and headers.get("ua", "").startswith("Mozilla/5.0") and headers.get("mid"),
          f"app-key={headers.get('app-key')} did 后缀={str(headers.get('did'))[-8:]}")
    check("注册后发送 ackDiff（含 pts/seq/pipeline）",
          bool(server.ackdiff_frames) and server.ackdiff_frames[0]["body"][0].get("pipeline") == "sync"
          and server.ackdiff_frames[0]["body"][0].get("seq") == 0,
          f"ackDiff 帧数={len(server.ackdiff_frames)}")
    check("心跳按 /! 发送且带 mid；被 ACK 后不判死",
          bool(server.heartbeat_mids) and all(m for m in server.heartbeat_mids)
          and client.stats["heartbeat_timeouts"] == 0,
          f"心跳 {len(server.heartbeat_mids)} 次，超时 {client.stats['heartbeat_timeouts']} 次")

    check("入站聊天消息解析正确（chat_id/发送者/文本/商品号）",
          len(got) == 1 and got[0].chat_id == "C1" and got[0].sender_id == "11112222"
          and got[0].text == "这个还在吗？" and got[0].item_id == "888888",
          f"on_message 调用 {len(got)} 次（跨重连仍为 1），text={got[0].text if got else '-'}")
    check("幂等键取帧 mid：重放（含跨重连重放）被丢弃（上游无去重，会重复回复）",
          client.stats["duplicates"] >= 1 and client.stats["dropped"].get("duplicate", 0) >= 1
          and client.stats["messages"] == 1,
          f"duplicates={client.stats['duplicates']} messages={client.stats['messages']}")

    check("一帧只 ACK 一次（上游同一帧 ACK 两次）",
          len(server.acks_from_client) == server.pushed_with_mid and server.pushed_with_mid >= 4,
          f"服务端推带 mid 帧 {server.pushed_with_mid} 个 → 客户端 ACK {len(server.acks_from_client)} 次")
    check("帧分类落点正确（typing 丢弃 / order_status 上报 / 聊天入链路）",
          client.stats["dropped"].get("typing", 0) >= 1
          and any(k == "order_status" for k, _ in events)
          and any(k == "message" for k, _ in events)
          and client.stats.get("frame_errors", 0) == 0,
          f"dropped={client.stats['dropped']} frame_errors={client.stats.get('frame_errors')}")
    check("解码路径记录为 base64+msgpack", any(p.get("decode_path") == "base64+msgpack" for k, p in events if k == "message"),
          str([p.get("decode_path") for k, p in events if k == "message"]))
    check("断线后按指数退避重连并重新注册", server.reg_count >= 2 and client.stats["reconnects"] >= 1,
          f"注册 {server.reg_count} 次，重连 {client.stats['reconnects']} 次，连接 {server.connections} 次")
    check("健康快照可用于面板/告警", client.health().get("reconnects") is not None
          and client.health().get("my_id", "").endswith("***"),
          json.dumps({k: v for k, v in client.health().items() if k in ("connected", "frames_in", "messages")}, ensure_ascii=False))


async def scenario_heartbeat_timeout():
    """服务端收到心跳不回：客户端必须在 interval+timeout 内判死并重连。"""
    server = await FakeGoofish(ack_heartbeat=False, push_frames=False).start()
    api = await make_api(server.port)
    client = XianyuWSClient(api, DEVICE_ID, url=f"ws://127.0.0.1:{server.port}/",
                            heartbeat_interval=1, heartbeat_timeout=1,
                            reconnect_initial=0.1, reconnect_max=0.2, max_reconnects=0)
    task = asyncio.create_task(client.run())
    deadline = time.time() + 20
    while time.time() < deadline and client.stats["heartbeat_timeouts"] == 0:
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await server.stop()
    await api.aclose()
    check("心跳无响应在 interval+timeout 内判死（上游任意 200 帧就算活着）",
          client.stats["heartbeat_timeouts"] >= 1,
          f"超时 {client.stats['heartbeat_timeouts']} 次，心跳发出 {client.stats['heartbeats_sent']} 次")


def scenario_send_frame():
    frame = build_text_frame("C1", "11112222", "1000000000001", "好的，给您留着～")
    body = frame.get("body", [{}, {}])
    payload = json.loads(base64.b64decode(body[0]["content"]["custom"]["data"]).decode())
    check("发送帧结构与上游一致（lwp/cid/contentType101/base64 负载/actualReceivers）",
          frame["lwp"] == "/r/MessageSend/sendByReceiverScope"
          and body[0]["cid"] == "C1@goofish" and body[0]["content"]["contentType"] == 101
          and payload == {"contentType": 1, "text": {"text": "好的，给您留着～"}}
          and body[1]["actualReceivers"] == ["11112222@goofish", "1000000000001@goofish"],
          f"cid={body[0]['cid']} receivers={body[1]['actualReceivers']}")


def scenario_token_refresh_cadence():
    """token 刷新周期：必须按 `token_refresh_interval` 生效，不能每轮都刷（真机踩过的坑）。"""
    from app.channels.xianyu.ws import XianyuWSClient

    class Dummy:
        unb = "1000000000001"
        user_agent = "UA"
        cookies = {}

        def cookie_str(self):
            return "unb=1"

    client = XianyuWSClient.__new__(XianyuWSClient)
    client.token_refresh_interval = 3600
    client.last_token_refresh_time = 0.0
    now = 1_000_000.0
    client.last_token_refresh_time = now
    check("刚取到 token 时不刷新（避免每分钟重连）",
          client.should_refresh_token(now + 60) is False
          and client.should_refresh_token(now + 3599) is False,
          "60s/3599s → False")
    check("到达 token_refresh_interval 才刷新",
          client.should_refresh_token(now + 3600) is True and client.should_refresh_token(now + 7200) is True,
          "3600s/7200s → True")
    check("检查周期封顶 60 秒且不小于 5 秒",
          client.token_check_interval() == 60.0, str(client.token_check_interval()))
    client.token_refresh_interval = 3
    check("极短的刷新周期也至少 5 秒检查一次（不会打爆接口）",
          client.token_check_interval() == 5.0, str(client.token_check_interval()))


def scenario_frame_dump():
    """帧诊断开关：能写结构摘要、不把买家正文整段落盘（本轮真机排查用）。"""
    import io as _io

    from app.channels.xianyu.ws import FrameDump, XianyuWSClient, _structure

    path = ROOT / ".logs" / "ktest_frames.jsonl"
    path.unlink(missing_ok=True)
    long_text = "买家正文不应该被整段落盘" * 3      # >24 字：默认模式只记长度
    dump = FrameDump(path, max_bytes=2000, full=False)
    dump.write({"kind": "sync_not_chat", "payload_structure": _structure(
        {"1": {"2": "C1@goofish", "5": 1789907123456, "10": {"reminderContent": long_text}}})})
    text = _io.open(path, encoding="utf-8").read()
    check("帧诊断（默认模式）：长正文只记长度、不落原文（短 id 仍保留值便于排查）",
          "reminderContent" in text and "str[" in text and long_text not in text
          and "C1@goofish" in text,
          text.strip()[:140])

    path.unlink(missing_ok=True)
    full_dump = FrameDump(path, full=True)
    full_dump.write({"kind": "sync_ok"}, payload={"1": {"10": {"reminderContent": "排查时必须看到正文"}}})
    full_text = _io.open(path, encoding="utf-8").read()
    check("帧诊断（full 模式）：连负载一起记，供排查消息字段",
          "排查时必须看到正文" in full_text, full_text.strip()[:130])

    class DummyApi:
        unb = "1000000000001"
        user_agent = "UA"

        def cookie_str(self):
            return "unb=1"

    # 构造函数带 dump 路径：这一步能抓到「FrameDump 里用了没 import 的 Path」这类仅在运行期炸的问题
    client = XianyuWSClient(DummyApi(), "DEV-1", frame_dump=str(path))
    check("带帧诊断路径构造客户端不报错（Path 等依赖齐全）", client.dump is not None, str(client.dump.path.name))

    path.write_text("x" * 3000, encoding="utf-8")
    dump.write({"kind": "after_rotate"})
    check("诊断文件超过上限会自动清空重开（不会无限长大）",
          "after_rotate" in path.read_text(encoding="utf-8"), "已轮转")


def scenario_real_payload_regression():
    """真机抓包回归：买家消息负载长什么样、判定顺序为什么关键（第 13 轮踩的坑）。"""
    from app.channels.xianyu.ws import is_chat_message, is_system_message, parse_inbound

    def payload(need_push):
        return {"1": {"1": {"1": "1000000000003@goofish"}, "2": "10000000002@goofish",
                      "3": "4318129569996.PNM", "4": 1789908889000, "5": int(time.time() * 1000),
                      "7": 1, "8": 0, "9": 1, "12": 0,
                      "10": {"_platform": "android", "reminderContent": "你好", "reminderTitle": "x***1",
                             "senderUserId": "1000000000003",
                             "reminderUrl": "https://www.goofish.com/item?itemId=888888"}},
                "3": {"needPush": need_push}}

    ok_true = parse_inbound(payload("true"), "1000000000001", message_id="m1")
    ok_false = parse_inbound(payload("false"), "1000000000001", message_id="m2")
    check("★ 真机负载回归：needPush=true 时解析出聊天消息（正文/会话/发送者/商品号）",
          ok_true[0] == "ok" and ok_true[1] and ok_true[1].text == "你好"
          and ok_true[1].chat_id == "10000000002" and ok_true[1].sender_id == "1000000000003"
          and ok_true[1].item_id == "888888",
          f"reason={ok_true[0]} msg={ok_true[1].text if ok_true[1] else None}")
    check("★ 判定顺序：needPush=false 但带正文时仍要当聊天消息（否则真实买家消息会被当系统消息丢掉）",
          ok_false[0] == "ok" and ok_false[1] is not None
          and is_chat_message(payload("false")) and is_system_message(payload("false")),
          f"reason={ok_false[0]}（旧顺序会返回 system 并丢弃）")

    # 会话列表推送（真机里与消息同时出现）不应被当成消息，也不该被标成「正在输入」
    conv_list = {"1": [{"1": "10000000002@goofish", "2": 1, "3": 0, "4": "1000000000001@goofish"}]}
    reason = parse_inbound(conv_list, "1000000000001", message_id="m3")[0]
    check("会话列表推送单独识别为 conv_list（不再误标成 typing，计数才可信）",
          reason == "conv_list", reason)


def scenario_history_and_auth():
    r"""历史消息解析 + 会话失效识别（真机踩过的两个坑的回归）。"""
    # 1) 真机字段：content.custom.data = base64({"text":{"text":"你好"}})
    real = {"message": {
        "content": {"custom": {"data": base64.b64encode(
            json.dumps({"atUsers": [], "contentType": 1, "text": {"text": "你好"}}).encode()).decode(),
            "summary": "你好", "type": 1}},
        "extension": {"senderUserId": "1000000000003", "reminderTitle": "x***1"},
        "createAt": 1789908885977, "messageId": "4318129569996.PNM", "cid": "10000000002@goofish"}}
    parsed = parse_history_message(real)
    check("历史消息按 content.custom.data 解出文本", parsed["text"] == "你好", f"text={parsed['text']!r}")
    check("历史消息文本来源可见（不再靠猜）", parsed["text_source"] == "custom.data.text",
          parsed["text_source"])
    check("历史消息时间用 createAt（旧代码读 createTime，一直是 None）",
          parsed["create_time_iso"] == time.strftime("%Y-%m-%d %H:%M:%S",
                                                    time.localtime(1789908885977 / 1000)),
          str(parsed["create_time_iso"]))

    # 2) 平台系统通知：没有 custom.data，只有 summary（旧代码在这里静默返回空）
    notice = {"message": {"content": {"custom": {"summary": "您的宝贝有人来询单啦", "type": 14}},
                          "extension": {"senderUserId": "1000000000003", "reminderTitle": "x***1"}}}
    p2 = parse_history_message(notice)
    check("无 custom.data 时回退到 custom.summary", p2["text"] == "您的宝贝有人来询单啦",
          f"{p2['text']!r} source={p2['text_source']}")
    check("★ 平台提示卡（type=14）标记为非买家文本（否则会去回复平台通知）",
          p2["is_plain_text"] is False and p2["content_type"] == 14,
          f"is_plain_text={p2['is_plain_text']} type={p2['content_type']}")
    check("买家真实文本 type=1 标记为可回复",
          parse_history_message({**real, "message": {**real["message"],
                                                     "content": {"custom": {"type": 1, "summary": "你好"}}}})["is_plain_text"] is True)

    # 3) 真的一无所有：必须是 none，与「解析失败」区分开
    check("空消息标记 source=none（解析失败会标 error）",
          history_text({}) == ("", "none"), str(history_text({})))

    # 4) 会话失效帧识别：平台不关连接，只回 401 "token is not found"
    check("识别 401 token is not found（静默僵尸连接）",
          auth_error({"code": 401, "body": {"reason": "token is not found",
                                            "code": "4000001", "scope": "reg"}}) == "token is not found",
          str(auth_error({"code": 401, "body": {"reason": "token is not found"}})))
    check("正常 200 帧不误判成失效", auth_error({"code": 200, "body": {"x": 1}}) is None)
    check("普通 400 帧不算会话失效", auth_error({"code": 400}) is None)


async def scenario_request_error_surface():
    """平台报错必须抛出，不能静默退化成「拉到了 0 条」。"""
    class FakeApi:
        unb = "1000000000001"
        user_agent = "fake-ua"

    # 不传 frame_dump：离线测试绝不往真实诊断文件里写
    client = XianyuWSClient(FakeApi(), DEVICE_ID)

    class FakeWS:
        async def send(self, raw):
            self.sent = raw

    for frame, label in (({"code": 401, "body": {"reason": "token is not found"}}, "401 会话失效"),
                         ({"code": 400}, "400 空 body")):
        ws = FakeWS()
        client._current_ws = ws
        task = asyncio.ensure_future(client.request("/r/x", []))
        await asyncio.sleep(0.05)
        mid = json.loads(ws.sent)["headers"]["mid"]
        client._pending_requests[mid].set_result(frame)
        try:
            await task
            ok, detail = False, "没有抛异常（会伪装成 0 条消息）"
        except RuntimeError as exc:
            ok, detail = True, str(exc)[:60]
        check(f"request() 对「{label}」抛出而非返回空结果", ok, detail)

    # 正常 200 原样返回
    ws = FakeWS()
    client._current_ws = ws
    task = asyncio.ensure_future(client.request("/r/x", []))
    await asyncio.sleep(0.05)
    mid = json.loads(ws.sent)["headers"]["mid"]
    client._pending_requests[mid].set_result({"code": 200, "body": {"ok": True}})
    frame = await task
    check("request() 正常 200 帧原样返回", frame["body"] == {"ok": True}, str(frame.get("body")))


async def scenario_session_invalid_recovery():
    r"""真机踩坑回归：平台不关连接，只回 401 "token is not found"。

    旧行为：该帧被当普通非同步帧丢掉 → health 一直 connected=true/registered=true，
    实际收不到任何消息（静默僵尸连接），值守形同虚设。
    期望：识别失效 → 触发换 token 重连 → 重连后的注册帧必须带**新 token**。
    """
    server = await FakeGoofish(ack_heartbeat=True, push_frames=False,
                               session_invalid_on_request=True).start()
    cookies = json.loads(REAL_CRED.read_text(encoding="utf-8"))
    tokens = ["TOKEN-1", "TOKEN-2"]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(MTOP_TOKEN_URL):
            tok = tokens[min(len(tokens) - 1, token_calls[0])]
            token_calls[0] += 1
            return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"],
                                             "data": {"accessToken": tok}})
        return httpx.Response(200, json={"content": {"success": True}})

    token_calls = [0]
    api = XianyuApi(parse_cookie_str(cookies["cookies_str"]),
                    transport=httpx.MockTransport(handler), credential_path=None)
    events = []
    client = XianyuWSClient(api, DEVICE_ID, url=f"ws://127.0.0.1:{server.port}/",
                            on_event=lambda k, p: events.append((k, p)),
                            heartbeat_interval=1, heartbeat_timeout=1,
                            reconnect_initial=0.2, reconnect_max=0.5, max_reconnects=3)

    task = asyncio.create_task(client.run())
    # 等注册完成（_register 里有 1 秒等待）后反复尝试查询，直到拿到「会话已失效」
    deadline = time.time() + 20
    seen_invalid_health = None
    while time.time() < deadline:
        try:
            await client.request("/r/MessageManager/listUserMessages", [], timeout=3)
        except RuntimeError as exc:
            if "会话已失效" in str(exc):
                seen_invalid_health = client.health().get("session_invalid")
                break
        except Exception:
            pass
        await asyncio.sleep(0.3)
    deadline = time.time() + 15
    while time.time() < deadline and server.reg_count < 2:
        await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await server.stop()
    await api.aclose()

    check("★ 收到 401 token is not found 时记为会话失效（不再静默丢弃）",
          client.stats.get("auth_errors", 0) >= 1
          and any(k == "session_invalid" for k, _ in events),
          f"auth_errors={client.stats.get('auth_errors')} events={[k for k, _ in events if k == 'session_invalid']}")
    check("★ 会话失效后自动重连（值守不会停在僵尸连接上）", server.reg_count >= 2,
          f"注册 {server.reg_count} 次")
    check("★ 重连注册用的是**新 token**（旧 token 已不可用）",
          len(server.reg_frames) >= 2
          and server.reg_frames[0]["headers"]["token"] == "TOKEN-1"
          and server.reg_frames[-1]["headers"]["token"] == "TOKEN-2",
          f"reg tokens={[f['headers'].get('token') for f in server.reg_frames]}")
    check("health 暴露 session_invalid 字段（面板/告警可判死）", seen_invalid_health is True,
          f"失效瞬间 session_invalid={seen_invalid_health}")


def scenario_full_message_shape():
    r"""★ 真机「完整消息形」回归：2026-09-20 21:26:51 平台推来的真实帧。

    这条帧当时被判成 `not_chat` **静默丢弃**（买家消息明明推到了连接上，值守却没反应）。
    形状与紧凑形不同：正文在 `1.10`，发送者在 `1.1.1`（`<uid>@goofish`），消息 id 在 `1.3`。
    现在解析器改为有界递归找「带文本的消息节点」，这条必须能解析出来。
    """
    real_full = {
        "1": {
            "1": {"1": "1000000000003@goofish"},
            "2": "10000000002@goofish",
            "3": "4306436296909.PNM",
            "4": 0,
            "5": int(time.time() * 1000),
            "6": {"1": 101, "3": {"1": "", "2": "你好", "3": "", "4": 1,
                                  "5": json.dumps({"atUsers": [], "contentType": 1,
                                                   "text": {"text": "你好"}})}},
            "7": 2, "8": 1, "9": 0,
            "10": {"reminderContent": "你好", "detailNotice": "你好", "senderUserId": "1000000000003",
                   "reminderTitle": "x***1", "sessionType": "1", "senderUserType": "0",
                   "reminderUrl": "fleamarket://message_chat?itemId=1012341728505&peerUserId=1000000000003"},
            "12": 1,
        },
        "3": {"needPush": "true"},
    }
    reason, msg = parse_inbound(real_full, "1000000000001", message_id="0ddd0007 0")
    check("★ 真机完整消息形能解析出买家消息（旧实现静默丢弃 → 值守没反应）",
          reason == "ok" and msg is not None, f"reason={reason}")
    if msg:
        check("★ 完整消息形：正文/会话/发送者/商品号都对",
              msg.text == "你好" and msg.chat_id == "10000000002"
              and msg.sender_id == "1000000000003" and msg.item_id == "1012341728505",
              f"text={msg.text!r} chat={msg.chat_id} sender={msg.sender_id} item={msg.item_id}")
        check("完整消息形：幂等键用负载里的消息 id（跨重连重放才判得住）",
              msg.message_id == "4306436296909.PNM", msg.message_id)
        check("命中的形状被记录下来（便于发现平台再次改嵌套）",
              bool(msg.shape), f"shape={msg.shape}")

    # 完整消息形里的会话列表推送仍然不能被当成消息
    conv_full = {"1": {"1": [{"1": "10000000002@goofish", "2": 1, "3": 1,
                              "4": "1000000000001@goofish"}]}}
    check("完整消息形同包的会话列表推送仍判为 conv_list（不误判成消息）",
          parse_inbound(conv_full, "1000000000001", message_id="m-cl")[0] in ("conv_list", "not_chat"),
          parse_inbound(conv_full, "1000000000001", message_id="m-cl")[0])


async def scenario_multi_message_package():
    r"""★ 真机回归：**一条同步包含多条消息且共用外层 mid**。

    2026-09-20 实测：平台一条同步包里有 13 条数据、其中 7 条是消息，全部共用外层 mid `77200003 0`。
    紧凑形消息自身没有消息 id，若拿外层 mid 当幂等键 → 第 2 条起全被判「重复」静默丢弃
    （买家连发两句，只有第一句会被回答）。
    """
    now = int(time.time() * 1000)
    package = multi_chat_frame("pkg-mid-1", [
        ("11112222", "在吗", now - 3000),
        ("11112222", "能便宜点吗", now - 2000),
        ("11112222", "发货快吗", now - 1000),
    ])
    server = await FakeGoofish(ack_heartbeat=True, push_frames=False,
                               custom_frames=[package]).start()
    api = await make_api(server.port)
    got = []

    async def on_message(msg):
        got.append(msg)

    client = XianyuWSClient(api, DEVICE_ID, url=f"ws://127.0.0.1:{server.port}/",
                            on_message=on_message, heartbeat_interval=1, heartbeat_timeout=1,
                            reconnect_initial=0.2, reconnect_max=0.5, max_reconnects=1)
    task = asyncio.create_task(client.run())
    deadline = time.time() + 15
    while time.time() < deadline and len(got) < 3:
        await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await server.stop()
    await api.aclose()

    texts = [m.text for m in got]
    check("★ 同包多条消息全部交付（外层 mid 共用不再把它们误判成重复）",
          texts == ["在吗", "能便宜点吗", "发货快吗"],
          f"交付={texts} duplicates={client.stats['duplicates']} dropped={client.stats['dropped']}")
    check("★ 同包多条消息的幂等键互不相同（内容键兜底）",
          len({m.message_id for m in got}) == len(got),
          f"ids={[m.message_id for m in got]}")
    check("幂等键来源可见（排查「被判成重复丢掉」靠它）",
          all(m.id_source for m in got), f"id_source={[m.id_source for m in got]}")


async def scenario_sync_cursor_resume():
    r"""★ 断线重连要从上次同步进度续（否则掉线窗口内买家发的消息永远补不回来）。

    `pts=now` 等于告诉平台「我已同步到现在」，掉线期间的消息就永久丢了——本机实测
    通道上线前那 4 条买家消息就是这样一条都没进链路。这里验证：平台在同步包里给的
    `maxPts` 会落盘，且**重连时的 ackDiff 带的是它**，而不是又一个 now。
    """
    import tempfile

    from app.channels.xianyu.ws import SyncCursor

    cursor_path = Path(tempfile.mkdtemp()) / "pts.json"
    cursor = SyncCursor(cursor_path)
    platform_pts = (int(time.time() * 1000) - 60000) * 1000   # 一分钟前的进度（微秒）

    # 同步包里带上平台的进度
    package = chat_frame("m-cursor", "11112222", "在吗")
    package["body"]["syncPushPackage"]["maxPts"] = platform_pts

    server = await FakeGoofish(ack_heartbeat=True, push_frames=False,
                               custom_frames=[package], close_after=1.0).start()
    api = await make_api(server.port)
    client = XianyuWSClient(api, DEVICE_ID, url=f"ws://127.0.0.1:{server.port}/",
                            heartbeat_interval=1, heartbeat_timeout=1,
                            reconnect_initial=0.2, reconnect_max=0.5, max_reconnects=3,
                            sync_pts_mode="last", sync_cursor=cursor)
    task = asyncio.create_task(client.run())
    # 等**第二条 ackDiff**（第一次注册用的是空游标=now，重连那次才该带平台给的进度）。
    # 注意别用 reg_count>=2 就收工：ackDiff 是在 /reg 之后 1 秒才发的，提前 cancel 会让它永远发不出来。
    deadline = time.time() + 25
    while time.time() < deadline and len(server.ackdiff_frames) < 2:
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await server.stop()
    await api.aclose()

    check("★ 平台给的同步进度会落盘（重连才有得续）", cursor.get() == platform_pts,
          f"cursor={cursor.get()} platform={platform_pts}")
    check("★ 游标已持久化到磁盘（进程重启也不丢）",
          cursor_path.exists() and json.loads(cursor_path.read_text(encoding="utf-8"))["pts"] == platform_pts,
          f"path={cursor_path.name}")
    pts_sent = [f["body"][0]["pts"] for f in server.ackdiff_frames]
    check("★ 重连的 ackDiff 带的是上次进度（不再是无脑 now，掉线窗口的消息才会被补推）",
          len(pts_sent) >= 2 and pts_sent[0] > 0 and pts_sent[-1] == platform_pts,
          f"各次 pts={pts_sent}（末次应等于 {platform_pts}）")
    class _Api:
        unb = "1000000000001"
        user_agent = "fake-ua"

    check("now 模式仍然是 now（默认行为没被改坏）",
          XianyuWSClient(_Api(), DEVICE_ID, sync_pts_mode="now").sync_pts_mode == "now",
          "mode=now")


def to_int_keys(obj, depth=0):
    """把外层消息表的数字键转成 int —— **真机 msgpack 编码出来就是这个样子**。

    这条是本项目最隐蔽的坑：JSON 夹具里键是字符串，真机是整数，于是
    `frame["1"]` / `parent.get("2")` 在线上全部取不到值，而离线自检全绿。
    """
    if isinstance(obj, dict) and depth <= 2:
        out = {}
        for key, value in obj.items():
            new_key = int(key) if isinstance(key, str) and key.isdigit() else key
            out[new_key] = value if depth >= 2 else to_int_keys(value, depth + 1)
        return out
    if isinstance(obj, list) and depth <= 2:
        return [to_int_keys(v, depth + 1) for v in obj]
    return obj


def scenario_int_keys_real_wire_shape():
    r"""★ 真机键类型回归：外层消息表的键是**整数**（msgpack 形态），不是字符串。

    旧实现在真机上静默出错的三个后果，全部由这个形状引起：
      1. `"10" in frame["1"]` → False → 买家消息被当 `not_chat` 丢弃；
      2. `parent.get("2")`（会话 id）→ None → **chat_id 变成空字符串**，所有买家被并成一个会话；
      3. `parent.get("3")`/`get("5")` → None → 幂等键退化成外层 mid、消息过期判断失效。
    """
    payload_str = {
        "1": {"1": {"1": "1000000000003@goofish"}, "2": "10000000002@goofish",
              "3": "4318231602515.PNM", "4": 0, "5": int(time.time() * 1000),
              "6": {"1": 101, "3": {"1": "", "2": "你好", "3": "", "4": 1, "5": "{}"}},
              "10": {"reminderContent": "你好", "senderUserId": "1000000000003",
                     "reminderTitle": "x***1", "detailNotice": "你好",
                     "reminderUrl": "fleamarket://message_chat?itemId=1012341728505&peerUserId=1000000000003"},
              "12": 1},
        "3": {"needPush": "true"},
    }
    payload_int = to_int_keys(payload_str)

    results = {}
    for label, payload in (("字符串键", payload_str), ("整数键（真机）", payload_int)):
        reason, msg = parse_inbound(payload, "1000000000001", message_id="m-int", position=0)
        results[label] = (reason, msg)

    for label, (reason, msg) in results.items():
        check(f"★ {label}：解析出买家消息且会话 id 正确",
              reason == "ok" and msg is not None and msg.chat_id == "10000000002",
              f"reason={reason} chat_id={msg.chat_id if msg else None!r}")
        check(f"★ {label}：消息 id / 正文 / 商品号 / 发送者都对",
              msg is not None and msg.message_id == "4318231602515.PNM" and msg.text == "你好"
              and msg.item_id == "1012341728505" and msg.sender_id == "1000000000003",
              f"id={msg.message_id if msg else None!r} text={msg.text if msg else None!r}")
        check(f"★ {label}：幂等键来源为 payload_id（不再退化成外层 mid）",
              msg is not None and msg.id_source == "payload_id" and msg.content_type == 1,
              f"id_source={msg.id_source if msg else None} content_type={msg.content_type if msg else None}")

    reason_str, msg_str = results["字符串键"]
    reason_int, msg_int = results["整数键（真机）"]
    check("★ 两种键类型解析结果一致（离线夹具与真机不再分叉）",
          msg_str is not None and msg_int is not None
          and (msg_str.chat_id, msg_str.text, msg_str.message_id, msg_str.item_id)
          == (msg_int.chat_id, msg_int.text, msg_int.message_id, msg_int.item_id),
          f"str=({msg_str.chat_id},{msg_str.text}) int=({msg_int.chat_id},{msg_int.text})")

    # 会话列表 / 订单状态 / 系统消息判定也必须键类型无关
    conv_int = to_int_keys({"1": [{"1": "10000000002@goofish", "2": 1, "3": 1,
                                   "4": "1000000000001@goofish"}]})
    check("★ 整数键的会话列表推送仍判为 conv_list",
          parse_inbound(conv_int, "1000000000001", message_id="m-cl")[0] == "conv_list",
          parse_inbound(conv_int, "1000000000001", message_id="m-cl")[0])
    order_int = to_int_keys({"1": "buyer@goofish", "3": {"redReminder": "等待买家付款"}})
    check("★ 整数键的订单状态帧仍能识别出状态",
          order_status(order_int) == "等待买家付款", str(order_status(order_int)))


async def scenario_slow_handler_does_not_block_frames():
    r"""★ 真机踩过的坑：业务处理慢（LLM/商品接口/拟人化延迟）**不能**堵住收帧循环。

    以前的写法是收帧循环里 `await self.on_message(msg)` 内联等待，于是：
    处理一条消息要十几秒 → 心跳 ACK 收不到 → 客户端判自己掉线并关连接 →
    **正好把正在进行的发送打断**（2026-09-20 真机：买家问「考研资料」，草稿生成了却没发出去）。
    现在消息走队列、由独立消费者处理，收帧循环立刻回去收下一帧。
    """
    now = int(time.time() * 1000)
    package = multi_chat_frame("slow-mid", [("11112222", "这条会慢慢处理", now)])
    server = await FakeGoofish(ack_heartbeat=True, push_frames=False,
                               custom_frames=[package]).start()
    api = await make_api(server.port)
    handled = []

    async def slow_on_message(msg):
        await asyncio.sleep(6)          # 模拟 LLM + 拟人化延迟（> 心跳判定阈值）
        handled.append(msg)

    client = XianyuWSClient(api, DEVICE_ID, url=f"ws://127.0.0.1:{server.port}/",
                            on_message=slow_on_message,
                            heartbeat_interval=1, heartbeat_timeout=1,
                            reconnect_initial=0.2, reconnect_max=0.5, max_reconnects=1)
    task = asyncio.create_task(client.run())
    # 处理函数还在睡的时候观察心跳：这期间 ACK 必须照常被处理
    await asyncio.sleep(4)
    acked_midway = client.stats["heartbeats_acked"]
    timeouts_midway = client.stats["heartbeat_timeouts"]
    connected_midway = client.connected
    deadline = time.time() + 15
    while time.time() < deadline and not handled:
        await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await server.stop()
    await api.aclose()

    check("★ 处理慢消息期间心跳照常被应答（收帧循环没被业务堵住）",
          acked_midway >= 2 and timeouts_midway == 0,
          f"处理中已应答={acked_midway} 判定超时={timeouts_midway}")
    check("★ 处理慢消息期间连接不被自己关掉（不再误判掉线）", connected_midway,
          f"connected={connected_midway}")
    check("慢消息最终仍然被处理（只是没堵住协议层）",
          len(handled) == 1 and handled[0].text == "这条会慢慢处理",
          f"处理条数={len(handled)}")
    check("交付队列指标可见（面板/长跑体检用）",
          "deliver_queue" in client.health() and client.stats.get("deliver_dropped", 0) == 0,
          f"queue={client.health().get('deliver_queue')} dropped={client.stats.get('deliver_dropped')}")


async def main():
    await scenario_normal()
    await scenario_heartbeat_timeout()
    scenario_send_frame()
    scenario_token_refresh_cadence()
    scenario_frame_dump()
    scenario_real_payload_regression()
    scenario_history_and_auth()
    scenario_full_message_shape()
    scenario_int_keys_real_wire_shape()
    await scenario_request_error_surface()
    await scenario_session_invalid_recovery()
    await scenario_multi_message_package()
    await scenario_sync_cursor_resume()
    await scenario_slow_handler_does_not_block_frames()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"闲鱼 WSS 客户端离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_ws_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n闲鱼 WSS 客户端离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
