# -*- coding: utf-8 -*-
r"""P1 存活体检：连上闲鱼长连接、**只收不回**，跑一段固定时间后出一份报告。

用途：在正式开影子模式/自动回复之前，先确认「连得上、注册成功、心跳正常、能收到帧」。
**本脚本绝不会回复任何买家**：它只用 `XianyuWSClient` 收帧计数，不构造引擎、不构造生成器、
不调用 `send_text`，并在报告里断言 `texts_sent == 0`；报告里也不写 Cookie/token 明文。

用法：
    # 真机体检（默认跑 60 秒；会用到凭据文件里的登录态）
    python scripts/xianyu_live_check.py --seconds 60

    # 只连不查登录态（少一次接口调用）
    python scripts/xianyu_live_check.py --seconds 60 --skip-login-check

    # 离线自测：指向本地假服务器（不连闲鱼）
    python scripts/xianyu_live_check.py --seconds 5 --url ws://127.0.0.1:PORT/ --fake-api

⚠️ 与旧 bot 同账号同时在线可能互相挤；真要跑之前请先停旧 bot（或换小号）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

from app.channels.xianyu.api import MTOP_ITEM_URL, MTOP_TOKEN_URL, XianyuApi  # noqa: E402
from app.channels.xianyu.protocol import WS_URL, load_credentials, load_or_create_device_id  # noqa: E402
from app.channels.xianyu.ws import XianyuWSClient  # noqa: E402

MASK_RE = re.compile(r"[0-9a-zA-Z_\-]{16,}")


def _mask(value: str, keep: int = 6) -> str:
    """脱敏：只留前后几位，报告里不出现完整凭据。"""
    if not value:
        return ""
    return value[:keep] + "***" + value[-2:]


class FrameCounter:
    """收帧统计器：**只计数，不做任何回复**。"""

    def __init__(self, max_samples: int = 5) -> None:
        self.frames = 0
        self.chat_messages = 0
        self.typing = 0
        self.order_status = 0
        self.duplicates = 0
        self.events: Dict[str, int] = {}
        self.reply_attempts = 0        # 必须恒为 0：本脚本没有任何回复逻辑
        self.samples: list = []
        self._max_samples = max_samples

    async def on_message(self, message) -> None:
        """收到一条「买家/卖家消息」——只记录，绝不回复。"""
        self.chat_messages += 1
        if len(self.samples) < self._max_samples:
            self.samples.append({
                "chat_id": _mask(message.chat_id, 4),
                "sender": _mask(message.sender_id, 4),
                "item_id": _mask(str(message.item_id or ""), 4),
                "text_len": len(message.text or ""),
                "from_seller": message.from_seller,
                "age_s": round((time.time() * 1000 - (message.create_time_ms or 0)) / 1000, 1),
            })

    def on_event(self, kind: str, payload: Dict[str, Any]) -> None:
        """通道事件计数（同样不触发任何回复）。"""
        self.events[kind] = self.events.get(kind, 0) + 1
        if kind == "order_status":
            self.order_status += 1


def _find_leaks(report_text: str, secrets: Dict[str, str]) -> list:
    """检查报告里是否混进了凭据明文（返回泄漏项的名字列表）。"""
    leaks = []
    for name, value in secrets.items():
        if value and len(value) >= 8 and value in report_text:
            leaks.append(name)
    return leaks


async def run_live_check(*, seconds: int = 60, url: str = WS_URL,
                         credential_path: Optional[Path] = None, fake_api: bool = False,
                         skip_login_check: bool = False, heartbeat_interval: int = 15,
                         device_id: Optional[str] = None,
                         api_factory: Optional[Callable[[], XianyuApi]] = None) -> Dict[str, Any]:
    """跑一次存活体检并返回报告（可被测试直接调用）。"""
    started = time.time()
    report: Dict[str, Any] = {
        "url": url, "seconds": seconds, "fake_api": fake_api, "started_at": started,
        "account": None, "device_id_suffix": None, "login_ok": None, "steps": [],
        "sends": 0, "ok": False, "problems": [],
    }

    def step(name: str, **extra: Any) -> None:
        report["steps"].append({"name": name, "at": round(time.time() - started, 2), **extra})

    # 1) 凭据
    if api_factory is None:
        creds_path = credential_path or (ROOT / "secrets" / "xianyu_credentials.json")
        creds = load_credentials(creds_path)
        unb = creds["account"]["unb"]
        report["account"] = _mask(unb, 4)
        device_id = device_id or load_or_create_device_id(unb, ROOT / "data" / "xianyu_device.json")
        report["device_id_suffix"] = device_id[-8:]
        cookies = creds["_cookies"]

        if fake_api:
            def handler(request: httpx.Request) -> httpx.Response:
                if str(request.url).startswith(MTOP_TOKEN_URL):
                    return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "FAKE"}})
                if str(request.url).startswith(MTOP_ITEM_URL):
                    return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {}}})
                return httpx.Response(200, json={"content": {"success": True}})

            api = XianyuApi(cookies, credential_path=None, transport=httpx.MockTransport(handler))
        else:
            api = XianyuApi(cookies, credential_path=creds_path, user_agent=creds.get("user_agent"))
    else:
        api = api_factory()
        device_id = device_id or f"FAKE-DEVICE-{int(time.time())}"
        report["account"] = _mask(api.unb, 4)
        report["device_id_suffix"] = device_id[-8:]

    counter = FrameCounter()
    client = XianyuWSClient(api, device_id, url=url, on_message=counter.on_message,
                            on_event=counter.on_event,
                            heartbeat_interval=heartbeat_interval, heartbeat_timeout=5,
                            reconnect_initial=1.0, reconnect_max=8.0)

    try:
        # 2) 登录态（可选）
        if not skip_login_check:
            try:
                report["login_ok"] = await api.has_login()
                step("has_login", ok=report["login_ok"])
            except Exception as exc:
                report["login_ok"] = False
                step("has_login", ok=False, error=f"{type(exc).__name__}: {exc}")
                report["problems"].append(f"登录态检查异常: {exc}")

        # 3) 连上 + 注册（run_once 内部会取 token、发 /reg 与 ackDiff）
        task = asyncio.create_task(client.run_once())
        deadline = started + seconds
        registered_at = None
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            if client.registered and registered_at is None:
                registered_at = time.time() - started
                step("registered", after_s=round(registered_at, 2))
            if task.done():
                break
        step("collected", frames=client.stats["frames_in"], messages=counter.chat_messages,
             heartbeats=client.stats["heartbeats_sent"])

        # 交付是异步的（收帧循环只入队，由消费者处理）：收尾前先等队列清空，
        # 否则统计会漏掉"刚收到、还在队列里"的消息（体检报告就少算一条）
        drained = await client.drain_delivery(timeout=5.0)
        step("delivered", messages=counter.chat_messages, queue_drained=drained)

        client.request_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        await api.aclose()

    stats = client.stats
    report.update({
        "connected": client.stats["connects"] > 0,
        "registered": bool(report.get("steps") and any(s["name"] == "registered" for s in report["steps"])),
        "registered_after_s": next((s["after_s"] for s in report["steps"] if s["name"] == "registered"), None),
        "frames_in": stats["frames_in"],
        "acks_sent": stats["acks_sent"],
        "heartbeats_sent": stats["heartbeats_sent"],
        "heartbeats_acked": stats.get("heartbeats_acked", 0),
        "heartbeat_timeouts": stats["heartbeat_timeouts"],
        "decode_errors": stats["decode_errors"],
        "frame_errors": stats["frame_errors"],
        "duplicates": stats["duplicates"],
        "dropped": stats["dropped"],
        "chat_messages": counter.chat_messages,
        "typing_frames": stats["dropped"].get("typing", 0),
        "order_status_events": counter.order_status,
        "channel_events": counter.events,
        "sends": stats.get("texts_sent", 0),
        "reply_attempts": counter.reply_attempts,
        "samples": counter.samples,
        "elapsed_s": round(time.time() - started, 1),
    })

    # 凭据泄漏自检：报告里不允许出现 cookie/token/unb 明文
    secrets = {"access_token": client.current_token or ""}
    for name, value in (api.cookies or {}).items():
        if len(str(value)) >= 12:
            secrets[f"cookie:{name}"] = str(value)
    leaks = _find_leaks(json.dumps(report, ensure_ascii=False), secrets)
    report["secret_leaks"] = leaks
    if leaks:
        report["problems"].append(f"报告里出现凭据明文: {leaks}")

    # 4) 判定
    if not report["connected"]:
        report["problems"].append("没有建立连接（网络/URL/凭据）")
    if not report["registered"]:
        report["problems"].append("没有完成 /reg 注册（token 或握手异常）")
    if report["registered"] and report["heartbeats_sent"] and report["heartbeat_timeouts"]:
        report["problems"].append("心跳出现超时（连接不稳）")
    if report["registered"] and report["heartbeats_sent"] >= 2 and not report["heartbeats_acked"]:
        report["problems"].append(
            f"心跳无应答（发 {report['heartbeats_sent']} 次、收到 0 次）：连接可能已被服务端静默丢弃")
    if report["sends"] != 0 or counter.reply_attempts != 0:
        report["problems"].append("严重：本应「只连不回」，却检测到发送/回复动作")
    report["ok"] = not report["problems"]

    # 报告落盘
    out = ROOT / ".logs" / "xianyu_live_check.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(out)
    return report


def _print_report(report: Dict[str, Any]) -> None:
    print("=" * 62)
    print("闲鱼存活体检（只连不回）")
    print("=" * 62)
    print(f"  目标      : {report['url']}")
    print(f"  账号      : {report.get('account')}  设备号后缀: {report.get('device_id_suffix')}")
    print(f"  登录态    : {report.get('login_ok')}")
    print(f"  已连接    : {report.get('connected')}   已注册: {report.get('registered')}"
          f"（耗时 {report.get('registered_after_s')}s）")
    print(f"  收帧      : {report.get('frames_in')}   ACK: {report.get('acks_sent')}"
          f"   聊天消息: {report.get('chat_messages')}")
    print(f"  心跳      : 发送 {report.get('heartbeats_sent')} 次，应答 {report.get('heartbeats_acked')} 次，"
          f"超时 {report.get('heartbeat_timeouts')} 次")
    print(f"  解码/帧错误: {report.get('decode_errors')} / {report.get('frame_errors')}"
          f"   重复帧: {report.get('duplicates')}")
    print(f"  发送动作  : {report.get('sends')}（必须为 0：本脚本不回复任何人）")
    print(f"  丢帧统计  : {report.get('dropped')}")
    print(f"  用时      : {report.get('elapsed_s')}s")
    print(f"  结论      : {'✅ 通过' if report['ok'] else '❌ 有问题'}")
    for problem in report["problems"]:
        print(f"    - {problem}")
    print(f"  报告      : {report.get('report_path')}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="闲鱼存活体检（只连不回复）")
    parser.add_argument("--seconds", type=int, default=60, help="采集时长（秒），默认 60")
    parser.add_argument("--url", default=WS_URL, help="覆盖 WSS 地址（自测用）")
    parser.add_argument("--skip-login-check", action="store_true", help="跳过 hasLogin 调用")
    parser.add_argument("--fake-api", action="store_true", help="用假接口（离线自测，不连闲鱼）")
    parser.add_argument("--credentials", default=None, help="凭据文件路径")
    args = parser.parse_args(argv)

    report = asyncio.run(run_live_check(
        seconds=args.seconds, url=args.url, fake_api=args.fake_api,
        skip_login_check=args.skip_login_check,
        credential_path=Path(args.credentials) if args.credentials else None))
    _print_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
