# -*- coding: utf-8 -*-
r"""闲鱼值守告警离线自检：冷却去重、规则映射、健康巡检、webhook 推送与失败隔离、告警查询接口。

不联网（webhook 用 httpx.MockTransport 假装飞书/企微）、不连闲鱼。
用法：python scripts/test_xianyu_alerts.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")
os.environ["XIANYU_ENABLED"] = "false"

from app.channels.xianyu.alerts import ALERT_PREFIX, AlertManager  # noqa: E402
from app.channels.xianyu.session import ChannelSessionStore  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def fresh_session(name):
    path = ROOT / ".logs" / f"ktest_alerts_{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return ChannelSessionStore(path)


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


async def scenario_rules():
    session = fresh_session("rules")
    mgr = AlertManager(session, cooldown_seconds=600)

    alert = await mgr.observe_ws_event("auth_error", {"error": "Cookie 已失效"})
    check("登录态失效 → critical 告警并落事件流水",
          alert is not None and alert.severity == "critical" and "Cookie" in alert.detail
          and any(e["kind"] == f"{ALERT_PREFIX}auth_error" for e in session.recent_events(10)),
          f"{alert.title if alert else '-'}")

    risk = await mgr.observe_ws_event("risk_control", {"error": "RGV587_ERROR"})
    check("风控 → critical 告警", risk is not None and risk.severity == "critical", risk.title if risk else "-")

    hb = await mgr.observe_ws_event("heartbeat_timeout", {"since": 21.0})
    check("心跳无响应 → warning 告警", hb is not None and hb.severity == "warning", hb.title if hb else "-")

    # 真机踩坑：平台不关连接，只回 401 token is not found。这条必须能吵醒人——
    # 否则值守"看着在跑"却收不到任何消息。
    invalid = await mgr.observe_ws_event("session_invalid", {"reason": "token is not found"})
    check("平台判定会话失效 → critical 告警（静默僵尸连接能被发现）",
          invalid is not None and invalid.severity == "critical"
          and any(e["kind"] == f"{ALERT_PREFIX}session_invalid" for e in session.recent_events(20)),
          invalid.title if invalid else "-")

    none_alert = await mgr.observe_ws_event("registered", {})
    check("未配置规则的事件不产生告警（避免噪音）", none_alert is None, "registered → 无告警")

    alerts = mgr.recent(10)
    check("最近告警可直接通过接口读（含 alert_kind）",
          len(alerts) >= 3 and all(a["alert_kind"] for a in alerts),
          f"{[a['alert_kind'] for a in alerts]}")


async def scenario_cooldown():
    clock = FakeClock()
    session = fresh_session("cooldown")
    mgr = AlertManager(session, cooldown_seconds=600, clock=clock)

    a1 = await mgr.fire("connection_lost", "未连接")
    a2 = await mgr.fire("connection_lost", "未连接")
    check("同类告警在冷却期内被抑制（不刷屏）",
          a1 is not None and a2 is None and mgr.suppressed_total == 1 and mgr.fired_total == 1,
          f"fired={mgr.fired_total} suppressed={mgr.suppressed_total}")

    clock.advance(601)
    a3 = await mgr.fire("connection_lost", "未连接")
    check("冷却期过后可再次告警，且带上期间被抑制的次数",
          a3 is not None and a3.count == 2 and mgr.fired_total == 2,
          f"count={a3.count if a3 else '-'}")
    check("告警总览可用于健康接口", set(mgr.stats()) >= {"fired", "suppressed", "webhook_configured", "active"},
          json.dumps(mgr.stats(), ensure_ascii=False))


async def scenario_patrol():
    session = fresh_session("patrol")
    mgr = AlertManager(session, cooldown_seconds=600)
    down = {"enabled": True, "started": True, "ws": {"connected": False, "registered": False},
            "last_error": None}
    out = await mgr.observe_health(down)
    check("健康巡检：启用中却未连接 → critical 告警",
          len(out) == 1 and out[0].kind == "connection_lost" and out[0].severity == "critical",
          out[0].title if out else "-")

    up = {"enabled": True, "started": True, "ws": {"connected": True, "registered": True}}
    out2 = await mgr.observe_health(up)
    recovered = [e for e in session.recent_events(10) if e["kind"] == f"{ALERT_PREFIX}recovered"]
    check("恢复后不再告警，并记一条 recovered 事件",
          out2 == [] and recovered and "connection_lost" not in mgr.stats()["active"],
          f"recovered 事件 {len(recovered)} 条")

    disabled = {"enabled": False, "started": False, "ws": {"connected": False, "registered": False}}
    out3 = await mgr.observe_health(disabled)
    check("未启用时不告警（避免误报）", out3 == [], "enabled=false → 无告警")


async def scenario_webhook():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"code": 0})

    session = fresh_session("webhook")
    mgr = AlertManager(session, webhook_url="https://example.com/hook", style="feishu",
                       transport=httpx.MockTransport(handler))
    await mgr.fire("auth_error", "登录态失效", "需要重新导出 Cookie", "critical")
    check("webhook（飞书样式）推送了告警文本",
          sent and sent[0].get("msg_type") == "text" and "闲鱼值守" in sent[0]["content"]["text"],
          json.dumps(sent[0], ensure_ascii=False)[:100] if sent else "-")

    sent.clear()
    mgr2 = AlertManager(session, webhook_url="https://example.com/hook", style="wecom",
                        transport=httpx.MockTransport(handler))
    await mgr2.fire("risk_control", "触发风控")
    check("webhook（企微样式）字段正确",
          sent and sent[0].get("msgtype") == "text" and "content" in sent[0].get("text", {}),
          json.dumps(sent[0], ensure_ascii=False)[:100] if sent else "-")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("网络不通")

    mgr3 = AlertManager(session, webhook_url="https://example.com/hook",
                        transport=httpx.MockTransport(boom))
    alert = await mgr3.fire("heartbeat_timeout", "心跳无响应")
    check("webhook 推送失败被隔离（告警照常记录，不影响值守）",
          alert is not None and mgr3.webhook_failures == 1
          and any(e["kind"] == f"{ALERT_PREFIX}heartbeat_timeout" for e in session.recent_events(5)),
          f"failures={mgr3.webhook_failures}")


def scenario_endpoint():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/v1/xianyu/alerts")
        health = client.get("/api/v1/xianyu/health").json()
        check("告警接口在未启用时也能访问（返回空列表）", r.status_code == 200 and r.json() == {"alerts": []},
              str(r.json()))
        check("健康快照包含告警状态占位（未启用时为 null）", "alerts" in health,
              f"alerts={health.get('alerts')}")
        dash = client.get("/dashboard")
        check("控制台已包含值守面板与告警/草稿/事件区块",
              dash.status_code == 200 and "闲鱼值守" in dash.text and "影子模式草稿" in dash.text
              and "通道事件" in dash.text and "/api/v1/xianyu/health" in dash.text,
              f"dashboard 字节数={len(dash.content)}")
        # 两个「面板全绿但其实已经瞎了」的关键指标必须留在面板上：
        # session_invalid=平台不关连接只回 401 时的僵尸态；texts_sent=影子模式必须恒为 0
        check("★ 面板显示会话有效性与发条数（僵尸连接与影子模式破防都能一眼看出）",
              "x-session" in dash.text and "session_invalid" in dash.text
              and "texts_sent" in dash.text and "影子模式却已发送" in dash.text,
              "含 x-session / session_invalid / texts_sent 告警文案")


async def main():
    await scenario_rules()
    await scenario_cooldown()
    await scenario_patrol()
    await scenario_webhook()
    scenario_endpoint()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"闲鱼值守告警离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_alerts_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n闲鱼值守告警离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
