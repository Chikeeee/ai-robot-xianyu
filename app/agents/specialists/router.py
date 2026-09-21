# -*- coding: utf-8 -*-
r"""专家路由（移植上游 `XianyuAgent.py:175-225` 的三级路由）。

顺序与上游一致：**技术优先** → 价格 → LLM 兜底。差别是加了两条确定性兜底：

- 命中注入/身份询问/无关话题（`guard.is_no_reply`）直接 `no_reply`，不花一次模型调用；
- LLM 兜底返回了非法类别时归 `default`（上游会把非法值当 default 用，这里显式记录）。
"""
from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, Dict, Optional

from app.agents.specialists.guard import is_no_reply

logger = logging.getLogger("airobot.specialists.router")

VALID_INTENTS = ("price", "tech", "default", "no_reply")

# 上游 XianyuAgent.py:179-190 的规则表（原样移植，含"技术优先"的注释语义）
RULES: Dict[str, Dict[str, list]] = {
    "tech": {
        "keywords": ["参数", "规格", "型号", "连接", "对比"],
        "patterns": [r"和.+比"],
    },
    "price": {
        "keywords": ["便宜", "价", "砍价", "少点"],
        "patterns": [r"\d+元", r"能少\d+"],
    },
}

_CLEAN_RE = re.compile(r"[^\w\u4e00-\u9fa5]")


def clean_text(message: str) -> str:
    """去掉标点/表情再匹配规则（上游 `XianyuAgent.py:195` 同款处理）。"""
    return _CLEAN_RE.sub("", message or "")


def rule_route(message: str) -> Optional[str]:
    """纯规则判定：命中返回 intent，未命中返回 None（交由 LLM 兜底）。"""
    text = clean_text(message)
    if not text:
        return None

    # 1) 技术类关键词（优先）
    if any(kw in text for kw in RULES["tech"]["keywords"]):
        return "tech"
    # 2) 技术类正则
    for pattern in RULES["tech"]["patterns"]:
        if re.search(pattern, text):
            return "tech"
    # 3) 价格类
    if any(kw in text for kw in RULES["price"]["keywords"]):
        return "price"
    for pattern in RULES["price"]["patterns"]:
        if re.search(pattern, text):
            return "price"
    return None


class SpecialistRouter:
    """规则优先 + LLM 兜底的路由器。`classify` 可注入（默认走分类专家提示词）。"""

    def __init__(self, classify: Optional[Callable[[str, str, str], Awaitable[str]]] = None) -> None:
        self._classify = classify

    async def route(self, message: str, item_desc: str = "", context: str = "") -> str:
        if is_no_reply(message):
            logger.info("命中确定性 no_reply 规则，跳过模型调用: %s", (message or "")[:20])
            return "no_reply"

        intent = rule_route(message)
        if intent:
            return intent

        if self._classify is None:
            from app.agents.specialists.agents import classify_intent as default_classify
            self._classify = default_classify

        try:
            raw = await self._classify(message, item_desc, context)
        except Exception as exc:
            logger.warning("LLM 分类失败，回落 default: %s", exc)
            return "default"

        intent = (raw or "").strip().lower()
        if intent not in VALID_INTENTS:
            logger.warning("分类返回非法类别 %r，按 default 处理", intent)
            return "default"
        return intent
