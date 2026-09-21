# -*- coding: utf-8 -*-
r"""闲鱼值守引擎：把入站消息接到 AI-Robot 的编排上，并决定「回不回、发不发」。

职责边界（这是本移植最关键的一层）：

- **收帧**由 `ws.py` 负责（纯协议，不阻塞）；
- **状态**由 `session.py` 负责（SQLite：历史/接管/议价/商品缓存/幂等/草稿/事件）；
- **回复内容**由 AI-Robot 的 `app.services.chat`（意图路由 + RAG + 语义缓存 + traces）负责，
  通过可注入的 `reply_generator` 调用，且**放在线程池里跑**——上游 `main.py:518` 是把同步 LLM 调用
  直接放在收帧协程里，会把心跳一起卡住；
- **发不发**由本层决定：默认 `shadow_mode=True`，生成的回复**只落 `drafts` 表不发送**（P2 影子模式），
  人工比对满意后再由 P4 显式打开发送。

人工接管沿用上游语义（`main.py:53-56/270-308/464-490`）：卖家在会话里发接管词（默认「。」）切换，
接管期间买家消息只记录不回复；卖家自己发的消息记为 assistant 存进历史。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.agents.specialists.guard import SAFE_REPLY, filter_outbound  # noqa: F401  合规过滤（单一来源）
from app.channels.xianyu.alerts import AlertManager
from app.channels.xianyu.api import XianyuApi, XianyuApiError, XianyuAuthError, XianyuRiskControlError
from app.channels.xianyu.outbound import OutboundPolicy
from app.channels.xianyu.protocol import WS_URL
from app.channels.xianyu.reply import GENERATORS, get_generator
from app.channels.xianyu.session import DEFAULT_MANUAL_TIMEOUT, ChannelSessionStore
from app.channels.xianyu.ws import InboundMessage, XianyuWSClient

logger = logging.getLogger("airobot.xianyu.engine")

# 生成器签名：(买家消息, 会话 id, 历史, 商品描述) -> {"reply": str, "intent": str, "sources": [...], ...}
ReplyGenerator = Callable[[str, str, List[Dict[str, str]], str], Awaitable[Dict[str, Any]]]


def _message_key(message_id: str) -> str:
    """消息级幂等键（`seen_frames.mid` 复用同一张表，加前缀避免与帧 mid 混淆）。"""
    return f"msg:{message_id}"


def build_item_description(item_info: Dict[str, Any]) -> str:
    """把商品信息压成一段紧凑 JSON（移植上游 main.py:320-361 的 SKU/价格区间逻辑）。"""
    def _price(value: Any) -> float:
        try:
            return round(float(value) / 100, 2)
        except (TypeError, ValueError):
            return 0.0

    clean_skus = []
    for sku in (item_info.get("skuList") or []):
        specs = [p.get("valueText") for p in (sku.get("propertyList") or []) if p.get("valueText")]
        clean_skus.append({
            "spec": " ".join(specs) if specs else "默认规格",
            "price": _price(sku.get("price", 0)),
            "stock": sku.get("quantity", 0),
        })

    prices = [s["price"] for s in clean_skus if s["price"] > 0]
    if prices:
        low, high = min(prices), max(prices)
        price_display = f"¥{low}" if low == high else f"¥{low} - ¥{high}"
    else:
        try:
            price_display = f"¥{round(float(item_info.get('soldPrice', 0) or 0), 2)}"
        except (TypeError, ValueError):
            price_display = "¥0"

    summary = {
        "title": item_info.get("title", ""),
        "desc": item_info.get("desc", ""),
        "price_range": price_display,
        "total_stock": item_info.get("quantity", 0),
        "sku_details": clean_skus,
    }
    return json.dumps(summary, ensure_ascii=False)


async def airobot_reply_generator(message: str, session_id: str,
                                  context: List[Dict[str, str]], item_desc: str) -> Dict[str, Any]:
    """兼容旧引用：实际实现在 `app.channels.xianyu.reply`。"""
    from app.channels.xianyu.reply import airobot_reply_generator as _impl
    return await _impl(message, session_id, context, item_desc)


class XianyuWatchEngine:
    """值守引擎：连接 + 状态 + 编排 + 发送策略。"""

    def __init__(
        self,
        api: XianyuApi,
        session: ChannelSessionStore,
        device_id: str,
        *,
        url: str = WS_URL,
        shadow_mode: bool = True,
        reply_generator: Optional[ReplyGenerator] = None,
        reply_engine: Optional[str] = None,
        toggle_keywords: Optional[str] = None,
        manual_timeout: int = DEFAULT_MANUAL_TIMEOUT,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        alerts: Optional["AlertManager"] = None,
        outbound: Optional[OutboundPolicy] = None,
        ws_client: Optional[XianyuWSClient] = None,
        ws_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.api = api
        self.session = session
        self.device_id = device_id
        self.shadow_mode = shadow_mode
        self.reply_engine = reply_engine or os.getenv("XIANYU_REPLY_ENGINE", "specialists")
        self.reply_generator = reply_generator or get_generator(self.reply_engine)
        self.toggle_keywords = toggle_keywords if toggle_keywords is not None else os.getenv(
            "XIANYU_TOGGLE_KEYWORDS", "。")
        self.manual_timeout = manual_timeout
        self._on_event = on_event
        self.alerts: Optional[AlertManager] = alerts
        self.outbound: OutboundPolicy = outbound or OutboundPolicy()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._task: Optional[asyncio.Task] = None
        # 商品接口失败后的冷却（item_id → 解禁时间戳）：风控/鉴权失败冷却久一点
        self._item_api_cooldown: Dict[str, float] = {}
        self.item_cooldown_risk = float(os.getenv("XIANYU_ITEM_COOLDOWN_RISK", "1800"))
        self.item_cooldown_error = float(os.getenv("XIANYU_ITEM_COOLDOWN_ERROR", "60"))
        self.counters: Dict[str, int] = {
            "messages_in": 0, "drafts": 0, "sent": 0, "manual_skipped": 0,
            "manual_toggles": 0, "item_cache_hits": 0, "item_api_calls": 0, "reply_errors": 0,
            "item_api_cooldown_skips": 0,
        }

        self.ws = ws_client or XianyuWSClient(
            api, device_id, url=url, on_message=self.handle_message,
            on_event=self._handle_ws_event, **(ws_kwargs or {}))

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self.session.record_event("engine_start", shadow_mode=self.shadow_mode, url=self.ws.url)
        self._task = asyncio.create_task(self.ws.run())
        logger.info("闲鱼值守引擎已启动（shadow_mode=%s）", self.shadow_mode)

    async def stop(self) -> None:
        self.ws.request_stop()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.session.record_event("engine_stop")
        logger.info("闲鱼值守引擎已停止")

    def _handle_ws_event(self, kind: str, payload: Dict[str, Any]) -> None:
        try:
            self.session.record_event(f"ws_{kind}", **payload)
        except Exception:
            logger.debug("记录 ws 事件失败", exc_info=True)
        self._dispatch_alert(kind, payload)
        if self._on_event:
            try:
                self._on_event(kind, payload)
            except Exception:
                logger.debug("转发 ws 事件失败", exc_info=True)

    def _dispatch_alert(self, kind: str, payload: Dict[str, Any]) -> None:
        """把事件交给告警器（异步任务，不阻塞收帧）。"""
        if not self.alerts:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.alerts.observe_ws_event(kind, payload or {}))

    # ------------------------------------------------------------------ #
    # 单条消息主流程
    # ------------------------------------------------------------------ #
    async def handle_message(self, msg: InboundMessage, *, allow_item_api: bool = True) -> Optional[Dict[str, Any]]:
        """处理一条入站消息。返回草稿/结果字典；影子模式下不发送。

        `allow_item_api=False` 时**不会**为取商品信息去打平台接口（模拟消息用，避免额外平台调用）。
        """
        lock = self._locks.setdefault(msg.chat_id, asyncio.Lock())
        async with lock:
            return await self._handle_locked(msg, allow_item_api=allow_item_api)

    async def _handle_locked(self, msg: InboundMessage, *, allow_item_api: bool = True) -> Optional[Dict[str, Any]]:
        self.counters["messages_in"] += 1

        # 1) 卖家侧：接管词切换 / 人工回复归档
        if msg.from_seller:
            if msg.text.strip() and msg.text.strip() in self.toggle_keywords:
                mode = self.session.toggle_manual_mode(msg.chat_id, self.manual_timeout)
                self.counters["manual_toggles"] += 1
                self.session.record_event("manual_toggle", chat_id=msg.chat_id, mode=mode)
                logger.info("会话 %s 切换为 %s", msg.chat_id, "人工接管" if mode == "manual" else "自动回复")
                return {"mode": mode, "sent": False}
            self.session.add_message(msg.chat_id, msg.sender_id, msg.item_id,
                                     "assistant", msg.text)
            self.session.record_event("seller_manual_reply", chat_id=msg.chat_id,
                                      text_len=len(msg.text))
            return {"mode": "manual", "sent": False, "recorded": True}

        # 2) 买家消息：先落库（人工接管期间也要留痕）
        self.session.add_message(msg.chat_id, msg.sender_id, msg.item_id, "user", msg.text)

        # 消息级幂等：同一条消息（同一 message_id）只生成一次、只发一次。
        # 内存去重挡不住进程重启，而平台在重连/补推时会重放同一条消息 ——
        # 真发送下那等于给买家回两条（本机在库里查到过 5 组重复 message_id 的草稿）。
        if self.session.seen(_message_key(msg.message_id)):
            self.counters["duplicate_messages"] = self.counters.get("duplicate_messages", 0) + 1
            self.session.record_event("duplicate_message_skipped", chat_id=msg.chat_id,
                                      message_id=msg.message_id)
            return {"skipped": True, "reason": "duplicate_message", "sent": False}

        if self.session.is_manual_mode(msg.chat_id):
            self.counters["manual_skipped"] += 1
            self.session.record_event("manual_skip", chat_id=msg.chat_id)
            return {"mode": "manual", "sent": False, "skipped": True}

        # 3) 取商品描述（缓存优先，未命中打接口并落缓存；模拟消息不打接口）
        item_desc = await self._item_description(msg.item_id, allow_api=allow_item_api)

        # 4) 生成回复（放线程池，绝不阻塞收帧）
        context = self.session.get_context(msg.chat_id)
        session_id = f"xianyu:{msg.chat_id}"
        try:
            result = await self.reply_generator(msg.text, session_id, context, item_desc)
        except (XianyuAuthError, XianyuRiskControlError):
            raise
        except Exception as exc:  # 单条失败不影响值守
            self.counters["reply_errors"] += 1
            self.session.record_event("reply_error", chat_id=msg.chat_id,
                                      error=f"{type(exc).__name__}: {exc}")
            self._dispatch_alert("reply_error", {"chat_id": msg.chat_id, "error": str(exc)})
            logger.warning("生成回复失败（会话 %s）: %s", msg.chat_id, exc)
            return {"error": str(exc), "sent": False}

        reply = (result or {}).get("reply") or ""
        if not reply or reply.strip() == "-":
            self.session.record_event("no_reply", chat_id=msg.chat_id,
                                      intent=(result or {}).get("intent"))
            return {"mode": "auto", "sent": False, "no_reply": True}

        intent = (result or {}).get("intent")
        reply = filter_outbound(reply)

        # 5) 议价计数（对齐上游 main.py:533-536：intent=price 时 +1）
        if intent == "price":
            count = self.session.increment_bargain_count(msg.chat_id)
            logger.info("会话 %s 议价次数 → %s", msg.chat_id, count)

        # 6) 落草稿（影子模式只到此为止；正式模式由 P4 打开发送）
        draft_id = self.session.save_draft(
            message_id=msg.message_id, chat_id=msg.chat_id, item_id=msg.item_id,
            inbound=msg.text, reply=reply, intent=intent,
            sources=(result or {}).get("sources"), engine=(result or {}).get("engine"),
            mode="shadow" if self.shadow_mode else "live")
        # 消息级幂等落库（跨进程重启仍然有效）：同一 message_id 只答一次。
        # 真发送下这条是硬要求——平台重连重放同一条消息时，凭空答复第二遍会给买家发两条。
        self.session.mark(_message_key(msg.message_id), msg.chat_id)
        self.counters["drafts"] += 1
        self.session.record_event("draft_saved", chat_id=msg.chat_id, draft_id=draft_id,
                                  intent=intent, shadow_mode=self.shadow_mode)

        sent = False
        if not self.shadow_mode:
            sent = await self._send(msg, reply, draft_id)
        return {"draft_id": draft_id, "reply": reply, "intent": intent,
                "mode": "shadow" if self.shadow_mode else "live", "sent": sent}

    # ------------------------------------------------------------------ #
    async def _item_description(self, item_id: Optional[str], *, allow_api: bool = True) -> str:
        if not item_id:
            return "（本条消息未带商品 ID）"
        cached = self.session.get_item_info(item_id)
        if cached:
            self.counters["item_cache_hits"] += 1
            return build_item_description(cached)
        if not allow_api:
            # 模拟消息：不打平台接口，用中文占位，避免为演示多打一次平台调用
            return f"（模拟消息：未查询商品 {item_id} 的详情）"

        # 失败后的冷却：同一商品连续失败时**不要每条消息都再打一次平台接口**。
        # 真机实测：同一条商品被平台以 RGV587（"被挤爆啦"）拒绝后，下一条消息照样再打一次，
        # 等于自己把风控信号刷满——7×24 值守最怕的就是这个（账号被标记就全盘皆输）。
        cooldown_until = self._item_api_cooldown.get(item_id, 0.0)
        now = time.time()
        if cooldown_until > now:
            self.counters["item_api_cooldown_skips"] = \
                self.counters.get("item_api_cooldown_skips", 0) + 1
            self.session.record_event("item_api_skipped_cooldown", item_id=item_id,
                                      remain_seconds=int(cooldown_until - now))
            return f"（商品 {item_id} 信息获取失败）"

        self.counters["item_api_calls"] += 1
        try:
            info = await self.api.get_item_info(item_id)
        except (XianyuAuthError, XianyuRiskControlError) as exc:
            # 风控要冷却更久：继续打只会让风控更重
            self._item_api_cooldown[item_id] = now + self.item_cooldown_risk
            self.session.record_event("item_fetch_auth_error", error=str(exc),
                                      cooldown_seconds=int(self.item_cooldown_risk))
            logger.error("取商品信息被拒（已进入 %ss 冷却，不再逐条重试）: %s",
                         int(self.item_cooldown_risk), exc)
            self._dispatch_alert("item_fetch_auth_error", {"item_id": item_id, "error": str(exc)})
            return f"（商品 {item_id} 信息获取失败）"
        except XianyuApiError as exc:
            self._item_api_cooldown[item_id] = now + self.item_cooldown_error
            self.session.record_event("item_fetch_error", error=str(exc),
                                      cooldown_seconds=int(self.item_cooldown_error))
            return f"（商品 {item_id} 信息获取失败）"
        if not info:
            self._item_api_cooldown[item_id] = now + self.item_cooldown_error
            return f"（商品 {item_id} 暂无信息）"
        self._item_api_cooldown.pop(item_id, None)
        self.session.save_item_info(item_id, info)
        return build_item_description(info)

    async def _send(self, msg: InboundMessage, reply: str, draft_id: int) -> bool:
        """真实发送（`shadow_mode=False` 时才会走到这里，即 P4）。

        所有出站消息都必须先过 `OutboundPolicy`（合规 + 限速 + 拟人化延迟 + 去重）：
        被拦就保留草稿不发送，并记事件（`send_blocked`）与告警，绝不绕过闸门硬发。
        """
        ws = self.ws.current_ws
        if ws is None:
            self.session.record_event("send_skipped_no_connection", chat_id=msg.chat_id)
            logger.warning("当前没有活动连接，跳过发送（草稿 %s 已保留）", draft_id)
            return False

        decision = await self.outbound.acquire(msg.chat_id, reply)
        if not decision.allowed:
            self.session.record_event("send_blocked", chat_id=msg.chat_id, draft_id=draft_id,
                                      reason=decision.reason, detail=json.dumps(decision.detail,
                                                                               ensure_ascii=False))
            self._dispatch_alert("send_blocked", {"chat_id": msg.chat_id, "reason": decision.reason,
                                                  "draft_id": draft_id})
            return False

        try:
            await self.ws.send_text(ws, msg.chat_id, msg.sender_id, decision.text)
        except Exception as exc:
            # 发送失败必须**留明确痕迹**：以前异常会冒到收帧循环，被记成 `frame_error`
            # （误导排查方向），草稿留在未发送状态却没人知道为什么。
            self.counters["send_errors"] = self.counters.get("send_errors", 0) + 1
            self.session.record_event("send_failed", chat_id=msg.chat_id, draft_id=draft_id,
                                      error=f"{type(exc).__name__}: {exc}")
            self._dispatch_alert("send_failed", {"chat_id": msg.chat_id, "draft_id": draft_id,
                                                 "error": str(exc)})
            logger.warning("发送失败（草稿 %s 保留未发送）: %s", draft_id, exc)
            # 把这次"已发送"记账回滚：否则 300 秒重复文本窗口会把重发的同一条回复拦掉，
            # 变成"发了失败 → 想重发却被自己的去重挡住 → 买家永远收不到"
            try:
                self.outbound.note_failed(msg.chat_id)
            except Exception:
                logger.debug("回滚出站记账失败（不影响值守）", exc_info=True)
            return False

        self.session.add_message(msg.chat_id, self.api.unb, msg.item_id, "assistant", decision.text)
        self.session.mark_draft_sent(draft_id)
        self.counters["sent"] += 1
        self.session.record_event("sent", chat_id=msg.chat_id, draft_id=draft_id,
                                  delay_s=decision.delay_seconds,
                                  compliance_adjusted=decision.adjusted)
        return True

    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        return {
            "shadow_mode": self.shadow_mode,
            "reply_engine": self.reply_engine,
            "available_reply_engines": sorted(GENERATORS),
            "toggle_keywords": self.toggle_keywords,
            "counters": dict(self.counters),
            "ws": self.ws.health(),
            "session": self.session.stats(),
            "manual_chats": self.session.list_manual_chats(),
            "alerts": self.alerts.stats() if self.alerts else None,
            "outbound": self.outbound.snapshot(),
        }
