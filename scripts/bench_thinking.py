# -*- coding: utf-8 -*-
"""对比 DeepSeek 推理开关对延迟与输出格式的影响（用于决定是否关闭 thinking）。"""
import os
import time

import httpx

KEY = os.environ["K"]
URL = "https://api.deepseek.com/v1/chat/completions"

SYS = '你是意图分类器，只输出 JSON：{"intent": "knowledge|order|chat", "reason": "简短理由"}'
USER = "退货运费谁承担？"

for model in ("deepseek-flash", "deepseek-v4-pro"):
    for label, extra in (("默认(带推理)", {}), ("关闭thinking", {"thinking": {"type": "disabled"}})):
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": USER}],
            "temperature": 0.3,
        }
        payload.update(extra)
        t0 = time.perf_counter()
        try:
            r = httpx.post(URL, json=payload, headers={"Authorization": "Bearer " + KEY}, timeout=180)
            dt = time.perf_counter() - t0
            d = r.json()
            msg = d["choices"][0]["message"]
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            print(f"[{model}] {label}: http={r.status_code} {dt:.2f}s "
                  f"reasoning_tokens={d.get('usage', {}).get('completion_tokens_details', {}).get('reasoning_tokens')} "
                  f"content={content[:90]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{model}] {label}: ERR {type(exc).__name__} {exc}")
