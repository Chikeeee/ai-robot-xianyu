# -*- coding: utf-8 -*-
r"""闲鱼通道的「回复生成器」适配层：把 AI-Robot 的两种编排接进值守引擎。

- `specialists`（默认）：`app/agents/specialists` —— 从 XianyuAutoAgent 移植的多专家路由
  （议价/技术/客服）+ 阶梯议价 + RAG 资料注入 + 合规过滤；
- `airobot`：AI-Robot 原生的 `app.services.chat` —— 意图路由（knowledge/order/chat）+ RAG + 语义缓存。

两者签名一致（`message, session_id, context, item_desc -> dict`），可在 `.env` 里用
`XIANYU_REPLY_ENGINE=specialists|airobot` 切换。注意：`airobot` 的 `chat()` 有按 session_id 的
**语义缓存**，在闲鱼场景会把相似问题原样复用答案（典型的机器人特征），所以默认不选它。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("airobot.xianyu.reply")

ReplyGenerator = Callable[[str, str, List[Dict[str, str]], str], Awaitable[Dict[str, Any]]]


async def specialist_reply_generator(message: str, session_id: str,
                                     context: List[Dict[str, str]], item_desc: str) -> Dict[str, Any]:
    """默认生成器：闲鱼专家层（保留上游的专家路由与阶梯议价）。"""
    from app.agents.specialists import get_specialist_service

    service = get_specialist_service()
    return await service.generate(message, item_desc=item_desc, context=context, session_id=session_id)


async def airobot_reply_generator(message: str, session_id: str,
                                  context: List[Dict[str, str]], item_desc: str) -> Dict[str, Any]:
    """备选生成器：AI-Robot 原生编排（意图路由 + RAG + 语义缓存 + traces）。

    `context`/`item_desc` 不注入 `chat()`（它用自己的会话记忆）；需要商品上下文与议价轮次时用专家层。
    """
    from app.services.chat import chat  # 延迟导入：避免通道模块与业务模块循环依赖

    result = await chat(message, session_id=session_id)
    return {
        "reply": result.get("reply", ""),
        "intent": result.get("intent"),
        "sources": result.get("sources", []),
        "engine": result.get("engine", "langchain"),
    }


GENERATORS: Dict[str, ReplyGenerator] = {
    "specialists": specialist_reply_generator,
    "airobot": airobot_reply_generator,
}


def get_generator(name: Optional[str] = None) -> ReplyGenerator:
    """按名字取生成器，未知名字回落 specialists 并记 warning。"""
    key = (name or "specialists").strip().lower()
    if key not in GENERATORS:
        logger.warning("未知的回复引擎 %r，回落 specialists（可选: %s）", name, ",".join(GENERATORS))
        key = "specialists"
    return GENERATORS[key]
