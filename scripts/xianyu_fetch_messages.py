# -*- coding: utf-8 -*-
r"""闲鱼排查工具：按会话 id 拉取最近聊天记录（只读，不发消息）。

用途：当「买家消息没被推送到本通道」时，用来确认**平台上到底有没有这条消息**，
并看到真实消息帧的字段结构（这正是解析器要对齐的东西）。

接口来自参考实现 cv-cat/XianYuApis 的 `listUserMessages`：
`lwp=/r/MessageManager/listUserMessages`，body 为 `[<cid>@goofish, False, 9007199254740991, <limit>, False]`。

用法：
    python scripts/xianyu_fetch_messages.py --cid 1000000000001 --limit 5
    python scripts/xianyu_fetch_messages.py --cid <对方userId> --limit 5 --dump .logs/fetch.json

⚠️ 只读：不会发送任何消息。会用到凭据文件里的登录态（与 P1 体检同级别）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

from app.channels.xianyu.api import XianyuApi  # noqa: E402
from app.channels.xianyu.protocol import (  # noqa: E402
    WS_URL,
    load_credentials,
    load_or_create_device_id,
)
from app.channels.xianyu.ws import (  # noqa: E402
    _structure,
    build_ack,
    generate_mid,
    is_sync_package,
)

LIST_URL = "/r/MessageManager/listUserMessages"


async def fetch(cid: str, limit: int = 10, *, url: str = WS_URL,
                credential_path: Optional[Path] = None, timeout: float = 25.0,
                api_factory=None) -> Dict[str, Any]:
    creds_path = credential_path or (ROOT / "secrets" / "xianyu_credentials.json")
    creds = load_credentials(creds_path)
    if api_factory:
        api = api_factory(creds["_cookies"], creds.get("user_agent"), creds_path)
    else:
        api = XianyuApi(creds["_cookies"], credential_path=creds_path,
                        user_agent=creds.get("user_agent"))
    device_id = load_or_create_device_id(creds["account"]["unb"], ROOT / "data" / "xianyu_device.json")

    from websockets.asyncio.client import connect as ws_connect

    token = (await api.get_token(device_id)).access_token
    headers = {
        "Cookie": api.cookie_str(), "Host": "wss-goofish.dingtalk.com", "Connection": "Upgrade",
        "User-Agent": api.user_agent, "Origin": "https://www.goofish.com",
    }
    send_mid = generate_mid()
    request = {"lwp": LIST_URL, "headers": {"mid": send_mid},
               "body": [f"{cid}@goofish", False, 9007199254740991, limit, False]}
    report: Dict[str, Any] = {"cid": cid, "request_mid": send_mid, "url": url,
                              "messages": [], "frames": [], "matched": False, "error": None}

    async with ws_connect(url, additional_headers=headers, open_timeout=20) as ws:
        await ws.send(json.dumps({
            "lwp": "/reg",
            "headers": {"cache-header": "app-key token ua wv",
                        "app-key": "444e9908a51d1cb236a27862abc769c9", "token": token,
                        "ua": api.user_agent, "dt": "j", "wv": "im:3,au:3,sy:6",
                        "sync": "0,0;0;0;", "did": device_id, "mid": generate_mid()}}))
        await asyncio.sleep(1)
        await ws.send(json.dumps(request))

        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            frame = json.loads(raw)
            ack = build_ack(frame)
            if ack:
                await ws.send(json.dumps(ack))
            if is_sync_package(frame):
                continue
            mid = (frame.get("headers") or {}).get("mid")
            report["frames"].append({"mid": mid, "lwp": frame.get("lwp"),
                                     "structure": _structure(frame)})
            if mid == send_mid or frame.get("lwp") == LIST_URL:
                report["matched"] = True
                body = frame.get("body") or {}
                models = body.get("userMessageModels") or []
                report["has_more"] = body.get("hasMore")
                for model in models:
                    message = model.get("message") or {}
                    content = (message.get("content") or {}).get("custom") or {}
                    text = ""
                    try:
                        payload = json.loads(base64.b64decode(content.get("data") or "").decode())
                        text = (payload.get("text") or {}).get("text", "")
                    except Exception:
                        pass
                    ext = message.get("extension") or {}
                    report["messages"].append({
                        "sender_id": ext.get("senderUserId"),
                        "sender_name": ext.get("reminderTitle"),
                        "text": text,
                        "create_time": message.get("createTime"),
                        "structure": _structure(ext),
                        "raw_model": model,
                    })
                break
    await api.aclose()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按会话 id 拉取闲鱼最近聊天记录（只读）")
    parser.add_argument("--cid", required=True, help="会话 id（通常是对方的 userId）")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--url", default=WS_URL)
    parser.add_argument("--dump", default=None)
    parser.add_argument("--dns", default=None,
                        help='DNS 覆盖，如 "wss-goofish.dingtalk.com=106.11.130.193"（本机 ISP DNS 故障时用）')
    args = parser.parse_args(argv)

    if args.dns:
        from app.channels.xianyu.dns import apply_override
        print(f"DNS 覆盖: {apply_override(args.dns)}")

    report = asyncio.run(fetch(args.cid, args.limit, url=args.url))
    print("=" * 60)
    print(f"会话 {report['cid']}：匹配到响应={report['matched']}，消息 {len(report['messages'])} 条")
    for m in report["messages"]:
        print(f"  [{m['create_time']}] {m['sender_name']}({str(m['sender_id'])[:6]}***): {m['text'][:60]}")
    print(f"  收到 {len(report['frames'])} 个非同步帧")
    if args.dump:
        Path(args.dump).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  报告：{args.dump}")
    return 0 if report["matched"] else 2


if __name__ == "__main__":
    sys.exit(main())
