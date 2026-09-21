# -*- coding: utf-8 -*-
r"""闲鱼值守告警：把通道异常变成「有冷却的告警」，并可选推送到 webhook。

上游没有任何告警，异常直接 `sys.exit(1)` 或打一行日志；值守 7×24 的实际情况是：
出错时人不在电脑前，需要**可查（事件流水）+ 可推（webhook）+ 不重复刷屏（冷却）**。

规则（都由实测过的通道事件驱动）：

| 触发 | 来源 | 严重度 |
|---|---|---|
| `auth_error` 登录态失效 | `ws`/`api` 抛 `XianyuAuthError` | critical（需人工重新导出 Cookie） |
| `risk_control` 触发风控 | 抛 `XianyuRiskControlError` | critical（需人工过滑块） |
| `heartbeat_timeout` 心跳无响应 | `ws` 判死 | warning |
| `connection_lost` 启用中但长时间未连接 | 健康巡检 | critical |
| `decode_failed` / `frame_error` 解析异常 | 收帧 | warning（按次数累计） |
| `reply_error` 生成失败 | 引擎 | warning |

推送样式支持 `feishu`（飞书自定义机器人）/ `wecom`（企业微信机器人）/ `generic`（原样 JSON）。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("airobot.xianyu.alerts")

ALERT_PREFIX = "alert:"

# 事件类型 → (告警类型, 标题, 严重度)
WS_EVENT_ALERTS: Dict[str, tuple] = {
    "auth_error": ("auth_error", "闲鱼登录态失效，需要重新导出 Cookie", "critical"),
    # 平台不关连接、只回 401 "token is not found"：连接看着是好的但已收不到消息
    "session_invalid": ("session_invalid", "闲鱼会话被平台判定失效（已自动换 token 重连）", "critical"),
    "risk_control": ("risk_control", "闲鱼触发风控（需人工过滑块后重新导出 Cookie）", "critical"),
    "heartbeat_timeout": ("heartbeat_timeout", "闲鱼长连接心跳无响应，已判定掉线并重连", "warning"),
    "decode_error": ("decode_error", "闲鱼同步包解码失败（帧已丢弃）", "warning"),
    "frame_error": ("frame_error", "处理闲鱼帧时出错（该帧已丢弃）", "warning"),
    "send_skipped_no_connection": ("send_failed", "想发送回复时没有活动连接（草稿已保留）", "warning"),
    "send_failed": ("send_failed", "回复发送失败（草稿已保留，需人工确认买家是否收到）", "critical"),
    "send_blocked": ("send_blocked", "出站闸门拦下了一条回复（限速/重复/静默时段/空回复）", "warning"),
    "connection_error": ("connection_error", "闲鱼长连接异常断开", "warning"),
}


@dataclass
class Alert:
    kind: str
    title: str
    severity: str = "warning"
    detail: str = ""
    ts: float = field(default_factory=time.time)
    count: int = 1

    def to_payload(self, style: str = "generic") -> Dict[str, Any]:
        text = f"[闲鱼值守][{self.severity}] {self.title}" + (f"\n{self.detail}" if self.detail else "")
        if style == "feishu":
            return {"msg_type": "text", "content": {"text": text}}
        if style == "wecom":
            return {"msgtype": "text", "text": {"content": text}}
        return {"kind": self.kind, "severity": self.severity, "title": self.title,
                "detail": self.detail, "ts": self.ts, "text": text}


class AlertManager:
    """告警判定 + 冷却去重 + 可选 webhook 推送（推送失败不影响值守）。"""

    def __init__(self, session=None, *, webhook_url: Optional[str] = None, style: str = "generic",
                 cooldown_seconds: int = 600, disconnect_grace: int = 60,
                 transport: Optional[httpx.AsyncBaseTransport] = None, timeout: float = 8.0,
                 clock=time.time) -> None:
        self.session = session
        self.webhook_url = webhook_url
        self.style = (style or "generic").lower()
        self.cooldown_seconds = cooldown_seconds
        self.disconnect_grace = disconnect_grace
        self.timeout = timeout
        self._transport = transport
        self._clock = clock
        self._last_fired: Dict[str, float] = {}
        self._counts: Dict[str, int] = {}
        self._active: Dict[str, bool] = {}
        self.fired_total = 0
        self.suppressed_total = 0
        self.webhook_failures = 0

    # ------------------------------------------------------------------ #
    def _record(self, alert: Alert) -> None:
        if self.session:
            try:
                self.session.record_event(f"{ALERT_PREFIX}{alert.kind}", detail=alert.detail,
                                          severity=alert.severity, count=alert.count)
            except Exception:
                logger.debug("写入告警事件失败", exc_info=True)

    async def fire(self, kind: str, title: str, detail: str = "",
                   severity: str = "warning") -> Optional[Alert]:
        """触发一条告警；冷却期内同类告警被抑制（返回 None）。"""
        now = self._clock()
        last = self._last_fired.get(kind, 0.0)
        if now - last < self.cooldown_seconds:
            self.suppressed_total += 1
            self._counts[kind] = self._counts.get(kind, 0) + 1
            logger.debug("告警 %s 在冷却期内被抑制（第 %s 次）", kind, self._counts[kind])
            return None
        self._last_fired[kind] = now
        self._active[kind] = True
        self.fired_total += 1
        alert = Alert(kind=kind, title=title, detail=detail, severity=severity, ts=now,
                      count=self._counts.pop(kind, 0) + 1)
        logger.warning("触发告警[%s] %s %s", severity, title, detail)
        self._record(alert)
        await self._push(alert)
        return alert

    async def _push(self, alert: Alert) -> bool:
        if not self.webhook_url:
            return False
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout,
                                         trust_env=False) as client:
                resp = await client.post(self.webhook_url, json=alert.to_payload(self.style))
            ok = 200 <= resp.status_code < 300
            if not ok:
                self.webhook_failures += 1
                logger.warning("告警 webhook 返回 %s", resp.status_code)
            return ok
        except Exception as exc:  # 推送失败不能影响值守
            self.webhook_failures += 1
            logger.warning("告警 webhook 推送失败（已忽略）: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    async def observe_ws_event(self, kind: str, payload: Dict[str, Any]) -> Optional[Alert]:
        """通道事件 → 告警。未配置规则的 kind 直接忽略。"""
        rule = WS_EVENT_ALERTS.get(kind)
        if not rule:
            return None
        alert_kind, title, severity = rule
        detail = json.dumps(payload, ensure_ascii=False, default=str)[:300] if payload else ""
        return await self.fire(alert_kind, title, detail, severity)

    async def observe_health(self, health: Dict[str, Any]) -> List[Alert]:
        """健康巡检：启用中但长时间未连接 → 告警；恢复后记一条恢复事件。"""
        out: List[Alert] = []
        enabled = bool(health.get("enabled"))
        started = bool(health.get("started"))
        ws = health.get("ws") or {}
        connected = bool(ws.get("connected") and ws.get("registered"))
        if enabled and started and not connected:
            alert = await self.fire(
                "connection_lost", "闲鱼通道未连接（值守中断）",
                f"enabled={enabled} started={started} connected={connected} "
                f"last_error={health.get('last_error')}",
                severity="critical")
            if alert:
                out.append(alert)
        elif connected and self._active.get("connection_lost"):
            self._active["connection_lost"] = False
            if self.session:
                self.session.record_event(f"{ALERT_PREFIX}recovered", detail="长连接已恢复")
                logger.info("闲鱼长连接已恢复")
        return out

    # ------------------------------------------------------------------ #
    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """最近告警（从事件流水里读，进程重启也还在）。"""
        if not self.session:
            return []
        events = self.session.recent_events(limit * 4)
        out = []
        for event in events:
            if str(event.get("kind", "")).startswith(ALERT_PREFIX):
                item = dict(event)
                item["alert_kind"] = item["kind"][len(ALERT_PREFIX):]
                out.append(item)
            if len(out) >= limit:
                break
        return out

    def stats(self) -> Dict[str, Any]:
        return {"fired": self.fired_total, "suppressed": self.suppressed_total,
                "webhook_configured": bool(self.webhook_url), "style": self.style,
                "cooldown_seconds": self.cooldown_seconds,
                "webhook_failures": self.webhook_failures,
                "active": [k for k, v in self._active.items() if v]}
