# -*- coding: utf-8 -*-
"""对话编排服务（异步版）：
1) 优先 CrewAI 多智能体（未安装/异常时自动降级，多 Agent 在独立线程运行）；
2) 降级路径：LangChain 意图分类路由 + RAG/工具；
3) 所有路径带会话记忆（session_id），支持多轮上下文。
"""
import asyncio
import json
import logging
import re
import time

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI

from app.agents.tools import CREW_TOOLS_READY, query_order
from app.config import settings
from app.rag.retriever import aanswer_with_rag, kb
from app.services.resilience import ainvoke_with_retry
from app.services.semantic_cache import semantic_cache
from app.services.memory import format_history, memory
from app.services.tracing import traces

logger = logging.getLogger("airobot.chat")

INTENT_PROMPT = ChatPromptTemplate.from_messages([
    ("system",
     "你是客服系统的意图分类器，只输出 JSON，不要输出解释、前后缀或代码块标记：\n"
     '{{"intent": "knowledge|order|chat", "reason": "简短理由"}}\n\n'
     "判定规则（按顺序）：\n"
     "1. order：用户在查**自己某一个具体订单/包裹**的进度或状态，通常出现「我的订单、我的快递、这个单、订单号、物流、快递、发货、还没到、签收」等线索，"
     "常带订单号/手机号/运单号。\n"
     "2. knowledge：用户在问平台**规则、政策、流程、条件、时限、价格、售后与退款规则、账号操作办法**等通用问题；"
     "即使句子里出现「订单、退款、退货、运费、物流」这些词，只要问的是规则就归 knowledge。\n"
     "3. chat：打招呼、寒暄、感谢、闲聊、情绪表达、与客服业务无关的内容。\n\n"
     "特别注意：问「退款/退货怎么弄、运费谁承担、几天内、要多久、能不能」这类规则问题一律 knowledge；"
     "只有明确要查某一个具体订单才判 order；无法确定时优先 knowledge，不要猜 order。\n\n"
     "示例：\n"
     "用户：怎么申请退款？ => {{\"intent\": \"knowledge\", \"reason\": \"询问退款流程规则\"}}\n"
     "用户：退货运费谁承担？ => {{\"intent\": \"knowledge\", \"reason\": \"询问运费承担规则\"}}\n"
     "用户：无理由退货要几天内申请？ => {{\"intent\": \"knowledge\", \"reason\": \"询问退货时限规则\"}}\n"
     "用户：港澳台能配送吗？ => {{\"intent\": \"knowledge\", \"reason\": \"询问配送范围规则\"}}\n"
     "用户：物流一直不动怎么办？ => {{\"intent\": \"knowledge\", \"reason\": \"询问异常处理规则\"}}\n"
     "用户：帮我查一下订单 12345 到哪了 => {{\"intent\": \"order\", \"reason\": \"查询具体订单物流\"}}\n"
     "用户：我的快递怎么还没到 => {{\"intent\": \"order\", \"reason\": \"查询具体包裹进度\"}}\n"
     "用户：你好呀 => {{\"intent\": \"chat\", \"reason\": \"打招呼\"}}\n"
     "用户：谢谢你 => {{\"intent\": \"chat\", \"reason\": \"表达感谢\"}}"),
    ("human", "{message}"),
])

# order 意图的确定性兜底：模型判 order 但句子里没有任何订单线索时改判（防止把规则类问题路由到订单工具）
ORDER_CUES = ("订单", "单号", "运单", "物流", "快递", "包裹", "发货", "到货", "签收",
              "我的单", "买的东", "退货进度", "退款进度")
ORDER_NO_RE = re.compile(r"\d{6,}")  # 6 位以上连续数字，多为订单号/手机号/运单号


def has_order_cue(message: str) -> bool:
    """句子是否包含「查询具体订单」的线索。只用于把 order 改判掉，不会把别的意图升级成 order。"""
    if ORDER_NO_RE.search(message):
        return True
    return any(cue in message for cue in ORDER_CUES)


def parse_intent_json(raw: str):
    """从模型输出里抽出意图；容忍 ```json 代码块与前后多余文字。返回 intent 或 None。"""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except Exception:
        return None
    intent = str(data.get("intent", "")).strip().lower()
    return intent or None

CHAT_PROMPT = ChatPromptTemplate.from_messages([
    ("system", "你是二手交易平台的智能客服，语气友好简洁；涉及订单或平台规则时引导用户使用对应功能。"),
    MessagesPlaceholder("history"),
    ("human", "{message}"),
])

_llm_cache: dict = {}


def get_llm() -> ChatOpenAI:
    key = (settings.llm_model, settings.llm_base_url, settings.llm_api_key)
    if key not in _llm_cache:
        _llm_cache[key] = ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key or "sk-placeholder-not-configured",
            base_url=settings.llm_base_url,
            temperature=0.3,
        )
    return _llm_cache[key]


async def classify_intent(message: str) -> str:
    """意图识别：LLM 分类 + JSON 解析容错 + order 线索兜底。

    - 解析失败/取值异常一律兜底为 chat（保持原有「不中断服务」语义）；
    - 模型判 order 但句子里没有任何订单线索时改判为 knowledge（知识库为空则 chat），
      避免「怎么申请退款？」这类规则问题被路由到订单查询工具。
    """
    chain = INTENT_PROMPT | get_llm() | StrOutputParser()
    raw = await ainvoke_with_retry(chain.ainvoke, {"message": message})
    intent = parse_intent_json(raw)
    if intent is None:
        logger.warning("意图 JSON 解析失败，默认 chat: %s", raw[:80])
        return "chat"
    if intent not in ("knowledge", "order", "chat"):
        logger.warning("意图取值异常 %s，按 chat 处理: %s", intent, message[:40])
        return "chat"
    if intent == "order" and not has_order_cue(message):
        fallback = "knowledge" if kb.chunk_count > 0 else "chat"
        logger.info("意图 order 但缺少订单线索，改判为 %s: %s", fallback, message[:40])
        return fallback
    return intent


async def fallback_chat(message: str, session_id: str) -> dict:
    """内置路由：不依赖 CrewAI，逻辑与 Crew 中 Task 一致；带会话记忆。"""
    history = memory.get_messages(session_id)
    intent = await classify_intent(message)
    if intent == "order":
        return {"reply": query_order(message), "intent": intent, "sources": [], "engine": "langchain"}
    if intent == "knowledge":
        answer, sources = await aanswer_with_rag(message, get_llm(), history)
        return {"reply": answer, "intent": intent, "sources": sources, "engine": "langchain"}
    chain = CHAT_PROMPT | get_llm() | StrOutputParser()
    reply = await ainvoke_with_retry(chain.ainvoke, {"message": message, "history": history})
    return {"reply": reply, "intent": "chat", "sources": [], "engine": "langchain"}


def _maybe_cache(query_vec: list | None, result: dict, message: str = "") -> None:
    """动态数据（订单）不缓存；无上下文问题才写入语义缓存。"""
    if query_vec is not None and result.get("intent") != "order":
        semantic_cache.put(query_vec, result, message)


async def chat(message: str, session_id: str = "default") -> dict:
    """对话入口：CrewAI 优先（线程池执行），失败自动降级内置路由；成功后写入会话记忆。"""
    entry = {"message": message[:80], "session_id": session_id, "status": 200}
    t_start = time.perf_counter()
    if not settings.llm_api_key:
        traces.record({**entry, "intent": "no-key", "total_ms": 0.0})
        return {"reply": "未配置 AIROBOT_LLM_API_KEY，请复制 .env.example 为 .env 并填入密钥。",
                "intent": None, "sources": [], "engine": "langchain"}

    # 语义缓存：仅无上下文的首轮问题参与命中/写入（避免与会话记忆耦合）
    query_vec = None
    if settings.cache_enabled and settings.embedding_api_key and not memory.get_messages(session_id):
        t_cache = time.perf_counter()
        query_vec = await asyncio.to_thread(kb.embed_query, message)
        cached = semantic_cache.get(query_vec, message)
        entry["cache_lookup_ms"] = round((time.perf_counter() - t_cache) * 1000, 1)
        entry["cache_checked"] = True
        if cached is not None:
            entry.update(cache_hit=True, intent=cached.get("intent"),
                         total_ms=round((time.perf_counter() - t_start) * 1000, 1))
            traces.record(entry)
            return {**cached, "cache_hit": True}
    else:
        entry["cache_lookup_ms"] = 0.0

    t_llm = time.perf_counter()
    if settings.use_crew and CREW_TOOLS_READY:
        try:
            history_text = format_history(memory.get_messages(session_id))
            from app.agents.crew import run_crew
            reply = await asyncio.to_thread(run_crew, message, history_text)
            result = {"reply": reply, "intent": "crew", "sources": [],
                      "engine": "crew", "used_crew": True}
            entry.update(intent="crew", engine="crew",
                         llm_ms=round((time.perf_counter() - t_llm) * 1000, 1),
                         sources=0, total_ms=round((time.perf_counter() - t_start) * 1000, 1))
            _maybe_cache(query_vec, result, message)
            memory.add(session_id, message, reply)
            traces.record(entry)
            return result
        except Exception as exc:  # CrewAI 调用失败 -> 降级
            logger.warning("CrewAI 调用失败，降级到内置路由: %s", exc)

    result = await fallback_chat(message, session_id)
    entry.update(intent=result.get("intent"), engine=result.get("engine"),
                 llm_ms=round((time.perf_counter() - t_llm) * 1000, 1),
                 sources=len(result.get("sources", [])),
                 total_ms=round((time.perf_counter() - t_start) * 1000, 1))
    _maybe_cache(query_vec, result, message)
    memory.add(session_id, message, result["reply"])
    traces.record(entry)
    return result
