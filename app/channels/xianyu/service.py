# -*- coding: utf-8 -*-
r"""闲鱼值守通道服务：把 engine / session / api 组装成一个可由 FastAPI 托管的对象。

- **默认不启动**：只有 `XIANYU_ENABLED=true` 时才会在 lifespan 里拉起（`config.settings`）。
- 启动失败（凭据缺失、Cookie 无效、取 token 被风控）**不会影响主服务**：把错误记在 `self.last_error`
  并如实暴露在健康接口里，等人工处理——这是与上游 `sys.exit(1)` 最大的区别。
- 对外只暴露「看」和「切换人工接管」两类接口，**不提供直接发送消息的接口**（发送只能由引擎决定）。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.channels.xianyu import config as channel_config
from app.channels.xianyu.alerts import AlertManager
from app.channels.xianyu.api import XianyuApi, XianyuAuthError, XianyuRiskControlError
from app.channels.xianyu.engine import XianyuWatchEngine
from app.channels.xianyu.outbound import build_policy
from app.channels.xianyu.protocol import (
    CookieError,
    load_credentials,
    load_or_create_device_id,
)
from app.channels.xianyu.session import ChannelSessionStore
from app.channels.xianyu.ws import SyncCursor

logger = logging.getLogger("airobot.xianyu.service")


class XianyuChannelService:
    """闲鱼通道生命周期 + 健康/查询接口。"""

    def __init__(self, settings: Optional[Any] = None,
                 api_factory: Optional[Callable[..., XianyuApi]] = None) -> None:
        """`api_factory(cookies, user_agent, credential_path) -> XianyuApi` 是**测试/干跑用的接缝**。

        不传就用真实 httpx 客户端（会真的打闲鱼 mtop 接口）。离线自测与端到端干跑必须传它，
        否则测试会顺手调真实平台接口——本项目踩过这个坑，故显式留出这个参数。
        """
        self.settings = settings or channel_config.settings
        self.api_factory = api_factory
        self.session: Optional[ChannelSessionStore] = None
        self.api: Optional[XianyuApi] = None
        self.engine: Optional[XianyuWatchEngine] = None
        self.alerts: Optional[AlertManager] = None
        self.device_id: Optional[str] = None
        self.started = False
        self.last_error: Optional[str] = None
        self.unb_masked: Optional[str] = None
        self.dns_overrides: Dict[str, str] = {}
        self._started_at: Optional[float] = None
        self._patrol_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    @property
    def shadow_mode(self) -> bool:
        return bool(getattr(self.settings, "shadow_mode", True))

    async def start(self) -> bool:
        """按配置启动通道。返回是否成功（失败不抛，记进 last_error）。"""
        if not self.enabled:
            logger.info("闲鱼通道未启用（XIANYU_ENABLED=false），跳过启动")
            return False
        try:
            from app.channels.xianyu.dns import apply_override
            overrides = apply_override(getattr(self.settings, "dns_override", "") or "")
            if overrides:
                self.dns_overrides = overrides

            creds = load_credentials(self.settings.credentials_path)
            cookies = creds["_cookies"]
            self.device_id = load_or_create_device_id(creds["account"]["unb"], self.settings.device_store)
            self.unb_masked = creds["account"]["unb"][:4] + "***"
            self.session = ChannelSessionStore(self.settings.db_path,
                                               max_history=self.settings.max_history)
            if self.api_factory:
                self.api = self.api_factory(cookies, creds.get("user_agent"),
                                            self.settings.credentials_path)
            else:
                self.api = XianyuApi(cookies, credential_path=self.settings.credentials_path,
                                     user_agent=creds.get("user_agent"))
            self.alerts = AlertManager(
                self.session,
                webhook_url=getattr(self.settings, "alert_webhook", "") or None,
                style=getattr(self.settings, "alert_style", "generic"),
                cooldown_seconds=getattr(self.settings, "alert_cooldown", 600))
            self.engine = XianyuWatchEngine(
                self.api, self.session, self.device_id,
                url=self.settings.ws_url,
                shadow_mode=self.shadow_mode,
                reply_engine=getattr(self.settings, "reply_engine", None),
                toggle_keywords=self.settings.toggle_keywords,
                manual_timeout=self.settings.manual_timeout,
                alerts=self.alerts,
                outbound=build_policy(self.settings),
                ws_kwargs={
                    "heartbeat_interval": self.settings.heartbeat_interval,
                    "heartbeat_timeout": self.settings.heartbeat_timeout,
                    "token_refresh_interval": self.settings.token_refresh_interval,
                    "message_expire_ms": self.settings.message_expire_ms,
                    "reconnect_initial": self.settings.reconnect_initial,
                    "reconnect_max": self.settings.reconnect_max,
                    "frame_dump": getattr(self.settings, "frame_dump", "") or None,
                    "sync_pts_mode": getattr(self.settings, "sync_pts", None),
                    # 同步游标落盘：断线重连时从上次进度续，掉线窗口内的买家消息才会被平台补推
                    "sync_cursor": SyncCursor(
                        Path(str(self.settings.device_store)).parent / "xianyu_sync_pts.json"),
                })
            await self.engine.start()
            self.started = True
            self.last_error = None
            self._started_at = asyncio.get_event_loop().time()
            self._patrol_task = asyncio.create_task(self._patrol_loop())
            logger.info("闲鱼通道已启动（shadow_mode=%s，账号 %s）", self.shadow_mode, self.unb_masked)
            return True
        except CookieError as exc:
            self.last_error = f"凭据不可用: {exc}"
            logger.error("闲鱼通道启动失败：%s", self.last_error)
        except (XianyuAuthError, XianyuRiskControlError) as exc:
            self.last_error = f"登录态问题: {exc}"
            logger.error("闲鱼通道启动失败：%s", self.last_error)
        except Exception as exc:  # 任何异常都不该拖垮主服务
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("闲鱼通道启动出现未预期异常（已隔离，不影响主服务）")
        return False

    async def _patrol_loop(self) -> None:
        """定期健康巡检 → 告警（启用中却长时间未连接、登录态失效等）。"""
        interval = max(5, int(getattr(self.settings, "alert_patrol_interval", 30) or 30))
        while True:
            try:
                await asyncio.sleep(interval)
                if self.alerts:
                    await self.alerts.observe_health(self.health())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("健康巡检异常（已忽略）", exc_info=True)

    async def stop(self) -> None:
        if self._patrol_task:
            self._patrol_task.cancel()
            await asyncio.gather(self._patrol_task, return_exceptions=True)
            self._patrol_task = None
        if self.engine:
            try:
                await self.engine.stop()
            except Exception:
                logger.debug("停止引擎失败", exc_info=True)
        if self.api:
            try:
                await self.api.aclose()
            except Exception:
                logger.debug("关闭 api 客户端失败", exc_info=True)
        self.started = False
        logger.info("闲鱼通道已停止")

    async def wait_ready(self, timeout: float = 20.0, interval: float = 0.2) -> bool:
        """等连接注册完成（`start()` 只是拉起任务，不等建连——避免拖慢主服务启动）。

        给启动脚本、P1 存活体检和测试用：返回是否在超时前完成注册。
        """
        if not self.engine:
            return False
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self.engine.ws.registered and self.engine.ws.connected:
                return True
            await asyncio.sleep(interval)
        return False

    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "enabled": self.enabled,
            "started": self.started,
            "shadow_mode": self.shadow_mode,
            "account": self.unb_masked,
            "last_error": self.last_error,
            "config": self.settings.summary(),
            # 未启动时也给占位，前端/巡检不用做字段存在性判断
            "alerts": self.alerts.stats() if self.alerts else None,
        }
        if self.engine:
            payload.update(self.engine.health())
        elif self.session:
            payload["session"] = self.session.stats()
        else:
            payload["session"] = None
        return payload

    def recent_drafts(self, limit: int = 20, unsent_only: bool = False) -> List[Dict[str, Any]]:
        if not self.session:
            return []
        return self.session.list_drafts(limit=limit, unsent_only=unsent_only)

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.session:
            return []
        return self.session.recent_events(limit=limit)

    def list_manual_chats(self) -> List[Dict[str, Any]]:
        if not self.session:
            return []
        return self.session.list_manual_chats()

    def recent_alerts(self, limit: int = 20) -> List[Dict[str, Any]]:
        """最近告警（从事件流水读，进程重启也还在）。"""
        if not self.alerts:
            return []
        return self.alerts.recent(limit)

    def toggle_manual(self, chat_id: str) -> str:
        if not self.session:
            raise RuntimeError("通道未启动（无状态库）")
        mode = self.session.toggle_manual_mode(chat_id, self.settings.manual_timeout)
        self.session.record_event("manual_toggle_api", chat_id=chat_id, mode=mode)
        return mode

    async def fetch_messages(self, cid: str, limit: int = 10, replay: bool = False,
                             timeout: float = 20.0) -> Dict[str, Any]:
        """按会话 id **只读**拉取最近聊天记录（复用当前连接，不再新开一条）。

        排查「买家消息没推过来」用：确认平台上到底有没有这条消息、真实字段长什么样。
        只发查询帧，**不会发送任何消息**。

        `replay=True` 时把拉到的买家消息**按时序喂进值守链路**（影子模式只落草稿、不发送）。
        用途：长连接漏推时可以补救，也让我们能用真实平台数据端到端验证「解析→路由→草稿」，
        而不必等买家现场再发一条。
        """
        if not (self.engine and self.engine.ws.current_ws):
            raise RuntimeError("通道未启动或当前无活动连接")
        frame = await self.engine.ws.request(
            "/r/MessageManager/listUserMessages",
            [f"{cid}@goofish", False, 9007199254740991, int(limit), False], timeout=timeout)
        body = frame.get("body") or {}
        if "userMessageModels" not in body:
            # 以前这里会静默返回 count=0，把「连接/查询异常」伪装成「会话是空的」
            raise RuntimeError(f"平台响应里没有 userMessageModels（body keys={sorted(body.keys())}）")

        from app.channels.xianyu.ws import parse_history_message

        messages = [parse_history_message(m) for m in (body.get("userMessageModels") or [])]
        unparsed = sum(1 for m in messages if not m["text"])

        replayed = []
        if replay:
            replayed = await self._replay(messages, cid)

        if self.session:
            self.session.record_event("fetch_messages", chat_id=cid, count=len(messages),
                                      replay=bool(replay))
        return {"cid": cid, "count": len(messages), "has_more": body.get("hasMore"),
                "messages": messages, "unparsed": unparsed, "replayed": replayed,
                "raw_keys": sorted(body.keys())}

    async def _replay(self, messages: List[Dict[str, Any]], cid: str) -> List[Dict[str, Any]]:
        """把拉到的买家消息按时序重放进值守链路（只落草稿，绝不发送）。"""
        from app.channels.xianyu.ws import InboundMessage

        if not self.session:
            raise RuntimeError("通道未启动（无状态库）")
        out: List[Dict[str, Any]] = []
        ordered = sorted(messages, key=lambda m: m.get("create_time") or 0)
        for m in ordered:
            if not m.get("text"):
                continue
            if not m.get("is_plain_text", True):
                # 平台/客户端注入的提示卡（如"恭喜新手卖家…"）不是买家提问，喂进去会生成答非所问的草稿
                out.append({"text": m["text"][:40], "skipped": "平台提示卡",
                            "content_type": m.get("content_type")})
                continue
            msg = InboundMessage(
                message_id=str(m.get("message_id") or f"replay-{uuid.uuid4().hex[:12]}"),
                chat_id=cid, sender_id=str(m.get("sender_id") or ""),
                sender_name=m.get("sender_name") or "", text=m["text"], item_id=None,
                create_time_ms=int(m.get("create_time") or time.time() * 1000),
            )
            result = await self.engine.handle_message(msg, allow_item_api=False) or {}
            out.append({"text": m["text"], "sender_id": m.get("sender_id"),
                        "draft": result.get("draft") or result.get("reply"),
                        "action": result.get("action"), "reason": result.get("reason")})
        self.session.record_event("replay_messages", chat_id=cid, count=len(out))
        return out

    async def resend_draft(self, draft_id: int) -> Dict[str, Any]:
        """重发一条**未发送**的草稿（发送失败后的补救口子）。

        为什么需要：发送可能因为连接刚好断开而失败，草稿会保留在库里；如果没有重发口子，
        那条回复就永远停在"未发送"（真机踩过：买家问「考研资料」，草稿已生成却卡在发送那一步）。
        仍然走同一个出站闸门（合规/限速/去重/拟人化延迟），不绕过任何安全检查。
        """
        if not (self.engine and self.session):
            raise RuntimeError("通道未启动：请先把 XIANYU_ENABLED=true 并重启服务")
        if self.shadow_mode:
            raise RuntimeError("影子模式下不发送（草稿只落库）；要重发请先把 XIANYU_SHADOW_MODE 改为 false")
        draft = self.session.get_draft(int(draft_id))
        if not draft:
            raise RuntimeError(f"没有 id={draft_id} 的草稿")
        if draft.get("sent"):
            raise RuntimeError(f"草稿 {draft_id} 已经发送过了（sent_at={draft.get('sent_at')}）")
        reply = (draft.get("reply") or "").strip()
        if not reply or reply == "-":
            raise RuntimeError(f"草稿 {draft_id} 没有可发送的正文")

        buyer_id = self.session.last_buyer_id(draft["chat_id"])
        if not buyer_id:
            raise RuntimeError(f"会话 {draft['chat_id']} 里找不到买家 uid，无法确定收件人")

        from app.channels.xianyu.ws import InboundMessage

        msg = InboundMessage(
            message_id=str(draft.get("message_id") or f"resend-{draft_id}"),
            chat_id=draft["chat_id"], sender_id=str(buyer_id), sender_name="买家",
            text=str(draft.get("inbound") or ""), item_id=draft.get("item_id"),
            create_time_ms=int(time.time() * 1000))
        self.session.record_event("draft_resend_attempt", chat_id=draft["chat_id"], draft_id=draft_id)
        ok = await self.engine._send(msg, reply, int(draft_id))
        return {"draft_id": draft_id, "sent": bool(ok), "chat_id": draft["chat_id"],
                "to_id": str(buyer_id)[:4] + "***", "text": reply}

    async def simulate_message(self, message: str, *, chat_id: str = "SIM-C1",
                               item_id: Optional[str] = None,
                               sender_id: str = "999999999") -> Dict[str, Any]:
        """影子模式自测：把一条**模拟买家消息**喂进值守链路（真实专家层 + 真实知识库 + 真实会话状态），
        只落草稿、**绝不发送**。

        为什么需要它：影子模式要验质量，但柜台可能几天没人来问；有了它，你可以立刻看到
        「买家这么问 → AI 会这么答」，而不用等真实买家，也不用另开一个闲鱼号。
        安全约束：**仅在 `shadow_mode=True` 时可用**——否则模拟消息可能被真的发到一个不存在的会话。
        """
        if not (self.engine and self.session):
            raise RuntimeError("通道未启动：请先把 XIANYU_ENABLED=true 并重启服务")
        if not self.shadow_mode:
            raise RuntimeError("仅影子模式下允许模拟消息（shadow_mode=false 时禁用，避免误发）")

        from uuid import uuid4

        from app.channels.xianyu.ws import InboundMessage

        msg = InboundMessage(
            message_id=f"sim-{uuid4().hex[:12]}", chat_id=chat_id, sender_id=sender_id,
            sender_name="模拟买家", text=message, item_id=item_id,
            create_time_ms=int(time.time() * 1000),
        )
        self.session.record_event("simulate_message", chat_id=chat_id, text_len=len(message))
        result = await self.engine.handle_message(msg, allow_item_api=False) or {}
        result["simulated"] = True
        result["chat_id"] = chat_id
        return result
