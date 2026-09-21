# -*- coding: utf-8 -*-
r"""闲鱼值守引擎离线自检：本地真 WebSocket 假服务器 + 假生成器（不连闲鱼、不调 LLM、不发真实消息）。

覆盖：影子模式（只落草稿不发送）、商品缓存命中、人工接管与超时、议价计数进上下文、
生成失败隔离、no_reply 跳过、合规过滤、live 模式发送帧、健康快照。

用法：python scripts/test_xianyu_engine.py
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

from app.channels.xianyu.api import MTOP_ITEM_URL, MTOP_TOKEN_URL, XianyuApi  # noqa: E402
from app.channels.xianyu.engine import XianyuWatchEngine, build_item_description, filter_outbound  # noqa: E402
from app.channels.xianyu.protocol import parse_cookie_str  # noqa: E402
from app.channels.xianyu.session import ChannelSessionStore  # noqa: E402

RESULTS = []
REAL_CRED = ROOT / "secrets" / "xianyu_credentials.json"
DEVICE_ID = "C882C442-D8C9-4A69-A9B0-A59EFDD765C8-1000000000001"
SELLER_UNB = "1000000000001"     # 与 DEVICE_ID/夹具里的卖家 id 一致（离线自检不依赖真实账号）
BUYER_ID = "11112222"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


# ---------------- MessagePack 打包（与其它自测同一份逻辑） ----------------
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


def chat_frame(mid, sender_id, text, item_id="888888", chat_id="C1"):
    payload = {"1": {"2": f"{chat_id}@goofish", "5": int(time.time() * 1000),
                     "10": {"senderUserId": sender_id, "reminderTitle": "买家A",
                            "reminderContent": text,
                            "reminderUrl": f"https://www.goofish.com/item?itemId={item_id}&x=1"}}}
    return {"headers": {"mid": mid, "sid": "s1"},
            "body": {"syncPushPackage": {"data": [{"data": base64.b64encode(pack(payload)).decode()}]}}}


class PushServer:
    """按需推送的假 goofish 服务端（不自动推脚本帧）。"""

    def __init__(self):
        self.connections = []
        self.received = []
        self.reg_count = 0
        self._server = None
        self.port = None

    async def start(self):
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws):
        self.connections.append(ws)
        try:
            async for raw in ws:
                frame = json.loads(raw)
                self.received.append(frame)
                if frame.get("lwp") == "/reg":
                    self.reg_count += 1
                elif frame.get("lwp") == "/!":
                    mid = (frame.get("headers") or {}).get("mid")
                    if mid:
                        await ws.send(json.dumps({"code": 200, "headers": {"mid": mid}}))
        except Exception:
            pass

    async def push(self, frame):
        for _ in range(50):
            if self.connections:
                break
            await asyncio.sleep(0.1)
        await self.connections[-1].send(json.dumps(frame))

    def sent_messages(self):
        return [f for f in self.received if f.get("lwp") == "/r/MessageSend/sendByReceiverScope"]


def make_api(port, item_calls):
    cookies = json.loads(REAL_CRED.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(MTOP_TOKEN_URL):
            return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "T"}})
        if url.startswith(MTOP_ITEM_URL):
            item_calls.append(url)
            return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {
                "title": "罗技 G304 无线鼠标", "desc": "9 成新，无盒", "soldPrice": 89,
                "quantity": 1, "skuList": [{"price": 8900, "quantity": 1,
                                            "propertyList": [{"valueText": "黑色"}]}]}}})
        return httpx.Response(200, json={"content": {"success": True}})

    api = XianyuApi(parse_cookie_str(cookies["cookies_str"]),
                    transport=httpx.MockTransport(handler), credential_path=None)
    # 离线自检不该依赖真实账号：unb 固定成与夹具一致的假值。
    # （脱敏后夹具里的卖家 id 是假值，若 api.unb 仍取自真实凭据，"卖家发言"判定就会失效，
    #   接管词场景随之失败——本机踩过一次）
    api.unb = SELLER_UNB
    return api


async def wait_for(cond, timeout=15.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    return False


class Recorder:
    """假生成器：按内容脚本化返回，同时记录调用参数。"""

    def __init__(self):
        self.calls = []

    async def __call__(self, message, session_id, context, item_desc):
        self.calls.append({"message": message, "session_id": session_id,
                           "context": context, "item_desc": item_desc})
        if "报错" in message:
            raise RuntimeError("模拟生成失败")
        if "不用回" in message:
            return {"reply": "-", "intent": None}
        if "便宜点" in message:
            return {"reply": "可以少 10 元，运费到付～", "intent": "price", "sources": [], "engine": "test"}
        if "微信" in message:
            return {"reply": "加我微信详聊", "intent": "chat", "sources": [], "engine": "test"}
        return {"reply": f"收到：{message}", "intent": "knowledge",
                "sources": ["kb.md#1"], "engine": "test"}


def fresh_session(name):
    path = ROOT / ".logs" / f"ktest_{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return ChannelSessionStore(path)


async def make_engine(server, session, rec, *, shadow=True, manual_timeout=3600, item_calls=None):
    api = make_api(server.port, item_calls if item_calls is not None else [])
    engine = XianyuWatchEngine(api, session, DEVICE_ID, url=f"ws://127.0.0.1:{server.port}/",
                               shadow_mode=shadow, reply_generator=rec,
                               manual_timeout=manual_timeout,
                               ws_kwargs={"heartbeat_interval": 5, "heartbeat_timeout": 5,
                                          "reconnect_initial": 0.2, "max_reconnects": 50})
    await engine.start()
    assert await wait_for(lambda: engine.ws.registered), "通道未注册成功"
    return engine, api


async def scenario_shadow(server, item_calls):
    session = fresh_session("shadow")
    rec = Recorder()
    engine, api = await make_engine(server, session, rec, shadow=True, item_calls=item_calls)

    await server.push(chat_frame("e-1", BUYER_ID, "这个还在吗？"))
    assert await wait_for(lambda: session.message_count("C1") == 1 and len(session.list_drafts()) == 1), "草稿未生成"

    drafts = session.list_drafts()
    d = drafts[0]
    check("影子模式：买家消息入库 + 生成草稿（mode=shadow、带 intent/sources）",
          session.message_count("C1") == 1 and d["reply"] == "收到：这个还在吗？"
          and d["intent"] == "knowledge" and d["mode"] == "shadow" and d["sent"] == 0,
          f"草稿 id={d['id']} intent={d['intent']} mode={d['mode']}")
    check("影子模式：**一条消息都没发出去**",
          len(server.sent_messages()) == 0 and engine.counters["sent"] == 0,
          f"服务端收到的发送帧={len(server.sent_messages())}")
    check("商品描述已注入生成器（标题+SKU+价格区间）",
          rec.calls and "罗技 G304 无线鼠标" in rec.calls[0]["item_desc"]
          and "黑色" in rec.calls[0]["item_desc"] and "¥89" in rec.calls[0]["item_desc"],
          rec.calls[0]["item_desc"][:80] if rec.calls else "-")
    check("会话 id 按会话隔离（xianyu:<chat_id>）",
          rec.calls[0]["session_id"] == "xianyu:C1", rec.calls[0]["session_id"])

    # 商品缓存：第二条消息不应再打接口
    before = len(item_calls)
    await server.push(chat_frame("e-2", BUYER_ID, "能便宜点吗？"))
    assert await wait_for(lambda: len(session.list_drafts()) == 2), "第二条草稿未生成"
    check("商品信息走缓存：第二条消息不再调接口",
          len(item_calls) == before and engine.counters["item_cache_hits"] >= 1,
          f"接口调用 {len(item_calls)} 次（首条 1 次），缓存命中 {engine.counters['item_cache_hits']} 次")

    # 议价：intent=price → 计数 +1，并把「议价次数」带进下一次上下文
    check("议价计数落库（intent=price → bargain_count+1）",
          session.get_bargain_count("C1") == 1, f"count={session.get_bargain_count('C1')}")
    await server.push(chat_frame("e-3", BUYER_ID, "再便宜 5 块行吗"))
    assert await wait_for(lambda: len(session.list_drafts()) == 3), "第三条草稿未生成"
    last_ctx = rec.calls[-1]["context"]
    check("议价次数以 system 消息进入下一次上下文（对齐上游）",
          any(m["role"] == "system" and "议价次数" in m["content"] for m in last_ctx),
          str([m for m in last_ctx if m["role"] == "system"]))

    # 合规过滤
    await server.push(chat_frame("e-4", BUYER_ID, "加个微信方便吗"))
    await wait_for(lambda: len(session.list_drafts()) == 4)
    d4 = session.list_drafts(limit=1)[0]
    check("合规过滤：命中站外引流词整条替换（对齐上游 _safe_filter）",
          d4["reply"] == "[安全提醒]请通过平台沟通" and filter_outbound("加我微信") == "[安全提醒]请通过平台沟通",
          d4["reply"])

    # 生成失败隔离
    await server.push(chat_frame("e-5", BUYER_ID, "这条会报错"))
    await wait_for(lambda: engine.counters["reply_errors"] == 1)
    check("生成失败被隔离：记事件、不发送、连接不受影响",
          engine.counters["reply_errors"] == 1 and engine.ws.connected
          and any(e["kind"] == "reply_error" for e in session.recent_events(20)),
          f"reply_errors={engine.counters['reply_errors']} ws.connected={engine.ws.connected}")

    # no_reply
    await server.push(chat_frame("e-6", BUYER_ID, "这条不用回"))
    await wait_for(lambda: any(e["kind"] == "no_reply" for e in session.recent_events(20)))
    check("生成器返回 '-' 时不落草稿（对齐上游 no_reply 语义）",
          len(session.list_drafts()) == 4, f"草稿数={len(session.list_drafts())}")

    health = engine.health()
    check("健康快照可直接喂面板（ws + session + counters + 接管列表）",
          health["ws"]["registered"] is True and health["session"]["drafts"] >= 4
          and health["shadow_mode"] is True and "counters" in health,
          json.dumps({"msgs": health["session"]["messages"], "drafts": health["session"]["drafts"],
                      "counters.drafts": health["counters"]["drafts"]}, ensure_ascii=False))

    await engine.stop()
    await api.aclose()


async def scenario_manual(server, item_calls):
    session = fresh_session("manual")
    rec = Recorder()
    engine, api = await make_engine(server, session, rec, shadow=True, manual_timeout=2,
                                    item_calls=item_calls)

    await server.push(chat_frame("m-1", "1000000000001", "。"))  # 卖家发接管词
    assert await wait_for(lambda: session.is_manual_mode("C1")), "未进入人工接管"
    check("卖家发接管词 → 进入人工接管（落库，重启不丢）",
          session.is_manual_mode("C1") and engine.counters["manual_toggles"] == 1,
          f"接管会话数={len(session.list_manual_chats())}")

    before = len(session.list_drafts())
    await server.push(chat_frame("m-2", BUYER_ID, "在吗"))
    assert await wait_for(lambda: engine.counters["manual_skipped"] == 1), "接管期间应跳过回复"
    check("接管期间买家消息只入库、不生成草稿",
          session.message_count("C1") == 1 and len(session.list_drafts()) == before
          and engine.counters["manual_skipped"] == 1,
          f"入库 {session.message_count('C1')} 条（含买家「在吗」），草稿数 {before} → {len(session.list_drafts())}，manual_skipped={engine.counters['manual_skipped']}")

    await server.push(chat_frame("m-3", "1000000000001", "我来看下库存"))
    await wait_for(lambda: any(e["kind"] == "seller_manual_reply" for e in session.recent_events(20)))
    roles = [m["role"] for m in session.get_context("C1")]
    check("卖家人工回复被记为 assistant 存进历史（供后续上下文）",
          roles.count("assistant") >= 1, f"roles={roles}")

    # 接管超时自动恢复
    await asyncio.sleep(2.2)
    check("人工接管超时后自动恢复自动回复（对齐上游 MANUAL_MODE_TIMEOUT）",
          session.is_manual_mode("C1") is False, "超时 2s 后 is_manual_mode=False")

    await server.push(chat_frame("m-4", BUYER_ID, "还在吗"))
    await wait_for(lambda: len(session.list_drafts()) > before)
    check("恢复自动回复后重新生成草稿", len(session.list_drafts()) > before,
          f"草稿数={len(session.list_drafts())}")

    await engine.stop()
    await api.aclose()


async def scenario_live(server, item_calls):
    """live 模式（P4 预演）：验证发送帧真的发出去、草稿标记已发送。"""
    session = fresh_session("live")
    rec = Recorder()
    engine, api = await make_engine(server, session, rec, shadow=False, item_calls=item_calls)

    await server.push(chat_frame("l-1", BUYER_ID, "发货快吗"))
    assert await wait_for(lambda: len(server.sent_messages()) == 1), "未发送"
    frame = server.sent_messages()[0]
    body = frame["body"]
    payload = json.loads(base64.b64decode(body[0]["content"]["custom"]["data"]).decode())
    drafts = session.list_drafts()
    check("live 模式：发送帧结构与上游一致（cid/actualReceivers/contentType101）",
          body[0]["cid"] == "C1@goofish" and body[1]["actualReceivers"][0] == f"{BUYER_ID}@goofish"
          and payload["text"]["text"] == "收到：发货快吗",
          f"cid={body[0]['cid']} text={payload['text']['text']}")
    check("live 模式：草稿被标记已发送，assistant 回复入历史",
          drafts and drafts[0]["sent"] == 1 and engine.counters["sent"] == 1
          and any(m["role"] == "assistant" for m in session.get_context("C1")),
          f"sent={drafts[0]['sent'] if drafts else '-'} counter={engine.counters['sent']}")

    await engine.stop()
    await api.aclose()


def scenario_item_desc():
    desc = build_item_description({"title": "T", "desc": "D", "soldPrice": 100, "quantity": 3,
                                   "skuList": [{"price": 9900, "quantity": 1,
                                                "propertyList": [{"valueText": "红"}, {"valueText": "大"}]},
                                               {"price": 12900, "quantity": 2, "propertyList": []}]})
    parsed = json.loads(desc)
    check("商品描述构造与上游一致（分→元、价格区间、SKU 规格拼接）",
          parsed["price_range"] == "¥99.0 - ¥129.0" and parsed["sku_details"][0]["spec"] == "红 大"
          and parsed["sku_details"][1]["spec"] == "默认规格" and parsed["total_stock"] == 3,
          f"price_range={parsed['price_range']} skus={parsed['sku_details']}")


async def scenario_item_api_cooldown(server):
    """★ 商品接口失败后必须冷却，不能每条消息都再打一次平台接口。

    真机实测：同一条商品被平台以 RGV587（"被挤爆啦"）拒绝后，下一条买家消息照样再打一次——
    等于自己把风控信号刷满。7×24 值守最怕的就是账号被标记。
    """
    from app.channels.xianyu.api import XianyuRiskControlError

    session = fresh_session("item_cooldown")
    rec = Recorder()
    engine, api = await make_engine(server, session, rec, shadow=True)

    calls = {"n": 0}

    async def deny(item_id):
        calls["n"] += 1
        raise XianyuRiskControlError("RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试")

    api.get_item_info = deny
    engine.item_cooldown_risk = 600.0   # 冷却 10 分钟

    for i in range(3):
        await server.push(chat_frame(f"cd-{i}", BUYER_ID, f"第{i}条消息"))
        assert await wait_for(lambda i=i: session.message_count("C1") == i + 1), "消息未入库"
    await wait_for(lambda: len(session.list_drafts()) >= 3, timeout=15)
    await asyncio.sleep(0.5)

    check("★ 商品接口被风控拒绝后进入冷却：连续 3 条消息只打了 1 次平台接口",
          calls["n"] == 1 and engine.counters["item_api_cooldown_skips"] >= 2,
          f"接口调用={calls['n']} 冷却跳过={engine.counters['item_api_cooldown_skips']}")
    check("★ 冷却不影响生成回复（买家照样有草稿，只是没商品详情）",
          len(session.list_drafts()) >= 3,
          f"草稿数={len(session.list_drafts())}")
    check("冷却原因写进事件流水（可观测）",
          any(e["kind"] == "item_api_skipped_cooldown"
              for e in session.recent_events(30)),
          str([e["kind"] for e in session.recent_events(30) if "item" in e["kind"]][:4]))
    await engine.stop()


async def scenario_message_idempotency(server):
    r"""★ 真发送下的硬要求：同一条消息（同一 message_id）只能答一次。

    内存去重挡不住进程重启，而平台在重连/补推时会重放同一条消息——真发送下那等于给买家回两条。
    本机在库里查到过 5 组重复 message_id 的草稿（其中一组一条已发送），所以这条必须有回归。
    """
    from app.channels.xianyu.ws import InboundMessage

    session = fresh_session("msg_idem")
    rec = Recorder()
    engine, _ = await make_engine(server, session, rec, shadow=False)

    def make_msg(mid, text, created):
        return InboundMessage(message_id=mid, chat_id="C1", sender_id=BUYER_ID,
                              sender_name="买家A", text=text, item_id=None,
                              create_time_ms=created)

    base = int(time.time() * 1000)
    first = await engine.handle_message(make_msg("dup-1", "这个还在吗", base), allow_item_api=False)
    second = await engine.handle_message(make_msg("dup-1", "这个还在吗", base), allow_item_api=False)
    await asyncio.sleep(0.3)

    check("★ 同一条消息第二次到达直接跳过（不重复生成、不重复发送）",
          second.get("skipped") is True and second.get("reason") == "duplicate_message"
          and first.get("skipped") is not True,
          f"first={json.dumps(first, ensure_ascii=False)[:60]} second={second.get('reason')}")
    check("★ 只落了一条草稿、只发了一条（买家不会收到两条）",
          len(session.list_drafts()) == 1 and engine.counters["sent"] == 1,
          f"草稿={len(session.list_drafts())} 已发送={engine.counters['sent']}")
    check("跳过会留事件（可观测，不是静默丢弃）",
          any(e["kind"] == "duplicate_message_skipped" for e in session.recent_events(20)),
          str([e["kind"] for e in session.recent_events(20) if "duplicate" in e["kind"]]))

    # 幂等键落库 → **进程重启后依然有效**（内存去重做不到这一点）
    from app.channels.xianyu.session import ChannelSessionStore
    reopened = ChannelSessionStore(session.db_path, max_history=100)
    check("★ 幂等记录已落 SQLite（重启后同一条消息仍不会被重复回答）",
          reopened.seen("msg:dup-1"),
          f"seen(msg:dup-1)={reopened.seen('msg:dup-1')}")

    # 不同消息不能被误伤
    third = await engine.handle_message(make_msg("dup-2", "能便宜点吗", base + 1000), allow_item_api=False)
    await asyncio.sleep(0.3)
    check("下一条不同消息照常处理（幂等不误伤后续消息）",
          third.get("skipped") is not True and len(session.list_drafts()) == 2,
          f"草稿={len(session.list_drafts())}")
    await engine.stop()


async def scenario_send_failure_visible(server):
    r"""★ 发送失败必须留明确痕迹，而且不能被自己的去重挡住重发。

    真机踩过：发送时连接正好断开 → 异常冒到收帧循环被记成 `frame_error`（误导方向），
    草稿留在未发送却没写原因；更糟的是出站记账按"已发送"算，300 秒重复文本窗口
    会把重发的同一条回复拦掉 —— 变成"发失败 → 重发被自己挡住 → 买家永远收不到"。
    """
    from app.channels.xianyu.ws import InboundMessage

    session = fresh_session("send_fail")
    rec = Recorder()
    engine, _ = await make_engine(server, session, rec, shadow=False)

    async def boom(ws, chat_id, to_id, text):
        raise ConnectionResetError("模拟发送途中连接断开")

    engine.ws.send_text = boom
    msg = InboundMessage(message_id="sf-1", chat_id="C1", sender_id=BUYER_ID, sender_name="买家A",
                         text="发货快吗", item_id=None, create_time_ms=int(time.time() * 1000))
    result = await engine.handle_message(msg, allow_item_api=False)
    await asyncio.sleep(0.3)

    drafts = session.list_drafts()
    check("★ 发送失败时草稿保留且标记未发送（不假装发成功）",
          len(drafts) == 1 and str(drafts[0]["sent"]) == "0",
          f"草稿={len(drafts)} sent={drafts[0]['sent'] if drafts else '-'} result={str(result)[:60]}")
    events = {e["kind"] for e in session.recent_events(30)}
    check("★ 留有明确的 send_failed 事件（不再混成 frame_error 误导排查）",
          "send_failed" in events and engine.counters.get("send_errors") == 1,
          f"send_errors={engine.counters.get('send_errors')} 相关事件={sorted(k for k in events if 'send' in k)}")
    detail = [e for e in session.recent_events(30) if e["kind"] == "send_failed"][0]
    check("send_failed 事件带错误原因与草稿 id",
          "draft_id" in str(detail["payload"]) and "ConnectionResetError" in str(detail["payload"]),
          str(detail["payload"])[:140])
    check("★ 出站记账已回滚（重发同一条不会被自己的去重窗口挡住）",
          engine.outbound.stats.get("rolled_back") == 1
          and engine.outbound.plan("C1", drafts[0]["reply"]).reason != "duplicate_text",
          f"rolled_back={engine.outbound.stats.get('rolled_back')} "
          f"重发判定={engine.outbound.plan('C1', drafts[0]['reply']).reason}")

    # 连接恢复后重发同一条应能成功
    async def ok_send(ws, chat_id, to_id, text):
        return {"headers": {"mid": "fake"}}

    engine.ws.send_text = ok_send
    session.mark_draft_sent  # noqa: B018  (保持接口可见性，实际重发由新一轮处理触发)
    retry = InboundMessage(message_id="sf-2", chat_id="C1", sender_id=BUYER_ID, sender_name="买家A",
                           text="发货快吗？", item_id=None, create_time_ms=int(time.time() * 1000))
    await engine.handle_message(retry, allow_item_api=False)
    await asyncio.sleep(0.3)
    check("连接恢复后后续消息正常发送（不影响值守）",
          engine.counters["sent"] >= 1,
          f"sent={engine.counters['sent']}")
    await engine.stop()


async def main():
    server = await PushServer().start()
    item_calls = []
    await scenario_shadow(server, item_calls)
    await scenario_manual(server, [])
    await scenario_live(server, [])
    await scenario_item_api_cooldown(server)
    await scenario_message_idempotency(server)
    await scenario_send_failure_visible(server)
    await server.stop()
    scenario_item_desc()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"闲鱼值守引擎离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_engine_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n闲鱼值守引擎离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
