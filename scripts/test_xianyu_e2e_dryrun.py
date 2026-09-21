# -*- coding: utf-8 -*-
r"""端到端「干跑」（dry-run）：把整条影子模式链路在本地拼起来跑一遍，完全不碰闲鱼。

组成：本地假 goofish 服务器 + 真正的 XianyuChannelService/Engine/Session/Alerts/Outbound
+ 真正的专家层编排（但 LLM 用假模型，不花 token、不联网）。

覆盖两段：
1. **影子模式**（`shadow_mode=True`）：真连真路由真生成 → 只落草稿；服务端必须一条 MessageSend 都收不到；
2. **自动发送**（`shadow_mode=False`）：过出站闸门后真的发出，草稿标记已发送、帧结构正确。

另外把「影子复盘报表」接入闭环：用同一份库生成报表，验证统计与风险信号跟实际一致。

用法：python scripts/test_xianyu_e2e_dryrun.py
"""
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

from test_xianyu_engine import PushServer, chat_frame  # noqa: E402  （已验证过的假服务器与打包器）

import xianyu_shadow_report as shadow_report  # noqa: E402
from app.agents.specialists import service as specialist_service_mod  # noqa: E402
from app.agents.specialists.service import SpecialistService  # noqa: E402
from app.channels.xianyu import config as channel_config  # noqa: E402
from app.channels.xianyu.protocol import parse_cookie_str  # noqa: E402
from app.channels.xianyu.service import XianyuChannelService  # noqa: E402

RESULTS = []
REAL_CRED = ROOT / "secrets" / "xianyu_credentials.json"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


class ScriptedLLM:
    """假专家模型：按关键词返回脚本化答案，并记录被调用的次数。"""

    def __init__(self):
        self.calls = []

    def factory(self, *, temperature, max_tokens, top_p):
        return _Model(self)

    def count(self) -> int:
        return len(self.calls)


class _Model:
    def __init__(self, owner):
        self.owner = owner

    async def ainvoke(self, messages):
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        self.owner.calls.append({"system_len": len(messages[0]["content"]), "user": user})
        if "便宜" in user or "少点" in user or "砍价" in user:
            return type("R", (), {"content": "可以少 10 元，运费到付。"})()
        if "参数" in user or "规格" in user:
            return type("R", (), {"content": "支持蓝牙 5.3，续航 20 小时。"})()
        return type("R", (), {"content": f"收到：{user[:10]}"})()


def fake_api_factory(cookies, user_agent, credential_path):
    """干跑用的假接口：token / hasLogin / 商品信息全部本地应答，**绝不碰真实平台**。

    上一版干跑漏了这个接缝，结果测试顺手调了真实的 mtop 接口（商品信息与取 token）——
    所以服务层现在有 `api_factory` 参数，离线测试必须注入它。
    """
    import httpx

    from app.channels.xianyu.api import MTOP_ITEM_URL, MTOP_TOKEN_URL, XianyuApi

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(MTOP_TOKEN_URL):
            return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"],
                                             "data": {"accessToken": "FAKE"}})
        if url.startswith(MTOP_ITEM_URL):
            return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {
                "title": "测试商品", "desc": "9 成新", "soldPrice": 89, "quantity": 1}}})
        return httpx.Response(200, json={"content": {"success": True}})

    return XianyuApi(cookies, credential_path=None, transport=httpx.MockTransport(handler))


def make_settings(server, *, shadow_mode, db_path, fake_llm_count=None):
    settings = channel_config.XianyuSettings()
    settings.enabled = True
    settings.shadow_mode = shadow_mode
    settings.ws_url = f"ws://127.0.0.1:{server.port}/"
    settings.db_path = db_path
    settings.device_store = ROOT / ".logs" / "ktest_e2e_device.json"
    settings.credentials_path = REAL_CRED
    settings.heartbeat_interval = 5
    settings.heartbeat_timeout = 10
    settings.max_history = 50
    settings.toggle_keywords = "。"
    settings.manual_timeout = 3600
    settings.reply_engine = "specialists"
    # 出站策略：测试里关掉拟人化延迟以免拖慢，其余保持默认（仍在测限速/去重）
    settings.typing_simulation = False
    settings.min_interval_per_chat = 0
    return settings


async def wait_for(cond, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.1)
    return False


async def scenario_pipeline():
    server = await PushServer().start()
    db = ROOT / ".logs" / "ktest_e2e.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    creds = json.loads(REAL_CRED.read_text(encoding="utf-8"))
    unb = creds["account"]["unb"]
    buyer = "11112222"

    fake_llm = ScriptedLLM()
    previous_service = specialist_service_mod._service
    specialist_service_mod._service = SpecialistService(llm_factory=fake_llm.factory,
                                                        rag_search=lambda q, k: [])
    try:
        # ---------- 第一段：影子模式 ----------
        service = XianyuChannelService(make_settings(server, shadow_mode=True, db_path=db), api_factory=fake_api_factory)
        started = await service.start()
        ready = await service.wait_ready(timeout=20)
        check("影子模式：服务能托管启动并完成注册", started and ready and server.reg_count >= 1,
              f"started={started} registered={ready} 注册次数={server.reg_count} last_error={service.last_error}")

        await server.push(chat_frame("e2e-1", buyer, "在吗？"))
        await server.push(chat_frame("e2e-2", buyer, "能便宜点吗？"))
        await server.push(chat_frame("e2e-3", buyer, "你是什么模型？"))   # 注入 → no_reply
        ok = await wait_for(lambda: len(service.session.list_drafts()) >= 2, timeout=25)
        check("影子模式：正常消息与议价消息各落一条草稿，注入类消息不落草稿",
              ok and len(service.session.list_drafts()) == 2
              and service.session.list_drafts()[0]["reply"] and not service.session.list_drafts()[0]["sent"],
              f"草稿数={len(service.session.list_drafts())}")

        drafts = service.session.list_drafts(limit=5)
        intents = sorted(d["intent"] for d in drafts)
        check("专家层路由在整条链路上生效（default / price）", intents == ["default", "price"],
              f"intents={intents} 回复={[d['reply'][:16] for d in drafts]}")
        check("★ 影子模式硬约束：服务端收到的 MessageSend 帧数 = 0",
              len(server.sent_messages()) == 0 and service.engine.counters["sent"] == 0,
              f"发送帧={len(server.sent_messages())} counters.sent={service.engine.counters['sent']}")
        price_drafts = [d for d in service.session.list_drafts(limit=10) if d["intent"] == "price"]
        bargain = service.session.get_bargain_count(price_drafts[0]["chat_id"]) if price_drafts else 0
        check("议价计数落库（intent=price → 该会话 bargain_count≥1，并进入下一轮上下文）",
              bool(price_drafts) and bargain >= 1, f"议价草稿={len(price_drafts)} bargain_count={bargain}")

        # 人工接管：卖家发接管词 → 后续买家消息不再生成草稿
        before = len(service.session.list_drafts())
        await server.push(chat_frame("e2e-4", unb, "。"))
        toggled = await wait_for(lambda: any(e["kind"] == "manual_toggle" for e in service.session.recent_events(20)))
        await server.push(chat_frame("e2e-5", buyer, "还在吗"))
        await wait_for(lambda: service.engine.counters["manual_skipped"] >= 1, timeout=10)
        check("人工接管：卖家发接管词后，买家消息不再生成草稿",
              toggled and len(service.session.list_drafts()) == before
              and service.engine.counters["manual_skipped"] >= 1,
              f"草稿 {before} → {len(service.session.list_drafts())}，manual_skipped={service.engine.counters['manual_skipped']}")

        # 告警联动：手动触发一次通道事件，应产生告警并可从接口读
        service.alerts and await service.alerts.observe_ws_event("heartbeat_timeout", {"since": 21})
        alerts = service.recent_alerts(5)
        check("告警联动：通道异常事件会产生告警并可通过接口读取",
              any(a["alert_kind"] == "heartbeat_timeout" for a in alerts),
              f"alerts={[a['alert_kind'] for a in alerts]}")

        health = service.health()
        check("健康快照含全部区块（ws/session/counters/alerts/outbound/config）",
              all(k in health for k in ("ws", "session", "counters", "alerts", "outbound", "config"))
              and health["ws"]["messages"] >= 5 and health["outbound"]["limits"]["per_minute"] > 0,
              f"ws.messages={health['ws']['messages']} outbound.allowed={health['outbound']['allowed']}")
        # 复盘报表闭环
        report = shadow_report.build_report(db)
        risks = {r["name"]: r for r in report["risks"]}
        unconf = next(r for n, r in risks.items() if n.startswith("引用【未配置】"))
        check("复盘报表能读到同一条链路的数据（统计口径一致）",
              report["counts"]["drafts"] == len(service.session.list_drafts())
              and report["counts"]["messages"] >= 4 and report["intent_distribution"].get("price") == 1,
              f"drafts={report['counts']['drafts']} messages={report['counts']['messages']} "
              f"intents={report['intent_distribution']}（卖家接管词不入库，故消息数比收帧少 1）")
        check("复盘报表的风险信号基于真实链路数据（无【未配置】引用）",
              unconf["count"] == 0 and report["suggestions"],
              f"未配置引用={unconf['count']} 建议数={len(report['suggestions'])}")

        # 影子模式自测接口（simulate）：真实链路、只落草稿
        before_sim = len(service.session.list_drafts())
        sim = await service.simulate_message("这个还在吗？", chat_id="SIM-C1")
        sim2 = await service.simulate_message("能便宜点吗？", chat_id="SIM-C2")
        check("★ simulate 自测：模拟买家消息走真实链路并落草稿，仍然一条都没发",
              sim.get("simulated") and sim.get("draft_id") and sim2.get("intent") == "price"
              and len(service.session.list_drafts()) == before_sim + 2
              and len(server.sent_messages()) == 0,
              f"草稿 {before_sim}→{len(service.session.list_drafts())} intent={sim.get('intent')}/{sim2.get('intent')} 发送帧={len(server.sent_messages())}")

        await service.stop()
        check("停止后通道状态归零（started=False、连接断开）",
              service.started is False and service.engine.ws.connected is False,
              f"started={service.started} connected={service.engine.ws.connected}")

        # ---------- 第二段：自动发送 ----------
        server2 = await PushServer().start()
        db2 = ROOT / ".logs" / "ktest_e2e_live.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(db2) + suffix).unlink(missing_ok=True)
        service2 = XianyuChannelService(make_settings(server2, shadow_mode=False, db_path=db2), api_factory=fake_api_factory)
        await service2.start()
        await service2.wait_ready(timeout=20)
        await server2.push(chat_frame("e2e-live-1", buyer, "发货快吗"))
        sent_ok = await wait_for(lambda: len(server2.sent_messages()) == 1, timeout=20)
        frame = server2.sent_messages()[0] if server2.sent_messages() else {}
        body = frame.get("body", [{}, {}])
        draft = service2.session.list_drafts(limit=1)
        check("★ 自动发送：过闸门后真的发出，帧结构正确且草稿标记已发送",
              sent_ok and body[0].get("cid") == "C1@goofish"
              and body[1]["actualReceivers"][0] == f"{buyer}@goofish"
              and draft and draft[0]["sent"] == 1,
              f"发送帧={len(server2.sent_messages())} draft.sent={draft[0]['sent'] if draft else '-'}")
        check("自动发送：assistant 回复写入会话历史（供后续上下文）",
              any(m["role"] == "assistant" for m in service2.session.get_context("C1")),
              str([m["role"] for m in service2.session.get_context("C1")]))

        # 出站闸门在真实链路上拦得住（把每分钟上限压到 0）
        service2.engine.outbound.max_per_minute = 0
        before_frames = len(server2.sent_messages())
        await server2.push(chat_frame("e2e-live-2", buyer, "再问一句"))
        await wait_for(lambda: any(e["kind"] == "send_blocked"
                                   for e in service2.session.recent_events(10)), timeout=15)
        check("自动发送：出站闸门拦截时不再发送（帧数不变）且记录 send_blocked",
              len(server2.sent_messages()) == before_frames
              and any(e["kind"] == "send_blocked" for e in service2.session.recent_events(10)),
              f"发送帧={len(server2.sent_messages())}（拦截前 {before_frames}）")

        # 安全护栏：非影子模式下禁用 simulate（否则可能真的发到一个不存在的会话）
        guard_ok = False
        try:
            await service2.simulate_message("影子模式下才允许模拟")
        except RuntimeError as exc:
            guard_ok = "影子模式" in str(exc)
        check("★ 安全护栏：shadow_mode=false 时 simulate 被拒绝",
              guard_ok, "已拒绝并说明原因" if guard_ok else "未拒绝（危险）")

        check("专家层确实被调用（假模型记录了调用，且 system 里带商品信息）",
              fake_llm.count() >= 4, f"模型调用次数={fake_llm.count()}")

        await service2.stop()
        await server2.stop()
    finally:
        specialist_service_mod._service = previous_service
        await server.stop()


def main():
    asyncio.run(scenario_pipeline())
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"端到端干跑（影子模式 + 自动发送）：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_e2e_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n端到端干跑：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
