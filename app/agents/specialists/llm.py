# -*- coding: utf-8 -*-
r"""专家层的大模型调用封装。

两个移植过来的关键点（上游 `XianyuAgent.py:8-32`）：

1. `enable_search`：只有通义千问（百炼）支持，其他网关会 **400**——所以默认按 base_url 判断，
   可用 `ENABLE_SEARCH` 覆盖。
2. `thinking={"type":"disabled"}`：推理模型（deepseek-v4-pro/flash）必须关思考，否则 500 token 预算
   会被推理吃光、`content` 返回空字符串（表现为「回复是空的」）——默认按模型名判断，可用 `DISABLE_THINKING` 覆盖。

`llm_factory` 可注入，便于离线自测（不真调模型）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.config import settings as app_settings

logger = logging.getLogger("airobot.specialists.llm")

DEFAULT_MAX_TOKENS = 500
DEFAULT_TOP_P = 0.8


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def build_extra_body(base_url: Optional[str] = None, model: Optional[str] = None) -> Dict[str, Any]:
    """按服务商/模型拼装额外请求体字段（移植上游 `build_extra_body`）。"""
    base_url = (base_url if base_url is not None else app_settings.llm_base_url or "").lower()
    model = (model if model is not None else app_settings.llm_model or "").lower()
    extra: Dict[str, Any] = {}
    if _flag("ENABLE_SEARCH", "dashscope" in base_url):
        extra["enable_search"] = True
    if _flag("DISABLE_THINKING", "deepseek" in model):
        extra["thinking"] = {"type": "disabled"}
    return extra


def build_chat_llm(*, temperature: float, max_tokens: int = DEFAULT_MAX_TOKENS,
                   top_p: float = DEFAULT_TOP_P, extra_body: Optional[Dict[str, Any]] = None):
    """构造 ChatOpenAI（复用 AI-Robot 的模型/网关配置）。"""
    from langchain_openai import ChatOpenAI

    body = build_extra_body() if extra_body is None else extra_body
    return ChatOpenAI(
        model=app_settings.llm_model,
        api_key=app_settings.llm_api_key or "sk-placeholder-not-configured",
        base_url=app_settings.llm_base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        extra_body=body or None,
    )


async def call_llm(messages: List[Dict[str, str]], *, temperature: float,
                   max_tokens: int = DEFAULT_MAX_TOKENS, top_p: float = DEFAULT_TOP_P,
                   llm_factory: Optional[Callable[..., Any]] = None) -> str:
    """调用专家模型，返回文本内容。空内容会记 warning（多半是没关思考）。"""
    factory = llm_factory or build_chat_llm
    llm = factory(temperature=temperature, max_tokens=max_tokens, top_p=top_p)
    response = await llm.ainvoke(messages)
    content = getattr(response, "content", None)
    if content is None:
        content = str(response)
    if not str(content).strip():
        logger.warning("专家模型返回空内容（若用推理模型请确认 DISABLE_THINKING=true）")
    return str(content).strip()
