# -*- coding: utf-8 -*-
"""意图路由 A/B 评测：旧实现（复刻上游提示词）vs 新实现（few-shot + order 线索兜底）。

用法：python scripts/eval_intent_router.py [每种变体每条消息的重复次数，默认 2]
结果写入 .logs/intent_eval.json 与 .logs/intent_eval.txt
"""
import asyncio
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1,api.deepseek.com")

from langchain_core.output_parsers import StrOutputParser  # noqa: E402
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402

from app.services.chat import (  # noqa: E402
    classify_intent as new_classify_intent,
    get_llm,
    has_order_cue,
)

# ---- 旧实现（严格复刻上游 app/services/chat.py 的分类器） ----
OLD_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "你是意图分类器，只输出 JSON：{{\"intent\": \"knowledge|order|chat\", \"reason\": \"简短理由\"}}"),
    ("human", "{message}"),
])

CASES = [
    ("怎么申请退款？", "knowledge"),
    ("退货运费谁承担？", "knowledge"),
    ("无理由退货要几天内申请？", "knowledge"),
    ("港澳台能配送吗？", "knowledge"),
    ("物流一直不动怎么办？", "knowledge"),
    ("你们支持开发票吗？", "knowledge"),
    ("会员积分怎么获得？", "knowledge"),
    ("商品和描述不符能退吗？", "knowledge"),
    ("帮我查一下订单 12345 到哪了", "order"),
    ("我的快递怎么还没到", "order"),
    ("订单 202608090001 发货了吗", "order"),
    ("查下我的单号 8899001122", "order"),
    ("我的订单什么时候能到", "order"),
    ("你好呀", "chat"),
    ("谢谢你", "chat"),
    ("今天天气怎么样", "chat"),
]


async def old_classify(message: str) -> str:
    chain = OLD_PROMPT | get_llm() | StrOutputParser()
    raw = await chain.ainvoke({"message": message})
    try:
        data = json.loads(raw.strip().strip("`"))
        return data.get("intent", "chat")
    except Exception:
        return "chat:解析失败"


async def main():
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    rows = []
    for message, expected in CASES:
        old_ints, new_ints = [], []
        for _ in range(repeats):
            old_ints.append(await old_classify(message))
            new_ints.append(await new_classify_intent(message))
        rows.append({
            "message": message,
            "expected": expected,
            "has_order_cue": has_order_cue(message),
            "old": old_ints,
            "new": new_ints,
            "old_ok": sum(1 for i in old_ints if i == expected),
            "new_ok": sum(1 for i in new_ints if i == expected),
        })
        print(f"{message[:26]:<28} 期望={expected:<9} 旧={old_ints} 新={new_ints}")

    total = len(CASES) * repeats
    old_acc = sum(r["old_ok"] for r in rows) / total
    new_acc = sum(r["new_ok"] for r in rows) / total

    lines = [f"重复次数 = {repeats}，用例数 = {len(CASES)}，总判定次数 = {total}", ""]
    lines.append(f"旧实现准确率 = {old_acc:.1%}（{sum(r['old_ok'] for r in rows)}/{total}）")
    lines.append(f"新实现准确率 = {new_acc:.1%}（{sum(r['new_ok'] for r in rows)}/{total}）")
    lines.append("")
    lines.append(f"{'用例':<30}{'期望':<11}{'旧判定':<26}{'新判定':<26}")
    for r in rows:
        lines.append(f"{r['message'][:28]:<30}{r['expected']:<11}{str(r['old']):<26}{str(r['new']):<26}")
    lines.append("")
    lines.append("旧实现判错的用例：")
    for r in rows:
        if r["old_ok"] < repeats:
            lines.append(f"  - {r['message']} 期望 {r['expected']}，实得 {r['old']}")
    lines.append("新实现仍判错的用例：")
    bad = [r for r in rows if r["new_ok"] < repeats]
    if not bad:
        lines.append("  （无）")
    for r in bad:
        lines.append(f"  - {r['message']} 期望 {r['expected']}，实得 {r['new']}")

    text = "\n".join(lines)
    io.open(r"D:\dsh\ai-robot-agent\.logs\intent_eval.txt", "w", encoding="utf-8").write(text)
    io.open(r"D:\dsh\ai-robot-agent\.logs\intent_eval.json", "w", encoding="utf-8").write(
        json.dumps({"repeats": repeats, "old_accuracy": old_acc, "new_accuracy": new_acc, "rows": rows},
                   ensure_ascii=False, indent=2))
    print(f"\n旧准确率 {old_acc:.1%} -> 新准确率 {new_acc:.1%}")


if __name__ == "__main__":
    asyncio.run(main())
