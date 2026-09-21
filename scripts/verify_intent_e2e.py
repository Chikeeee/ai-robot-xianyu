# -*- coding: utf-8 -*-
"""意图路由补丁上线后的端到端验证（走真实 HTTP 接口，含 SSE）。"""
import io
import json
import time

import httpx

BASE = "http://127.0.0.1:8000"
client = httpx.Client(base_url=BASE, timeout=300.0)

CASES = [
    ("曾误判为 order", "怎么申请退款？", "knowledge"),
    ("曾误判为 order", "港澳台能配送吗？", "knowledge"),
    ("规则类（含物流字样）", "物流一直不动怎么办？", "knowledge"),
    ("订单查询（带单号）", "帮我查一下订单 12345 到哪了", "order"),
    ("订单查询（不带单号）", "我的快递怎么还没到", "order"),
    ("闲聊", "今天天气怎么样", "chat"),
]

out = {"stats": client.get("/api/v1/stats").json(), "http": [], "sse": None}

for tag, q, expected in CASES:
    t0 = time.perf_counter()
    r = client.post("/api/v1/chat", json={"message": q, "session_id": "intent-verify"})
    body = r.json()
    got = body.get("intent")
    out["http"].append({
        "tag": tag, "message": q, "expected": expected, "got": got,
        "correct": got == expected, "elapsed_s": round(time.perf_counter() - t0, 2),
        "reply": body.get("reply", "")[:120], "sources": body.get("sources", []),
    })
    print(f"[{'OK ' if got == expected else 'FAIL'}] {q:<24} 期望={expected:<10} 实得={got}")

# SSE 走的是同一套 classify_intent，单独验证一次意图事件
events = []
with client.stream("POST", "/api/v1/chat/stream",
                   json={"message": "无理由退货要几天内申请？", "session_id": "intent-verify-sse"}) as resp:
    for line in resp.iter_lines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except Exception:
                pass
intent_ev = next((e for e in events if e.get("type") == "intent"), None)
out["sse"] = {
    "status": resp.status_code,
    "intent_event": intent_ev,
    "expected_intent": "knowledge",
    "correct": bool(intent_ev and intent_ev.get("intent") == "knowledge"),
    "token_events": sum(1 for e in events if e.get("type") == "token"),
}
print(f"[{'OK ' if out['sse']['correct'] else 'FAIL'}] SSE 意图事件 = {intent_ev}")

ok = sum(1 for c in out["http"] if c["correct"]) + (1 if out["sse"]["correct"] else 0)
total = len(CASES) + 1
out["summary"] = f"{ok}/{total} 正确"
print(f"\n合计 {ok}/{total} 正确")

io.open(r"D:\dsh\ai-robot-agent\.logs\intent_verify.json", "w", encoding="utf-8").write(
    json.dumps(out, ensure_ascii=False, indent=2))
