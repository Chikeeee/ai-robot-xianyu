# -*- coding: utf-8 -*-
r"""只读取件工具（xianyu_fetch_messages.py）的离线自检：本地假服务器按协议回应拉取请求。

覆盖：请求构造（lwp/body 形状与参考实现一致）、响应解析（sender/text/时间/结构摘要）、
分页字段（hasMore）、以及「平台没回匹配帧」时如实返回未匹配。

用法：python scripts/test_xianyu_fetch.py
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

import xianyu_fetch_messages as fetch_mod  # noqa: E402
from app.channels.xianyu.api import MTOP_TOKEN_URL, XianyuApi  # noqa: E402

RESULTS = []
REAL_CRED = ROOT / "secrets" / "xianyu_credentials.json"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


class FakeIMServer:
    """假 IM 服务器：记录请求，按脚本回应用户消息拉取。"""

    def __init__(self, *, respond=True, has_more=False, messages=3):
        self.respond = respond
        self.has_more = has_more
        self.messages = messages
        self.requests = []
        self._server = None
        self.port = None

    async def start(self):
        async def handler(ws):
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    self.requests.append(frame)
                    if frame.get("lwp") == "/!":
                        mid = (frame.get("headers") or {}).get("mid")
                        if mid:
                            await ws.send(json.dumps({"code": 200, "headers": {"mid": mid}}))
                        continue
                    if frame.get("lwp") == fetch_mod.LIST_URL and self.respond:
                        mid = frame["headers"]["mid"]
                        models = []
                        for i in range(self.messages):
                            payload = {"contentType": 1, "text": {"text": f"买家第{i + 1}句"}}
                            models.append({"message": {
                                "content": {"custom": {"data": base64.b64encode(
                                    json.dumps(payload).encode()).decode()}},
                                "extension": {"senderUserId": "11112222", "reminderTitle": "小号A"},
                                "createTime": int(time.time() * 1000) - (self.messages - i) * 1000,
                            }})
                        await ws.send(json.dumps({
                            "headers": {"mid": mid, "sid": "s1"},
                            "body": {"userMessageModels": models, "hasMore": 1 if self.has_more else 0},
                        }))
            except Exception:
                pass

        self._server = await serve(handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def list_requests(self):
        return [r for r in self.requests if r.get("lwp") == fetch_mod.LIST_URL]


def api_factory(cookies, user_agent, credential_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(MTOP_TOKEN_URL):
            return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "FAKE"}})
        return httpx.Response(200, json={"content": {"success": True}})

    return XianyuApi(cookies, credential_path=None, transport=httpx.MockTransport(handler))


async def scenario_ok():
    server = await FakeIMServer(messages=3, has_more=True).start()
    report = await fetch_mod.fetch("11112222", limit=5, url=f"ws://127.0.0.1:{server.port}/",
                                   credential_path=REAL_CRED, api_factory=api_factory)
    await server.stop()

    reqs = server.list_requests()
    body = reqs[0]["body"] if reqs else []
    check("请求构造与参考实现一致（lwp=/r/MessageManager/listUserMessages）",
          bool(reqs) and reqs[0]["lwp"] == fetch_mod.LIST_URL,
          f"请求数={len(reqs)}")
    check("请求 body 形状：['<cid>@goofish', False, 大cursor, limit, False]",
          len(body) == 5 and body[0] == "11112222@goofish" and body[1] is False
          and body[2] == 9007199254740991 and body[3] == 5 and body[4] is False,
          json.dumps(body, ensure_ascii=False))
    check("响应解析：匹配到响应、取出 3 条消息（含发送者/文本/时间）",
          report["matched"] and len(report["messages"]) == 3
          and report["messages"][0]["sender_name"] == "小号A"
          and report["messages"][0]["text"] == "买家第1句"
          and report["messages"][0]["create_time"],
          f"matched={report['matched']} 条数={len(report['messages'])}")
    check("保留 hasMore 与结构摘要（便于看真实字段形状）",
          report.get("has_more") == 1
          and report["messages"][0]["structure"].get("senderUserId") == "11112222",
          json.dumps(report["messages"][0]["structure"], ensure_ascii=False))
    check("只读：整个过程没有发送消息帧",
          not [r for r in server.requests if r.get("lwp") == "/r/MessageSend/sendByReceiverScope"],
          f"总请求数={len(server.requests)}")


async def scenario_no_response():
    server = await FakeIMServer(respond=False).start()
    report = await fetch_mod.fetch("11112222", limit=3, url=f"ws://127.0.0.1:{server.port}/",
                                   credential_path=REAL_CRED, api_factory=api_factory)
    await server.stop()
    check("平台没回匹配帧时：如实返回 matched=false、消息为空（不编造）",
          report["matched"] is False and report["messages"] == [],
          f"matched={report['matched']} frames={len(report['frames'])}")


async def main():
    await scenario_ok()
    await scenario_no_response()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"只读取件工具离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_fetch_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n只读取件工具离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
