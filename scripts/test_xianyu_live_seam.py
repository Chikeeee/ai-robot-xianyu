# -*- coding: utf-8 -*-
r"""闲鱼值守**实时链路端到端自检**：假平台 socket → 交付队列 → 引擎 → 真的写出站帧。

为什么需要它：其它套件各测一段——
- `test_xianyu_ws.py` 测协议层（解析/心跳/重连），但用假回调，不碰引擎；
- `test_xianyu_engine.py` 直接调 `handle_message`，**绕过了收帧循环与交付队列**。

而真机上出问题的恰恰是这两段之间的接缝。2026-09-20 真机事故：买家问「考研资料」，
草稿生成了却没发出去——因为收帧循环内联 `await on_message()` 被业务堵住，
心跳 ACK 收不到 → 客户端判自己掉线并关连接 → 正好打断发送。
本套件把整条链路串起来跑，并显式断言"处理慢消息时协议层不被堵住"。

**不连闲鱼、不发真实消息**：假服务器只记录收到的出站帧。回复生成器换成确定性假实现
（离线，生成器本身已在专家层套件与真实 replay 里验过）。

用法：python scripts/test_xianyu_live_seam.py
"""
import argparse
import asyncio
import base64
import json
import os
import struct
import sys
import time
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def pack(obj):
    """最小 MessagePack 打包（与其它套件同一份实现）。"""
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


BUYER = "1000000000003"
CID = "10000000002"
ITEM = "1012341728505"


def chat_frame(mid, text, *, created=None, sender=BUYER, cid=CID, item=ITEM):
    """真机形状的入站消息帧（外层消息表用 int 键——真机就是这样，JSON 夹具别再用字符串键）。"""
    payload = {
        1: {
            1: {1: f"{sender}@goofish"},
            2: f"{cid}@goofish",
            3: f"{int(time.time() * 1000)}.PNM",
            4: 0,
            5: created or int(time.time() * 1000),
            6: {1: 101, 3: {1: "", 2: text, 3: "", 4: 1, 5: "{}"}},
            10: {"reminderContent": text, "detailNotice": text, "senderUserId": sender,
                 "reminderTitle": "x***1", "sessionType": "1",
                 "reminderUrl": f"fleamarket://message_chat?itemId={item}&peerUserId={sender}"},
            12: 1,
        },
        3: {"needPush": "true"},
    }
    return {"headers": {"mid": mid, "sid": "sid-seam"},
            "body": {"syncPushPackage": {"data": [
                {"data": base64.b64encode(pack(payload)).decode()}]}}}


class FakePlatform:
    """假闲鱼平台：应答心跳、接收推送指令、**记录客户端发来的出站帧**。"""

    def __init__(self):
        self.regs = 0
        self.heartbeats = 0
        self.outbound = []          # 收到的发送帧
        self.acks = 0
        self._ws = None
        self._server = None
        self.port = None

    async def start(self):
        async def handler(ws):
            self._ws = ws
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    lwp = frame.get("lwp")
                    if lwp == "/reg":
                        self.regs += 1
                    elif lwp == "/!":
                        self.heartbeats += 1
                        mid = (frame.get("headers") or {}).get("mid")
                        if mid:
                            await ws.send(json.dumps({"code": 200, "headers": {"mid": mid}}))
                            self.acks += 1
                    elif lwp == "/r/MessageSend/sendByReceiverScope":
                        self.outbound.append(frame)
                    elif frame.get("code") == 200:
                        self.acks += 0
            except Exception:
                pass

        self._server = await serve(handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def push(self, frame):
        await self._ws.send(json.dumps(frame))

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()


def decode_outbound(frame):
    """解出站帧里的正文（与协议层 build_text_frame 的形状对应）。"""
    body = (frame or {}).get("body") or []
    if not body:
        return None, None, []
    head = body[0] if isinstance(body[0], dict) else {}
    receivers = (body[1] or {}).get("actualReceivers") if len(body) > 1 else []
    data = ((head.get("content") or {}).get("custom") or {}).get("data")
    text = None
    if data:
        try:
            text = (json.loads(base64.b64decode(data).decode("utf-8")).get("text") or {}).get("text")
        except Exception:
            text = None
    return head.get("cid"), text, receivers or []


def offline_api_factory(cookies, user_agent, credential_path):
    """离线 api：所有 mtop 请求走 MockTransport（自检绝不能打真实平台接口）。"""
    import httpx

    from app.channels.xianyu.api import XianyuApi

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "login.token" in url:
            return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"],
                                             "data": {"accessToken": "FAKE-SEAM-TOKEN"}})
        return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {
            "itemDO": {"title": "自检用商品", "desc": "自检用描述", "soldPrice": "9900",
                       "originalPrice": "12900", "itemId": ITEM}}})

    return XianyuApi(cookies, credential_path=None, user_agent=user_agent,
                     transport=httpx.MockTransport(handler))


def install_fake_generator(delay: float, calls: list):
    """确定性假生成器（可指定耗时，用来验证"慢处理不堵协议层"）。"""
    from app.channels.xianyu import reply

    async def fake_generator(text, chat_id, history, item_desc):
        if delay:
            await asyncio.sleep(delay)
        calls.append({"text": text, "chat_id": chat_id})
        return {"reply": f"[seam] 收到「{text}」", "intent": "default",
                "sources": ["seam-test#0"], "engine": "seam-fake"}

    reply.GENERATORS["__seam_test__"] = fake_generator


async def build_service(server, *, shadow, handler_delay, db_name):
    from app.channels.xianyu import config as cfg
    from app.channels.xianyu.service import XianyuChannelService

    db = ROOT / ".logs" / f"ktest_{db_name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    settings = cfg.XianyuSettings()
    settings.enabled = True
    settings.shadow_mode = shadow
    settings.reply_engine = "__seam_test__"
    settings.ws_url = f"ws://127.0.0.1:{server.port}/"
    settings.heartbeat_interval = 1
    settings.heartbeat_timeout = 1
    settings.db_path = db
    settings.device_store = ROOT / ".logs" / f"ktest_{db_name}_device.json"
    settings.frame_dump = ""              # 绝不污染真实诊断文件
    settings.typing_simulation = False    # 拟人化延迟对测试无意义
    settings.min_interval_per_chat = 0
    settings.max_per_minute = 100
    settings.max_per_hour = 100
    settings.max_per_day = 0
    settings.send_allowlist = ""
    settings.patrol_interval = 3600
    install_fake_generator(handler_delay, [])

    service = XianyuChannelService(settings, api_factory=offline_api_factory)
    await service.start()
    ready = await service.wait_ready(timeout=45)
    return service, ready


async def wait_for(predicate, timeout=20.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def scenario_live_send_and_slow_handler():
    """① 真发送链路：推送 → 队列 → 引擎 → 出站帧真的写出去；② 慢处理不堵协议层。"""
    platform = await FakePlatform().start()
    calls = []
    from app.channels.xianyu import reply
    install_fake_generator(0, calls)          # 先不慢，把发送链路跑通
    service, ready = await build_service(platform, shadow=False, handler_delay=0,
                                         db_name="seam_live")
    check("真实服务栈对着假平台完成注册（真实 ws/engine/session 代码路径）", ready and platform.regs >= 1,
          f"ready={ready} 注册={platform.regs} last_error={service.last_error}")
    if not ready:
        await service.stop()
        await platform.stop()
        return False

    await platform.push(chat_frame("seam-live-1", "考研资料", created=int(time.time() * 1000)))
    ok = await wait_for(lambda: len(platform.outbound) >= 1, timeout=25)
    drafts = service.session.list_drafts(10)
    check("★ 平台推送 → 交付队列 → 引擎 → **出站帧真的发出去了**",
          ok and len(drafts) >= 1, f"出站帧={len(platform.outbound)} 草稿={len(drafts)}")
    if ok:
        cid, text, receivers = decode_outbound(platform.outbound[0])
        check("★ 出站帧指向正确的会话与收件人（真机上发错人是最可怕的事故）",
              cid == f"{CID}@goofish" and f"{BUYER}@goofish" in receivers,
              f"cid={cid} receivers={receivers}")
        check("★ 发出的正文就是草稿正文（不是模板或空串）",
              text == drafts[0]["reply"], f"发出={text!r} 草稿={drafts[0]['reply']!r}")
        check("草稿被标记已发送 + 计数正确",
              str(drafts[0]["sent"]) == "1" and service.engine.counters["sent"] == 1,
              f"sent={drafts[0]['sent']} counter={service.engine.counters['sent']}")

    # 重放分两种，两道防线各管一种，必须分开验：
    #   ① 同一进程内整帧重放 → 收帧层的内存去重拦住（ws_duplicate）
    #   ② 进程重启后的重放（内存去重已清空）→ 只能靠**落 SQLite 的消息级幂等**拦住
    replay_frame = chat_frame("seam-live-replay", "考研资料",
                              created=int(time.time() * 1000) - 1000)
    before = len(platform.outbound)
    await platform.push(replay_frame)
    await wait_for(lambda: service.engine.ws.stats["duplicates"] >= 1, timeout=10)
    await platform.push(replay_frame)
    await asyncio.sleep(1.5)
    check("① 同进程内整帧重放被收帧层内存去重拦住（ws_duplicate，不再多回一条）",
          len(platform.outbound) == before and service.engine.ws.stats["duplicates"] >= 1,
          f"出站帧 {before}→{len(platform.outbound)} duplicates={service.engine.ws.stats['duplicates']}")

    # 模拟重启：把内存去重换新的（等价于刚启动），再推同一帧
    from app.channels.xianyu.ws import InMemoryDedupe
    service.engine.ws.dedupe = InMemoryDedupe()
    before2 = len(platform.outbound)
    await platform.push(replay_frame)
    ok_guard = await wait_for(
        lambda: any(e["kind"] == "duplicate_message_skipped" for e in service.session.recent_events(30)),
        timeout=10)
    await asyncio.sleep(1.5)
    check("★ ② 重启后的重放被 SQLite 消息级幂等拦住（真发送下防「给买家回两条」）",
          ok_guard and len(platform.outbound) == before2,
          f"出站帧 {before2}→{len(platform.outbound)} 幂等跳过事件={'有' if ok_guard else '无'}")

    # 慢处理：处理函数睡 4 秒，期间心跳必须照常被应答、连接不能被自己关掉
    service.engine.reply_generator = None
    import app.channels.xianyu.engine as engine_mod
    calls.clear()

    async def slow_generator(text, chat_id, history, item_desc):
        await asyncio.sleep(4)
        calls.append(text)
        return {"reply": "[seam] 慢回复", "intent": "default", "sources": [], "engine": "seam-fake"}

    service.engine.reply_generator = slow_generator
    acks_before = platform.acks
    await platform.push(chat_frame("seam-slow-1", "发货快吗", created=int(time.time() * 1000)))
    await asyncio.sleep(3.0)   # 处理函数还在睡
    acks_during = platform.acks - acks_before
    timeouts_during = service.engine.ws.stats["heartbeat_timeouts"]
    connected_during = service.engine.ws.connected
    check("★ 处理慢消息期间心跳照常被应答（收帧循环没被业务堵住）",
          acks_during >= 1 and timeouts_during == 0,
          f"处理中应答={acks_during} 判定超时={timeouts_during}")
    check("★ 处理慢消息期间连接不被自己关掉（真机事故的直接回归）",
          connected_during, f"connected={connected_during}")
    ok_slow = await wait_for(lambda: len(calls) >= 1, timeout=15)
    check("慢消息最终仍被处理完", ok_slow, f"处理条数={len(calls)}")

    check("交付队列指标可见（面板/长跑体检用）",
          "deliver_queue" in service.engine.ws.health()
          and service.engine.ws.stats.get("deliver_dropped", 0) == 0,
          f"queue={service.engine.ws.health().get('deliver_queue')} "
          f"errors={service.engine.ws.stats.get('deliver_errors')}")

    await service.stop()
    await platform.stop()
    return True


async def scenario_shadow_never_sends():
    """影子模式硬约束：整条链路跑通但**一条都不发**（出站帧为零）。"""
    platform = await FakePlatform().start()
    service, ready = await build_service(platform, shadow=True, handler_delay=0,
                                         db_name="seam_shadow")
    if not ready:
        await service.stop()
        await platform.stop()
        return check("影子模式子场景：服务未起来", False, f"last_error={service.last_error}")
    await platform.push(chat_frame("seam-shadow-1", "这个还在吗", created=int(time.time() * 1000)))
    ok = await wait_for(lambda: len(service.session.list_drafts(10)) >= 1, timeout=20)
    await asyncio.sleep(1.0)
    check("★ 影子模式：草稿照常生成，但平台一个出站帧都没收到",
          ok and not platform.outbound and service.engine.ws.stats["texts_sent"] == 0,
          f"草稿={len(service.session.list_drafts(10))} 出站帧={len(platform.outbound)}")
    await service.stop()
    await platform.stop()


async def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_out", default=None)
    args = parser.parse_args(argv)

    await scenario_live_send_and_slow_handler()
    await scenario_shadow_never_sends()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"闲鱼实时链路端到端自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {n}" + (f" — {d}" if d else "") for n, ok, d in RESULTS]
    (ROOT / ".logs" / "xianyu_live_seam_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"passed": passed, "total": total,
             "results": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS]},
            ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n闲鱼实时链路端到端自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
