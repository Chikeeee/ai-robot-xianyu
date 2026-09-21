# -*- coding: utf-8 -*-
"""验证「启动自动导入 data/」后的检索效果与来源标注。"""
import io
import json
import time

import httpx

BASE = "http://127.0.0.1:8000"
client = httpx.Client(base_url=BASE, timeout=300.0)

out = {"stats": client.get("/api/v1/stats").json(), "queries": []}

CASES = [
    ("投诉/法律风险", "遇到投诉、辱骂或者法律风险的问题，客服该怎么处理？"),
    ("转人工话术", "通用的转人工话术是什么？"),
    ("售前咨询", "产品怎么收费？支持试用吗？"),
    ("原知识库仍在", "退货运费谁承担？"),
]

for tag, q in CASES:
    t0 = time.perf_counter()
    try:
        r = client.post("/api/v1/chat", json={"message": q, "session_id": "verify-auto-" + tag})
        body = r.json()
        status = r.status_code
    except Exception as exc:  # noqa: BLE001
        body = f"{type(exc).__name__}: {exc}"
        status = "TIMEOUT/ERR"
    out["queries"].append({
        "tag": tag, "q": q, "status": status,
        "elapsed_s": round(time.perf_counter() - t0, 2), "body": body,
    })
    print(f"[{tag}] {status} {out['queries'][-1]['elapsed_s']}s")

with io.open(r"D:\dsh\ai-robot-agent\.logs\verify_auto.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("chunks =", out["stats"]["total_chunks"])
