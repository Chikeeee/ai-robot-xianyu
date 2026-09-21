# -*- coding: utf-8 -*-
"""确定性验证 order 线索兜底（不依赖 LLM 随机性）：把模型输出强制为 order，看兜底是否改判。"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

import app.services.chat as chat  # noqa: E402
from app.rag.retriever import kb  # noqa: E402

DATA = Path(r"D:\dsh\ai-robot-agent\data")
SUPPORTED = {".md", ".markdown", ".txt", ".pdf", ".docx"}
for p in sorted(x for x in DATA.glob("*") if x.is_file() and x.suffix.lower() in SUPPORTED):
    kb.ingest_file(p)
print("kb.chunk_count =", kb.chunk_count)


async def fake_invoke(func, payload):
    return '```json\n{"intent": "order", "reason": "强制 order 用于测试兜底"}\n```'


chat.ainvoke_with_retry = fake_invoke  # 强制模型永远判 order

CASES = [
    ("怎么申请退款？", "knowledge"),
    ("退货运费谁承担？", "knowledge"),
    ("帮我查一下订单 12345 到哪了", "order"),
    ("查下我的单号 8899001122", "order"),
    ("我的快递怎么还没到", "order"),
]


async def main():
    ok = 0
    print("\n强制模型输出 order 后的最终判定（含兜底）：")
    for message, expected in CASES:
        got = await chat.classify_intent(message)
        flag = "OK " if got == expected else "FAIL"
        ok += 1 if got == expected else 0
        print(f"  [{flag}] {message:<26} 期望={expected:<10} 实得={got:<10} 有订单线索={chat.has_order_cue(message)}")
    print(f"\n兜底用例通过 {ok}/{len(CASES)}")

    print("\nhas_order_cue 单独检查：")
    for m in ["怎么申请退款？", "无理由退货要几天内申请？", "今天天气怎么样",
              "我的订单什么时候能到", "订单 202608090001 发货了吗", "物流一直不动怎么办？"]:
        print(f"  {m:<24} -> {chat.has_order_cue(m)}")


asyncio.run(main())
