# -*- coding: utf-8 -*-
r"""出站策略离线自检：合规过滤、限速、去重、拟人化延迟、静默时段、与引擎发送路径的联动。

全部用注入的时钟/随机源/假 sleep，**不真等待、不连平台、不发消息**。
用法：python scripts/test_xianyu_outbound.py
"""
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

from app.agents.specialists.guard import SAFE_REPLY  # noqa: E402
from app.channels.xianyu.outbound import OutboundPolicy  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


class Clock:
    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_policy(clock, **kw):
    kw.setdefault("rng", random.Random(42))
    return OutboundPolicy(clock=clock, **kw)


def scenario_basic():
    clock = Clock()
    policy = make_policy(clock, min_interval_per_chat=0, typing_simulation=False)
    d = policy.plan("C1", "在的，成色如图～")
    check("正常文本放行且不需要等待", d.allowed and d.delay_seconds == 0 and d.text == "在的，成色如图～",
          f"allowed={d.allowed} delay={d.delay_seconds}")

    d2 = policy.plan("C1", "加我微信详聊")
    check("站外引流词被替换成安全话术（adjusted=True）",
          d2.allowed and d2.text == SAFE_REPLY and d2.adjusted,
          f"text={d2.text} adjusted={d2.adjusted}")

    for text in ("", "-", "   "):
        d3 = policy.plan("C1", text)
        check(f"空回复/占位（{text!r}）不允许发出", not d3.allowed and d3.reason == "empty_reply", d3.reason)


async def scenario_rate_limits():
    clock = Clock()
    policy = make_policy(clock, min_interval_per_chat=0, typing_simulation=False, max_per_minute=2)
    decisions = [policy.plan("C%d" % i, f"消息{i}") for i in range(3)]
    # plan 不改状态：三次都应为允许，说明 plan 是纯判定
    check("plan() 是纯判定（不改状态，可预演）", all(d.allowed for d in decisions),
          f"{[d.allowed for d in decisions]}")

    out = []
    for i in range(3):
        out.append(await policy.acquire("C%d" % i, f"消息{i}", sleeper=asyncio.sleep))
    got = out
    check("全局限速：每分钟上限 2 条，第 3 条被拦（不排队）",
          [g.allowed for g in got] == [True, True, False] and got[2].reason == "rate_limited_minute",
          f"{[(g.allowed, g.reason) for g in got]}")
    check("统计口径正确（allowed/blocked/blocked_reasons）",
          policy.snapshot()["allowed"] == 2 and policy.snapshot()["blocked"] == 1
          and policy.snapshot()["blocked_reasons"]["rate_limited_minute"] == 1,
          json.dumps(policy.snapshot()["blocked_reasons"], ensure_ascii=False))

    clock.advance(61)
    d = policy.plan("C9", "一分钟后又能发了")
    check("滑出时间窗后恢复放行", d.allowed, d.reason)


async def scenario_hour_limit():
    clock = Clock()
    policy = make_policy(clock, min_interval_per_chat=0, typing_simulation=False,
                         max_per_minute=100, max_per_hour=2)

    for i in range(2):
        await policy.acquire("C%d" % i, f"文本{i}", sleeper=asyncio.sleep)
        clock.advance(90)          # 错开 90 秒，避开每分钟限制
    clock.advance(-90)
    d = policy.plan("C3", "第三条")
    check("每小时上限生效（rate_limited_hour）", not d.allowed and d.reason == "rate_limited_hour",
          f"{d.reason} {d.detail}")


async def scenario_chat_interval():
    clock = Clock()
    policy = make_policy(clock, min_interval_per_chat=5, typing_simulation=False)

    waits = []

    async def fake_sleep(seconds):
        waits.append(round(seconds, 2))
        clock.advance(seconds)

    a = await policy.acquire("C1", "第一条", sleeper=fake_sleep)
    b = await policy.acquire("C1", "第二条", sleeper=fake_sleep)
    c = await policy.acquire("C2", "另一个会话无需等待", sleeper=fake_sleep)
    check("同会话最小间隔：第二条被延迟到 5 秒后才发（跨会话互不影响）",
          a.allowed and b.allowed and abs(b.delay_seconds - 5.0) < 0.5 and c.delay_seconds == 0
          and waits and abs(waits[0] - 5.0) < 0.5,
          f"延迟={b.delay_seconds}s 等待记录={waits} 跨会话延迟={c.delay_seconds}")

    clock2 = Clock()
    policy2 = make_policy(clock2, min_interval_per_chat=5, max_wait_seconds=1, typing_simulation=False)
    await policy2.acquire("C3", "第一条", sleeper=asyncio.sleep)
    d = policy2.plan("C3", "第二条")
    check("等待超过上限时直接拦掉（不无限排队）",
          not d.allowed and d.reason == "chat_wait_too_long", f"{d.reason} {d.detail}")


async def scenario_duplicate():
    clock = Clock()
    policy = make_policy(clock, min_interval_per_chat=0, typing_simulation=False,
                         duplicate_window_seconds=300)

    await policy.acquire("C1", "好的，今天发货", sleeper=asyncio.sleep)
    d = policy.plan("C1", "好的，今天发货")
    check("重复文本抑制：同会话同一句话在窗口内不再发（防循环刷屏）",
          not d.allowed and d.reason == "duplicate_text", f"{d.reason} {d.detail}")

    clock.advance(301)
    d2 = policy.plan("C1", "好的，今天发货")
    check("超出重复窗口后可以再发", d2.allowed, d2.reason)

    d3 = policy.plan("C2", "好的，今天发货")
    check("重复判定按会话隔离（另一个会话不受影响）", d3.allowed, d3.reason)


def scenario_typing_delay():
    clock = Clock()
    policy = make_policy(clock, min_interval_per_chat=0, typing_simulation=True,
                         typing_base_range=(0.5, 0.5), typing_per_char_range=(0.2, 0.2),
                         typing_max_delay=10.0)
    short = policy.plan("C1", "在的")           # 0.5 + 2*0.2 = 0.9
    long_one = policy.plan("C2", "字" * 200)    # 0.5 + 40 → 上限 10
    check("拟人化延迟 = 基础 + 每字，且封顶",
          abs(short.delay_seconds - 0.9) < 0.01 and long_one.delay_seconds == 10.0,
          f"短={short.delay_seconds}s 长={long_one.delay_seconds}s（上限 10s）")

    off = make_policy(Clock(), min_interval_per_chat=0, typing_simulation=False).plan("C3", "字" * 200)
    check("关闭拟人化时无延迟", off.delay_seconds == 0.0, str(off.delay_seconds))

    upstream_formula = 0.5 + 2 * 0.2
    check("与上游随机延迟公式一致（基础 0-1s + 每字 0.1-0.3s，上限 10s）",
          abs(short.delay_seconds - upstream_formula) < 0.01,
          f"期望 {upstream_formula} 实得 {short.delay_seconds}")


def scenario_quiet_hours():
    # 用本地时区构造 02:00 与 12:00
    import datetime as dt
    base = dt.datetime(2026, 9, 20, 2, 0, 0).timestamp()
    clock = Clock(base)
    policy = make_policy(clock, min_interval_per_chat=0, typing_simulation=False,
                         quiet_hours="23:00-08:00")
    d = policy.plan("C1", "深夜不打扰")
    check("静默时段（跨天 23:00-08:00）：凌晨 2 点不发",
          not d.allowed and d.reason == "quiet_hours", f"{d.reason} {d.detail}")

    clock.now = dt.datetime(2026, 9, 20, 12, 0, 0).timestamp()
    d2 = policy.plan("C1", "白天正常发")
    check("静默时段外正常放行", d2.allowed, d2.reason)

    clock.now = dt.datetime(2026, 9, 20, 9, 0, 0).timestamp()
    same_day = make_policy(clock, min_interval_per_chat=0, typing_simulation=False,
                           quiet_hours="09:00-18:00").plan("C1", "同天区间内")
    check("同天时段（09:00-18:00）也生效", not same_day.allowed and same_day.reason == "quiet_hours",
          same_day.reason)

    off = make_policy(Clock(), min_interval_per_chat=0, typing_simulation=False, quiet_hours="")
    check("留空即不启用静默时段", off.plan("C1", "随时可发").allowed)


async def scenario_engine_integration():
    """与引擎发送路径联动：被拦时不发送、草稿保持未发送、记事件。"""
    import base64
    import struct
    import json as _json

    from websockets.asyncio.server import serve
    import httpx

    from app.channels.xianyu.api import MTOP_TOKEN_URL, XianyuApi
    from app.channels.xianyu.engine import XianyuWatchEngine
    from app.channels.xianyu.protocol import parse_cookie_str
    from app.channels.xianyu.session import ChannelSessionStore

    sent_frames = []

    async def handler(ws):
        try:
            async for raw in ws:
                frame = _json.loads(raw)
                if frame.get("lwp") == "/r/MessageSend/sendByReceiverScope":
                    sent_frames.append(frame)
                elif frame.get("lwp") == "/!":
                    mid = (frame.get("headers") or {}).get("mid")
                    if mid:
                        await ws.send(_json.dumps({"code": 200, "headers": {"mid": mid}}))
        except Exception:
            pass

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    def chat_payload(text):
        """解密后的负载（parse_inbound 吃的就是这个，不是外层帧）。"""
        return {"1": {"2": "C1@goofish", "5": int(time.time() * 1000),
                      "10": {"senderUserId": "111", "reminderTitle": "买家",
                             "reminderContent": text,
                             "reminderUrl": "https://www.goofish.com/item?itemId=8&x=1"}}}

    cookies = _json.loads((ROOT / "secrets" / "xianyu_credentials.json").read_text(encoding="utf-8"))

    def api_handler(request):
        if str(request.url).startswith(MTOP_TOKEN_URL):
            return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "T"}})
        return httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {"title": "T"}}})

    api = XianyuApi(parse_cookie_str(cookies["cookies_str"]),
                    transport=httpx.MockTransport(api_handler), credential_path=None)
    db = ROOT / ".logs" / "ktest_outbound_engine.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    session = ChannelSessionStore(db)

    async def fake_gen(message, session_id, context, item_desc):
        return {"reply": "收到～", "intent": "default", "sources": [], "engine": "test"}

    # 策略：把「每分钟上限」压到 0 条 → 一定被拦
    policy = OutboundPolicy(min_interval_per_chat=0, typing_simulation=False, max_per_minute=1)
    engine = XianyuWatchEngine(api, session, "DEV-1", url=f"ws://127.0.0.1:{port}/",
                               shadow_mode=False, reply_generator=fake_gen, outbound=policy,
                               ws_kwargs={"heartbeat_interval": 30})
    await engine.start()
    for _ in range(40):
        if engine.ws.registered:
            break
        await asyncio.sleep(0.2)

    from app.channels.xianyu.ws import parse_inbound
    msg = parse_inbound(chat_payload("在吗"), "2200730824084", message_id="ob-1")[1]
    # 先占掉每分钟唯一的名额
    await policy.acquire("OTHER", "占位", sleeper=asyncio.sleep)
    result = await engine.handle_message(msg)

    check("引擎发送路径先过出站闸门：被拦时不发送、草稿保持未发送",
          result.get("sent") is False and len(sent_frames) == 0
          and session.list_drafts()[0]["sent"] == 0,
          f"sent={result.get('sent')} 发送帧={len(sent_frames)} 草稿 sent={session.list_drafts()[0]['sent']}")
    check("被拦时记 send_blocked 事件",
          any(e["kind"] == "send_blocked" for e in session.recent_events(10)),
          str([e["kind"] for e in session.recent_events(6)]))

    # 放开限速后应能真正发出
    policy.max_per_minute = 100
    policy._sent_times.clear()
    result2 = await engine.handle_message(
        parse_inbound(chat_payload("发货快吗"), "2200730824084", message_id="ob-2")[1])
    await asyncio.sleep(0.3)
    check("放开限速后正常发出（帧结构正确、草稿标记已发送）",
          result2.get("sent") is True and len(sent_frames) == 1
          and sent_frames[0]["body"][1]["actualReceivers"][0] == "111@goofish"
          and session.list_drafts(limit=1)[0]["sent"] == 1,
          f"发送帧={len(sent_frames)} sent={result2.get('sent')}")

    await engine.stop()
    await api.aclose()
    server.close()
    await server.wait_closed()


async def scenario_gradual_rollout():
    """★ 灰度发送的两道新闸门：白名单（锁影响面）与每日上限（硬天花板）。

    这是「P4 灰度自动发送」的核心保险：第一次真发送时，靠白名单把影响面锁死在一个会话里，
    靠每日上限保证即使出问题也不会滚雪球。
    """
    import datetime as dt

    async def _no_sleep(seconds):
        return None

    clock = Clock()
    locked = make_policy(clock, min_interval_per_chat=0, typing_simulation=False,
                         allowlist=["C1", "C2"])
    d1 = locked.plan("C1", "在白名单里，可以发")
    check("白名单内的会话正常放行", d1.allowed, f"{d1.reason}")
    d2 = locked.plan("C9", "不在白名单，一律不发")
    check("★ 白名单外的会话被拦（理由 not_in_allowlist，不是静默丢弃）",
          not d2.allowed and d2.reason == "not_in_allowlist", f"{d2.reason} {d2.detail}")

    empty = make_policy(clock, min_interval_per_chat=0, typing_simulation=False, allowlist=[])
    check("白名单留空＝不限制（正常值守模式）", empty.plan("C9", "谁都能回").allowed)

    # 每日上限：跨小时也要累计（_sent_times 只留一小时，容易被漏算）
    # 注意要用 acquire()：plan() 是纯判定、不记账，只有 acquire 才累加当日计数
    base = dt.datetime(2026, 9, 20, 9, 0, 0).timestamp()
    clock2 = Clock(base)
    capped = make_policy(clock2, min_interval_per_chat=0, typing_simulation=False,
                         max_per_day=2, max_per_hour=100, max_per_minute=100)
    allowed = []
    for i in range(5):
        clock2.advance(1800)                      # 每条间隔 30 分钟，绕开分钟/小时限速
        decision = await capped.acquire("C1", f"第{i}条不同内容",
                                        sleeper=_no_sleep, simulate_typing=False)
        allowed.append(decision.allowed)
    check("★ 每日上限生效且按天累计（间隔 30 分钟仍被拦住，不是只算最近一小时）",
          allowed == [True, True, False, False, False],
          f"5 次判定结果={allowed}（应前 2 次放行）")
    d3 = capped.plan("C1", "超限后的理由")
    check("超限理由为 rate_limited_day 且带当日计数",
          d3.reason == "rate_limited_day" and d3.detail.get("per_day") == 2,
          f"{d3.reason} {d3.detail}")

    # 第二天应重置
    clock2.now = dt.datetime(2026, 9, 21, 9, 0, 0).timestamp()
    d4 = capped.plan("C1", "第二天可以继续发")
    check("每日上限跨天自动重置", d4.allowed, f"{d4.reason}")

    unlimited = make_policy(Clock(), min_interval_per_chat=0, typing_simulation=False, max_per_day=0)
    check("每日上限 0＝不限制（默认不改变原有行为）",
          unlimited.max_per_day == 0 and unlimited.plan("C1", "无上限").allowed)


async def main():
    scenario_basic()
    await scenario_rate_limits()
    await scenario_hour_limit()
    await scenario_chat_interval()
    await scenario_duplicate()
    scenario_typing_delay()
    scenario_quiet_hours()
    await scenario_gradual_rollout()
    await scenario_engine_integration()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    lines = [f"出站策略离线自检：{passed}/{total} 通过", ""]
    lines += [f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
              for name, ok, detail in RESULTS]
    (ROOT / ".logs" / "xianyu_outbound_selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n出站策略离线自检：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
