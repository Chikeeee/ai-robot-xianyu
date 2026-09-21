# -*- coding: utf-8 -*-
r"""P1 存活体检脚本的离线自检：用本地假服务器验证「只连不回」的硬约束与报告质量。

关键断言（这也是 P1 的核心安全要求）：
1. 体检过程**一条消息都没发出去**（服务端收到的 MessageSend 帧数 = 0）；
2. 报告里 `sends == 0` 且 `reply_attempts == 0`；
3. 报告**不含任何 Cookie/token 明文**；
4. 健康时判定通过（exit 0），连不上/注册不上时判定失败（exit 1）。

用法：python scripts/test_xianyu_live_check.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

import xianyu_live_check as live  # noqa: E402

RESULTS = []
REAL_CRED = ROOT / "secrets" / "xianyu_credentials.json"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


class FakeGoofish:
    """假服务器：正常应答注册与心跳，推几条帧（含聊天消息与订单状态）。"""

    def __init__(self, *, ack_heartbeat=True, push=True, accept=True):
        self.ack_heartbeat = ack_heartbeat
        self.push = push
        self.accept = accept
        self.regs = 0
        self.heartbeats = 0
        self.message_send_frames = []
        self.received = []
        self._server = None
        self.port = None

    async def start(self):
        async def handler(ws):
            try:
                pushed = False
                async for raw in ws:
                    frame = json.loads(raw)
                    self.received.append(frame)
                    lwp = frame.get("lwp")
                    if lwp == "/reg":
                        self.regs += 1
                    elif lwp == "/!":
                        self.heartbeats += 1
                        if self.ack_heartbeat:
                            mid = (frame.get("headers") or {}).get("mid")
                            if mid:
                                await ws.send(json.dumps({"code": 200, "headers": {"mid": mid}}))
                    elif lwp == "/r/MessageSend/sendByReceiverScope":
                        self.message_send_frames.append(frame)
                    if self.push and not pushed and self.regs:
                        pushed = True
                        await asyncio.sleep(0.4)
                        # 重放必须是**整帧一模一样**（含负载里的 createAt）：真机重放不会改时间戳，
                        # 每推一次重新生成时间戳等于模拟"另一条内容相同的消息"，测不出跨重连去重
                        replay = _chat_frame("lc-1", "11112222", "在吗？")
                        await ws.send(json.dumps(replay))
                        await asyncio.sleep(0.3)
                        await ws.send(json.dumps(_order_frame("lc-2")))
                        await asyncio.sleep(0.3)
                        await ws.send(json.dumps(replay))  # 同 mid、同负载重放
            except Exception:
                pass

        self._server = await serve(handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()


def _pack(obj):
    import struct
    if obj is None:
        return b"\xc0"
    if obj is True:
        return b"\xc3"
    if obj is False:
        return b"\xc2"
    if isinstance(obj, int):
        if 0 <= obj <= 0x7F:
            return bytes([obj])
        if 0 <= obj <= 0xFFFFFFFF:
            return b"\xce" + struct.pack(">I", obj)
        return b"\xcf" + struct.pack(">Q", obj)
    if isinstance(obj, str):
        raw = obj.encode("utf-8")
        if len(raw) <= 31:
            return bytes([0xA0 | len(raw)]) + raw
        return b"\xd9" + struct.pack(">B", len(raw)) + raw
    if isinstance(obj, dict):
        return bytes([0x80 | len(obj)]) + b"".join(_pack(k) + _pack(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return bytes([0x90 | len(obj)]) + b"".join(_pack(v) for v in obj)
    raise TypeError(type(obj))


def _b64(obj):
    import base64
    return base64.b64encode(_pack(obj)).decode()


def _chat_frame(mid, sender_id, text, item_id="888888", chat_id="C1"):
    payload = {"1": {"2": f"{chat_id}@goofish", "5": int(time.time() * 1000),
                     "10": {"senderUserId": sender_id, "reminderTitle": "买家A",
                            "reminderContent": text,
                            "reminderUrl": f"https://www.goofish.com/item?itemId={item_id}&x=1"}}}
    return {"headers": {"mid": mid, "sid": "s1"},
            "body": {"syncPushPackage": {"data": [{"data": _b64(payload)}]}}}


def _order_frame(mid, status="等待买家付款"):
    payload = {"1": "buyer@goofish", "3": {"redReminder": status}}
    return {"headers": {"mid": mid},
            "body": {"syncPushPackage": {"data": [{"data": _b64(payload)}]}}}


async def scenario_healthy():
    server = await FakeGoofish().start()
    report = await live.run_live_check(seconds=5, url=f"ws://127.0.0.1:{server.port}/",
                                       fake_api=True, skip_login_check=True,
                                       heartbeat_interval=2)
    await server.stop()

    check("体检能连上并完成注册", report["connected"] and report["registered"],
          f"connected={report['connected']} registered={report['registered']} after={report['registered_after_s']}s")
    check("心跳发出并被应答（无超时）",
          report["heartbeats_sent"] >= 1 and report["heartbeat_timeouts"] == 0,
          f"心跳={report['heartbeats_sent']} 超时={report['heartbeat_timeouts']}")
    check("收到帧并解析出聊天消息 / 订单状态 / 重复帧",
          report["frames_in"] >= 4 and report["chat_messages"] >= 1
          and report["order_status_events"] >= 1 and report["duplicates"] >= 1,
          f"收帧={report['frames_in']} 聊天={report['chat_messages']} 订单={report['order_status_events']} 重复={report['duplicates']}")
    check("★ 只连不回：服务端一条 MessageSend 都没收到，报告 sends=0 / reply_attempts=0",
          len(server.message_send_frames) == 0 and report["sends"] == 0
          and report["reply_attempts"] == 0,
          f"服务端收到发送帧={len(server.message_send_frames)}，报告 sends={report['sends']}")
    check("报告里不含 Cookie/token 明文（自动扫描）",
          report["secret_leaks"] == [], f"leaks={report['secret_leaks']}")
    check("采集时长受 --seconds 约束", 4.5 <= report["elapsed_s"] <= 7.5, f"elapsed={report['elapsed_s']}s")
    check("健康时结论为通过（exit 0）", report["ok"] is True and report["heartbeats_acked"] >= 1,
          f"problems={report['problems']} 心跳应答={report['heartbeats_acked']}")
    return report


async def scenario_broken():
    """服务端不回心跳：体检应判失败而不是无限等。"""
    server = await FakeGoofish(ack_heartbeat=False, push=False).start()
    report = await live.run_live_check(seconds=4, url=f"ws://127.0.0.1:{server.port}/",
                                       fake_api=True, skip_login_check=True,
                                       heartbeat_interval=1)
    await server.stop()
    check("心跳无响应时判定失败（并给出具体原因）",
          report["ok"] is False and report["heartbeats_sent"] >= 2 and report["heartbeats_acked"] == 0
          and any("心跳" in p for p in report["problems"]),
          f"发={report['heartbeats_sent']} 应答={report['heartbeats_acked']} problems={report['problems']}")
    return report


async def scenario_unreachable():
    """端口不可达：应判失败并写清原因，不抛异常。"""
    report = await live.run_live_check(seconds=3, url="ws://127.0.0.1:9/",
                                       fake_api=True, skip_login_check=True)
    check("连不上时判定失败且不抛异常",
          report["ok"] is False and report["connected"] is False
          and any("没有建立连接" in p for p in report["problems"]),
          f"problems={report['problems']}")
    return report


def scenario_real_credentials_loading():
    """真机模式（不连网）也要能加载凭据与设备号，且不把明文写进报告。"""
    import asyncio as aio

    async def _run():
        return await live.run_live_check(seconds=0, url="ws://127.0.0.1:9/", fake_api=True,
                                         credential_path=REAL_CRED)

    report = aio.run(_run())
    check("真机模式能读凭据并复用持久化 device_id（报告只出现脱敏账号）",
          report["account"] and report["account"].endswith("***86")
          and report["device_id_suffix"] and report["secret_leaks"] == [],
          f"account={report['account']} device 后缀={report['device_id_suffix']}")
    report_file = Path(report["report_path"])
    text = report_file.read_text(encoding="utf-8")
    creds = json.loads(REAL_CRED.read_text(encoding="utf-8"))
    cookie_values = [v for v in creds["cookies_str"].split("; ") if "=" in v]
    leaked = [c.split("=", 1)[1] for c in cookie_values
              if len(c.split("=", 1)[1]) >= 12 and c.split("=", 1)[1] in text]
    check("落盘报告文件里同样没有 Cookie 明文", leaked == [], f"泄漏字段数={len(leaked)}")


def main():
    asyncio.run(scenario_healthy())
    asyncio.run(scenario_broken())
    asyncio.run(scenario_unreachable())
    scenario_real_credentials_loading()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"P1 存活体检脚本离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_live_check_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\nP1 存活体检脚本离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
