# -*- coding: utf-8 -*-
r"""闲鱼通道服务层离线自检：默认关闭时的接口行为 + 用本地假服务器验证「离线也能托管启动」。

覆盖：
1. 未启用时（默认）健康接口如实返回 `enabled=false`、`started=false`，且**不建连**；
2. 显式启用后用本地假 WebSocket 服务器启动成功（影子模式），健康快照字段齐全；
3. 草稿/事件/接管列表接口可用，接管切换接口能落库；
4. 凭据缺失时启动失败被隔离（记 last_error、不抛异常、不影响主服务）。

用法：python scripts/test_xianyu_service.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")
os.environ["XIANYU_ENABLED"] = "false"  # 先确保默认关闭

RESULTS = []
REAL_CRED = ROOT / "secrets" / "xianyu_credentials.json"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


class TinyServer:
    """只回应心跳注册的最小假服务器（够 service 启动即可）。"""

    def __init__(self):
        self.regs = 0
        self.heartbeats = 0
        self._server = None
        self.port = None

    async def start(self):
        async def handler(ws):
            try:
                async for raw in ws:
                    frame = json.loads(raw)
                    if frame.get("lwp") == "/reg":
                        self.regs += 1
                    elif frame.get("lwp") == "/!":
                        self.heartbeats += 1
                        mid = (frame.get("headers") or {}).get("mid")
                        if mid:
                            await ws.send(json.dumps({"code": 200, "headers": {"mid": mid}}))
            except Exception:
                pass

        self._server = await serve(handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()


def scenario_disabled():
    """默认关闭：接口如实返回未启用，且绝不建连。"""
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/v1/xianyu/health")
        body = r.json()
        check("未启用时健康接口可用且如实返回 enabled=false / started=false",
              r.status_code == 200 and body["enabled"] is False and body["started"] is False,
              f"enabled={body.get('enabled')} started={body.get('started')} session={body.get('session')}")
        r2 = client.get("/api/v1/xianyu/drafts")
        r3 = client.get("/api/v1/xianyu/events")
        check("未启用时草稿/事件接口返回空列表而不是报错",
              r2.status_code == 200 and r2.json() == {"drafts": []}
              and r3.status_code == 200 and r3.json() == {"events": []},
              f"drafts={r2.json()} events={r3.json()}")
        # 注意：shadow_mode 取自运行环境的 .env（灰度期间会被改成 false），
        # 这里只断言"字段如实暴露且类型正确"，不让运维配置把门禁搞红
        check("健康快照里带配置摘要（便于排障：凭据是否存在、开关、心跳周期）",
              "config" in body and body["config"]["credentials_exists"] is True
              and isinstance(body["config"]["shadow_mode"], bool)
              and "heartbeat_interval" in body["config"],
              json.dumps({k: body["config"][k] for k in ("enabled", "shadow_mode", "credentials_exists")},
                         ensure_ascii=False))


async def scenario_enabled_offline():
    """显式启用后用本地假服务器托管启动（影子模式）。"""
    from app.channels.xianyu import config as cfg

    server = await TinyServer().start()
    settings = cfg.XianyuSettings()
    settings.enabled = True
    settings.shadow_mode = True
    settings.ws_url = f"ws://127.0.0.1:{server.port}/"
    settings.heartbeat_interval = 5
    settings.db_path = ROOT / ".logs" / "ktest_service.db"
    settings.device_store = ROOT / ".logs" / "ktest_service_device.json"
    for suffix in ("", "-wal", "-shm"):
        Path(str(settings.db_path) + suffix).unlink(missing_ok=True)

    from app.channels.xianyu.service import XianyuChannelService

    service = XianyuChannelService(settings)
    ok = await service.start()
    # 超时给足：门禁里 12 个套件连着跑，建连偶发慢（本机曾出现一次抖动导致误报）
    ready = await service.wait_ready(timeout=45)
    check("显式启用后能托管启动并完成注册（用本地假服务器，不连闲鱼）",
          ok and service.started and ready and server.regs >= 1,
          f"started={service.started} registered={service.engine.ws.registered} 注册次数={server.regs} last_error={service.last_error}")

    health = service.health()
    check("健康快照含 ws/session/counters/接管列表，可直接喂面板",
          all(k in health for k in ("ws", "session", "counters", "manual_chats"))
          and health["ws"]["registered"] is True and health["shadow_mode"] is True,
          json.dumps({"registered": health["ws"]["registered"], "drafts": health["session"]["drafts"],
                      "messages": health["session"]["messages"]}, ensure_ascii=False))

    mode = service.toggle_manual("C9")
    check("接管切换接口生效并落库（等价卖家发接管词）",
          mode == "manual" and any(c["chat_id"] == "C9" for c in service.list_manual_chats()),
          f"mode={mode} chats={[c['chat_id'] for c in service.list_manual_chats()]}")
    mode2 = service.toggle_manual("C9")
    check("再切一次恢复自动回复", mode2 == "auto" and service.list_manual_chats() == [],
          f"mode={mode2}")

    await service.stop()
    check("停止后 started=false，连接已关闭",
          service.started is False and service.engine.ws.connected is False,
          f"started={service.started} connected={service.engine.ws.connected}")
    await server.stop()


async def scenario_bad_credentials():
    """凭据缺失：启动失败被隔离，不抛异常、主服务不受影响。"""
    from app.channels.xianyu import config as cfg
    from app.channels.xianyu.service import XianyuChannelService

    settings = cfg.XianyuSettings()
    settings.enabled = True
    settings.credentials_path = ROOT / ".logs" / "no_such_credentials.json"
    settings.db_path = ROOT / ".logs" / "ktest_service2.db"
    service = XianyuChannelService(settings)
    ok = await service.start()
    check("凭据缺失时启动失败被隔离（记 last_error，不抛异常、不退出进程）",
          ok is False and service.started is False and service.last_error
          and "凭据" in service.last_error,
          f"last_error={service.last_error}")


async def main():
    scenario_disabled()
    await scenario_enabled_offline()
    await scenario_bad_credentials()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"闲鱼通道服务层离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_service_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n闲鱼通道服务层离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
