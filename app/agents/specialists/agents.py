# -*- coding: utf-8 -*-
r"""四个闲鱼专家（移植上游 `XianyuAgent.py:228-326`）。

| 专家 | 上游 | 差异 |
|---|---|---|
| `classify` | `ClassifyAgent` | 只输出类别名，供路由兜底 |
| `price` | `PriceAgent` | **动态温度** `min(0.3 + n*0.15, 0.9)` 原样保留；额外把「▲当前议价轮次：N」写进提示词 |
| `tech` | `TechAgent` | 上游把知识库那段注释掉了；这里**接上 AI-Robot 的 RAG 资料**（真正复用） |
| `default` | `DefaultAgent` | 温度 0.7，与上游一致 |

统一走 `call_llm`（含 `enable_search` / 关闭思考的处理），并支持注入假 LLM 做离线自测。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from app.agents.specialists.guard import filter_outbound
from app.agents.specialists.llm import DEFAULT_MAX_TOKENS, DEFAULT_TOP_P, call_llm
from app.agents.specialists.prompts import load_all

logger = logging.getLogger("airobot.specialists.agents")


class SpecialistAgent:
    """专家基类：拼 system（商品信息 + 对话历史 + 专家提示词）→ 调模型 → 合规过滤。"""

    name = "base"
    temperature = 0.4

    def __init__(self, system_prompt: str, *, llm_factory: Optional[Callable[..., Any]] = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS, top_p: float = DEFAULT_TOP_P) -> None:
        self.system_prompt = system_prompt
        self.llm_factory = llm_factory
        self.max_tokens = max_tokens
        self.top_p = top_p

    def temperature_for(self, bargain_count: int = 0) -> float:
        return self.temperature

    def build_messages(self, message: str, item_desc: str = "", context: str = "",
                       extra_context: str = "", bargain_count: int = 0) -> List[Dict[str, str]]:
        system = (f"【商品信息】{item_desc}\n"
                  f"【你与客户的对话历史】{context}\n"
                  f"{self.system_prompt}")
        if extra_context:
            # 强制引用资料：提示词文件是从上游原样搬来的，里面并没有"只能依据资料回答"的约束，
            # 不改提示词文件的前提下，在拼装层补一条硬要求，避免模型用先验编造参数或政策。
            system += (f"\n【知识库资料】\n{extra_context}\n"
                       "【资料使用要求】只能依据【商品信息】与【知识库资料】作答；"
                       "资料里没有的（含标着【未配置】的内容）一律说不确定并引导转人工，禁止编造参数、政策、时效或价格。")
        return [{"role": "system", "content": system}, {"role": "user", "content": message}]

    async def reply(self, message: str, item_desc: str = "", context: str = "",
                    extra_context: str = "", bargain_count: int = 0) -> str:
        messages = self.build_messages(message, item_desc, context, extra_context, bargain_count)
        content = await call_llm(messages, temperature=self.temperature_for(bargain_count),
                                 max_tokens=self.max_tokens, top_p=self.top_p,
                                 llm_factory=self.llm_factory)
        return filter_outbound(content)


class ClassifyAgent(SpecialistAgent):
    name = "classify"
    temperature = 0.0

    async def reply(self, message: str, item_desc: str = "", context: str = "",
                    extra_context: str = "", bargain_count: int = 0) -> str:
        # 分类不做合规过滤（它输出的是类别名）
        messages = self.build_messages(message, item_desc, context)
        return await call_llm(messages, temperature=self.temperature, max_tokens=16,
                              top_p=self.top_p, llm_factory=self.llm_factory)


class PriceAgent(SpecialistAgent):
    """议价专家：温度随议价轮次上升（越高越"松口"），轮次写进提示词。"""

    name = "price"
    temperature = 0.3
    MAX_TEMPERATURE = 0.9
    STEP = 0.15

    def temperature_for(self, bargain_count: int = 0) -> float:
        return min(self.temperature + max(0, bargain_count) * self.STEP, self.MAX_TEMPERATURE)

    def build_messages(self, message: str, item_desc: str = "", context: str = "",
                       extra_context: str = "", bargain_count: int = 0) -> List[Dict[str, str]]:
        messages = super().build_messages(message, item_desc, context, extra_context, bargain_count)
        messages[0]["content"] += f"\n▲当前议价轮次：{max(0, bargain_count)}"
        return messages


class TechAgent(SpecialistAgent):
    """技术/参数专家：优先依据注入的 RAG 资料，资料没有就如实说不确定。"""

    name = "tech"
    temperature = 0.4


class DefaultAgent(SpecialistAgent):
    name = "default"
    temperature = 0.7


def build_agents(*, llm_factory: Optional[Callable[..., Any]] = None) -> Dict[str, SpecialistAgent]:
    prompts = load_all()
    return {
        "classify": ClassifyAgent(prompts["classify"], llm_factory=llm_factory),
        "price": PriceAgent(prompts["price"], llm_factory=llm_factory),
        "tech": TechAgent(prompts["tech"], llm_factory=llm_factory),
        "default": DefaultAgent(prompts["default"], llm_factory=llm_factory),
    }


async def classify_intent(message: str, item_desc: str = "", context: str = "") -> str:
    """默认分类器：用分类专家提示词问模型要类别名。"""
    agent = ClassifyAgent(load_all()["classify"])
    raw = await agent.reply(message, item_desc, context)
    return (raw or "").strip().lower()
