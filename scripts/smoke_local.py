# -*- coding: utf-8 -*-
"""部署验收脚本：健康检查 / 指标 / 非流式对话 / SSE 流式 / 控制台。"""
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
client = httpx.Client(base_url=BASE, timeout=180.0)

out = {}


def show(title, value):
    print(f"\n=== {title} ===")
    print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))


# 1) health
r = client.get("/health")
show("GET /health", {"status_code": r.status_code, "body": r.json()})

# 2) stats
r = client.get("/api/v1/stats")
show("GET /api/v1/stats", {"status_code": r.status_code, "body": r.json()})

# 3) 非流式对话：知识问答
t0 = time.perf_counter()
r = client.post("/api/v1/chat", json={"message": "退货运费谁承担？", "session_id": "smoke-knowledge"})
show("POST /api/v1/chat (knowledge)", {
    "status_code": r.status_code,
    "elapsed_s": round(time.perf_counter() - t0, 2),
    "body": r.json(),
})

# 4) 同一会话追问（验证多轮记忆）
r = client.post("/api/v1/chat", json={"message": "那买家自己原因的退货呢？", "session_id": "smoke-knowledge"})
show("POST /api/v1/chat (follow-up, memory)", {"status_code": r.status_code, "body": r.json()})

# 5) 订单意图
r = client.post("/api/v1/chat", json={"message": "帮我查一下订单 12345 到哪了", "session_id": "smoke-order"})
show("POST /api/v1/chat (order)", {"status_code": r.status_code, "body": r.json()})

# 6) 闲聊
r = client.post("/api/v1/chat", json={"message": "你好呀", "session_id": "smoke-chat"})
show("POST /api/v1/chat (chat)", {"status_code": r.status_code, "body": r.json()})

# 7) 语义缓存命中（同问题、新会话 = 首轮无上下文）
t0 = time.perf_counter()
r = client.post("/api/v1/chat", json={"message": "退货运费谁承担？", "session_id": "smoke-cache"})
show("POST /api/v1/chat (cache probe)", {
    "status_code": r.status_code,
    "elapsed_s": round(time.perf_counter() - t0, 2),
    "body": r.json(),
})

# 8) SSE 流式
events = []
t0 = time.perf_counter()
first_token_s = None
with client.stream("POST", "/api/v1/chat/stream",
                   json={"message": "怎么申请退款？", "session_id": "smoke-stream"}) as resp:
    status = resp.status_code
    for line in resp.iter_lines():
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        events.append(ev)
        if ev.get("type") == "token" and first_token_s is None:
            first_token_s = round(time.perf_counter() - t0, 2)
show("POST /api/v1/chat/stream (SSE)", {
    "status_code": status,
    "total_s": round(time.perf_counter() - t0, 2),
    "first_token_s": first_token_s,
    "event_types": [e.get("type") for e in events],
    "stages": [e.get("stage") for e in events if e.get("type") == "stage"],
    "done": next((e for e in events if e.get("type") == "done"), None),
    "answer": "".join(e.get("content", "") for e in events if e.get("type") == "token")[:200],
})

# 9) traces
r = client.get("/api/v1/traces?limit=20")
body = r.json()
show("GET /api/v1/traces", {
    "status_code": r.status_code,
    "count": len(body.get("traces", body if isinstance(body, list) else [])),
    "aggregate": body.get("stats") or body.get("aggregate"),
})

# 10) dashboard
r = client.get("/dashboard")
show("GET /dashboard", {"status_code": r.status_code, "bytes": len(r.content),
                        "has_title": "控制台" in r.text or "dashboard" in r.text.lower()})

# 11) 限流参数与最终 stats
r = client.get("/api/v1/stats")
show("GET /api/v1/stats (final)", r.json())
print("\nALL CHECKS EXECUTED")
