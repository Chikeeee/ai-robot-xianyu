# -*- coding: utf-8 -*-
r"""专家层服务：路由 → 专家 → （price/tech/default 注入 RAG 资料）→ 合规过滤 → 记录 traces。

这是「XianyuAutoAgent 特色功能」在 AI-Robot 上的落点：
上游的四个专家 + 三级路由 + 阶梯议价保留，**补上 AI-Robot 的 RAG 与可观测**：

- `price` / `tech` / `default` 专家的回答会把知识库检索到的资料一起喂进去（上游把这段注释掉了）；
  `price` 尤其需要：议价规则（底价、单次让价上限、先让买家出价）本来就写在 `data/卖家规则.md` 里，
  不注入的话模型只能凭先验编价格——实测买家问「几块钱」时回复既没来源也不谈价。
- 每次生成都往 AI-Robot 的 `traces` 记一条（`engine=xianyu-specialist`），
  所以现有控制台的意图分布/耗时统计**能直接看到闲鱼流量**，不需要改核心意图枚举。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

from app.agents.specialists.agents import SpecialistAgent, build_agents
from app.agents.specialists.guard import filter_outbound
from app.agents.specialists.router import SpecialistRouter

logger = logging.getLogger("airobot.specialists.service")

RAG_INTENTS = ("price", "tech", "default")
BARGAIN_RE = re.compile(r"议价次数[:：]\s*(\d+)")


def extract_bargain_count(context: Any) -> int:
    """从会话上下文里取议价次数（移植上游 `XianyuAgent.py:145-165` 的语义）。

    上游从 system 消息里正则提取；这里同时支持 list[dict] 与已拼好的字符串。
    """
    if isinstance(context, str):
        match = BARGAIN_RE.search(context)
        return int(match.group(1)) if match else 0
    for msg in (context or []):
        try:
            if msg.get("role") == "system" and "议价次数" in (msg.get("content") or ""):
                match = BARGAIN_RE.search(msg["content"])
                if match:
                    return int(match.group(1))
        except Exception:
            continue
    return 0


def format_context(context: Any) -> str:
    """把上下文拼成提示词里的一段文本（移植上游 `format_history`）。"""
    if isinstance(context, str):
        return context
    lines = []
    for msg in (context or []):
        try:
            if msg.get("role") in ("user", "assistant"):
                lines.append(f"{msg['role']}: {msg['content']}")
        except Exception:
            continue
    return "\n".join(lines)


class SpecialistService:
    """闲鱼专家服务（可在无网络环境下用假 LLM 完整自测）。"""

    def __init__(self, *, llm_factory: Optional[Callable[..., Any]] = None,
                 agents: Optional[Dict[str, SpecialistAgent]] = None,
                 router: Optional[SpecialistRouter] = None,
                 rag_search: Optional[Callable[[str, int], List[Any]]] = None,
                 rag_top_k: int = 3, enable_rag: bool = True,
                 record_traces: bool = True) -> None:
        self.agents = agents or build_agents(llm_factory=llm_factory)
        self.router = router or SpecialistRouter()
        self.rag_search = rag_search
        self.rag_top_k = rag_top_k
        self.enable_rag = enable_rag
        self.record_traces = record_traces

    # ------------------------------------------------------------------ #
    def _search(self, query: str) -> tuple[str, List[str]]:
        """检索知识库资料，返回 (拼接文本, 来源列表)。失败不影响主链路。"""
        if not self.enable_rag:
            return "", []
        search = self.rag_search
        try:
            if search is None:
                from app.rag.retriever import kb
                search = kb.search
            docs = search(query, self.rag_top_k) or []
        except Exception as exc:
            logger.warning("知识库检索失败（继续无资料作答）: %s", exc)
            return "", []
        texts, sources = [], []
        for doc in docs:
            content = getattr(doc, "page_content", None) or (doc.get("content") if isinstance(doc, dict) else "")
            meta = getattr(doc, "metadata", None) or (doc.get("metadata") if isinstance(doc, dict) else {}) or {}
            if content:
                texts.append(content)
            if meta:
                sources.append(f"{meta.get('title', '')}#{meta.get('chunk', 0)}")
        return "\n\n".join(texts), sources

    # ------------------------------------------------------------------ #
    async def generate(self, message: str, *, item_desc: str = "", context: Any = None,
                       session_id: Optional[str] = None, bargain_count: Optional[int] = None) -> Dict[str, Any]:
        started = time.perf_counter()
        context_text = format_context(context)
        if bargain_count is None:
            bargain_count = extract_bargain_count(context)

        intent = await self.router.route(message, item_desc, context_text)
        if intent == "no_reply":
            self._trace(message, session_id, intent, started, sources=0)
            return {"reply": "-", "intent": "no_reply", "specialist": "no_reply",
                    "sources": [], "engine": "specialists"}

        agent = self.agents.get(intent) or self.agents["default"]
        extra_context, sources = ("", [])
        if intent in RAG_INTENTS:
            extra_context, sources = await asyncio.to_thread(self._search, message)

        reply = await agent.reply(message, item_desc=item_desc, context=context_text,
                                 extra_context=extra_context, bargain_count=bargain_count)
        self._trace(message, session_id, intent, started, sources=len(sources), specialist=agent.name)
        return {"reply": reply, "intent": intent, "specialist": agent.name,
                "sources": sources, "engine": "specialists",
                "bargain_count": bargain_count, "temperature": agent.temperature_for(bargain_count)}

    # ------------------------------------------------------------------ #
    def _trace(self, message: str, session_id: Optional[str], intent: str,
               started: float, *, sources: int, specialist: Optional[str] = None) -> None:
        if not self.record_traces:
            return
        try:
            from app.services.tracing import traces
            traces.record({
                "message": (message or "")[:80],
                "session_id": session_id or "xianyu",
                "status": 200,
                "intent": intent,
                "engine": "xianyu-specialist" if specialist is None else f"xianyu-{specialist}",
                "cache_checked": False,
                "sources": sources,
                "total_ms": round((time.perf_counter() - started) * 1000, 1),
            })
        except Exception:
            logger.debug("记录 traces 失败", exc_info=True)


_service: Optional[SpecialistService] = None


def get_specialist_service() -> SpecialistService:
    """进程内单例（提示词与 agents 只构建一次）。"""
    global _service
    if _service is None:
        _service = SpecialistService()
        logger.info("闲鱼专家服务已初始化（专家: %s）", ",".join(_service.agents))
    return _service


def reset_specialist_service() -> None:
    """测试用：清掉单例（换提示词/换假 LLM 后重新构建）。"""
    global _service
    _service = None
