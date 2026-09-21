# -*- coding: utf-8 -*-
r"""出站策略：所有「要发给买家」的消息都必须过这一关（合规 + 限速 + 拟人化延迟 + 去重）。

上游在这块几乎是空的：只有一个可选的随机延迟（`main.py:543-554`，默认关闭），
没有任何频率限制、没有重复抑制、没有静默时段。自动值守最容易被平台风控抓的特征就是
「秒回 + 模板化 + 高频」，所以这里把闸门集中到一处，**先判定再等待再记账**：

| 关卡 | 作用 | 默认 |
|---|---|---|
| 合规过滤 | 站外引流词整条替换成安全话术（复用 `specialists.guard`） | 开 |
| 空回复/占位 | 空串、`-`（no_reply）不允许发出 | 开 |
| 重复文本抑制 | 同一会话窗口内重复同一句话 → 拦（防循环刷屏） | 300 秒窗口 |
| 每会话最小间隔 | 同一会话两条消息之间至少间隔 N 秒 → 需要等待就等，等太久就拦 | 3 秒 / 最长排 30 秒 |
| 全局限速 | 每分钟 / 每小时总发送上限 → 超了直接拦（不排队） | 20 条/分、300 条/时 |
| 拟人化延迟 | 基础延迟 + 每字延迟（上限 10 秒），模拟人工打字 | 开 |
| 静默时段 | 指定时间段不发（默认关闭） | 关 |

判定逻辑与等待分离：`plan()` 是纯计算（可离线单测、可预演），`acquire()` 才会真的 `sleep` 并记账。
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from app.agents.specialists.guard import filter_outbound

logger = logging.getLogger("airobot.xianyu.outbound")

PLACEHOLDER_REPLIES = ("", "-", "—")


@dataclass
class OutboundDecision:
    allowed: bool
    text: str = ""
    reason: str = ""
    delay_seconds: float = 0.0
    adjusted: bool = False          # 合规过滤是否改写了内容
    detail: Dict[str, Any] = field(default_factory=dict)


class OutboundPolicy:
    """出站闸门。所有时间来源、随机源与 sleep 都可注入，便于离线自测与预演。"""

    def __init__(
        self,
        *,
        min_interval_per_chat: float = 3.0,
        max_wait_seconds: float = 30.0,
        max_per_minute: int = 20,
        max_per_hour: int = 300,
        duplicate_window_seconds: float = 300.0,
        typing_simulation: bool = True,
        typing_base_range: Tuple[float, float] = (0.0, 1.0),
        typing_per_char_range: Tuple[float, float] = (0.1, 0.3),
        typing_max_delay: float = 10.0,
        quiet_hours: Optional[str] = None,
        allowlist: Optional[Iterable[str]] = None,
        max_per_day: int = 0,
        clock: Callable[[], float] = time.time,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.min_interval_per_chat = max(0.0, min_interval_per_chat)
        self.max_wait_seconds = max(0.0, max_wait_seconds)
        self.max_per_minute = max(1, max_per_minute)
        self.max_per_hour = max(1, max_per_hour)
        self.duplicate_window_seconds = max(0.0, duplicate_window_seconds)
        self.typing_simulation = typing_simulation
        self.typing_base_range = typing_base_range
        self.typing_per_char_range = typing_per_char_range
        self.typing_max_delay = typing_max_delay
        self.quiet_hours = self._parse_quiet_hours(quiet_hours)
        # 灰度白名单：非空时**只允许**给这些会话发送（P4 第一次真发送用它把影响面锁死在一个会话里）
        self.allowlist = {str(c).strip() for c in (allowlist or []) if str(c).strip()}
        # 每日发送上限（0=不限）：给"自动发送"一个硬天花板，出事也不会滚雪球
        self.max_per_day = max(0, int(max_per_day))
        self._clock = clock
        self._rng = rng or random.Random()

        self._last_sent_at: Dict[str, float] = {}
        self._last_text: Dict[str, Tuple[str, float]] = {}
        self._sent_times: list = []
        # 全部发送记录（不过期）：每日上限要跨"最近一小时"统计，_sent_times 会被裁剪
        self._sent_log: list = []
        self._pending_until: Dict[str, float] = {}
        self.stats: Dict[str, Any] = {"allowed": 0, "blocked": 0, "adjusted": 0,
                                      "delays": 0, "total_delay_seconds": 0.0}

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_quiet_hours(spec: Optional[str]) -> Optional[Tuple[int, int]]:
        """解析 `23:00-08:00`（支持跨天）。空/非法则视为不启用。"""
        if not spec or "-" not in spec:
            return None
        try:
            start_s, end_s = spec.split("-", 1)
            start = int(start_s.strip().split(":")[0])
            end = int(end_s.strip().split(":")[0])
            if 0 <= start <= 23 and 0 <= end <= 24:
                return start, end
        except Exception:
            logger.warning("静默时段配置无法解析，已忽略: %r", spec)
        return None

    def _in_quiet_hours(self, now: float) -> bool:
        if not self.quiet_hours:
            return False
        start, end = self.quiet_hours
        hour = time.localtime(now).tm_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end      # 跨天，如 23:00-08:00

    def _rate_counts(self, now: float) -> Tuple[int, int]:
        self._sent_times = [t for t in self._sent_times if now - t < 3600]
        last_minute = sum(1 for t in self._sent_times if now - t < 60)
        return last_minute, len(self._sent_times)

    def _daily_count(self, now: float) -> int:
        """当天（**本地时区**，与卖家作息一致）已发送条数。

        `_sent_times` 只留最近一小时，算不了当天，所以这里查全部记录 `_sent_log`。
        注意 `mktime` 要的是 **9 元组**（年月日时分秒+周几+年内第几天+夏令时），
        用 `localtime(now)[:3] + (0,0,0,0,0)` 只凑出 8 个 → TypeError，
        被 except 吞掉后会悄悄退化成"最近 24 小时"（本机自测抓到过）。
        """
        try:
            t = time.localtime(now)
            day_start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
        except Exception:
            logger.debug("本地零点计算失败，退回滚动 24 小时口径", exc_info=True)
            day_start = now - 86400
        return sum(1 for t in self._sent_log if t >= day_start)

    def typing_delay(self, text: str) -> float:
        """拟人化延迟：基础延迟 + 每字延迟（上限封顶）。"""
        if not self.typing_simulation or not text:
            return 0.0
        base = self._rng.uniform(*self.typing_base_range)
        per_char = self._rng.uniform(*self.typing_per_char_range)
        return round(min(base + len(text) * per_char, self.typing_max_delay), 2)

    # ------------------------------------------------------------------ #
    def plan(self, chat_id: str, text: str, *, simulate_typing: bool = True) -> OutboundDecision:
        """纯判定：不 sleep、不改状态（除合规改写结果体现在返回值里）。"""
        now = self._clock()
        raw = text or ""
        if raw.strip() in PLACEHOLDER_REPLIES:
            return OutboundDecision(False, text=raw, reason="empty_reply",
                                    detail={"length": len(raw)})

        safe = filter_outbound(raw)
        adjusted = safe != raw

        # 灰度白名单排在最前：不在名单内的会话**一律不发**（P4 首发送靠它把影响面锁在一个会话）
        if self.allowlist and chat_id not in self.allowlist:
            return OutboundDecision(False, text=safe, reason="not_in_allowlist", adjusted=adjusted,
                                    detail={"allowlist_size": len(self.allowlist)})

        if self._in_quiet_hours(now):
            return OutboundDecision(False, text=safe, reason="quiet_hours",
                                    adjusted=adjusted, detail={"quiet_hours": self.quiet_hours})

        if self.max_per_day:
            sent_today = self._daily_count(now)
            if sent_today >= self.max_per_day:
                return OutboundDecision(False, text=safe, reason="rate_limited_day", adjusted=adjusted,
                                        detail={"per_day": sent_today, "limit": self.max_per_day})

        last_minute, last_hour = self._rate_counts(now)
        if last_minute >= self.max_per_minute:
            return OutboundDecision(False, text=safe, reason="rate_limited_minute", adjusted=adjusted,
                                    detail={"per_minute": last_minute, "limit": self.max_per_minute})
        if last_hour >= self.max_per_hour:
            return OutboundDecision(False, text=safe, reason="rate_limited_hour", adjusted=adjusted,
                                    detail={"per_hour": last_hour, "limit": self.max_per_hour})

        prev_text, prev_at = self._last_text.get(chat_id, ("", 0.0))
        if (self.duplicate_window_seconds and prev_text and safe == prev_text
                and (now - prev_at) < self.duplicate_window_seconds):
            return OutboundDecision(False, text=safe, reason="duplicate_text", adjusted=adjusted,
                                    detail={"window_seconds": self.duplicate_window_seconds})

        wait_until = max(self._last_sent_at.get(chat_id, 0.0) + self.min_interval_per_chat,
                         self._pending_until.get(chat_id, 0.0))
        chat_wait = max(0.0, wait_until - now)
        if chat_wait > self.max_wait_seconds:
            return OutboundDecision(False, text=safe, reason="chat_wait_too_long", adjusted=adjusted,
                                    detail={"need_wait": round(chat_wait, 2),
                                            "max_wait": self.max_wait_seconds})

        delay = chat_wait
        if simulate_typing:
            delay += self.typing_delay(safe)
        return OutboundDecision(True, text=safe, reason="ok", delay_seconds=round(delay, 2),
                                adjusted=adjusted,
                                detail={"chat_wait": round(chat_wait, 2),
                                        "typing_delay": round(delay - chat_wait, 2)})

    # ------------------------------------------------------------------ #
    async def acquire(self, chat_id: str, text: str, *, sleeper=None,
                      simulate_typing: bool = True) -> OutboundDecision:
        """判定 → 真的等待 → 记账。只有 allowed=True 才允许发送。"""
        import asyncio

        sleep = sleeper or asyncio.sleep
        decision = self.plan(chat_id, text, simulate_typing=simulate_typing)
        if decision.adjusted:
            self.stats["adjusted"] += 1
        if not decision.allowed:
            self.stats["blocked"] += 1
            self.stats.setdefault("blocked_reasons", {})
            self.stats["blocked_reasons"][decision.reason] = \
                self.stats["blocked_reasons"].get(decision.reason, 0) + 1
            logger.info("出站被拦（%s）: chat=%s %s", decision.reason, chat_id, decision.detail)
            return decision

        if decision.delay_seconds > 0:
            self.stats["delays"] += 1
            self.stats["total_delay_seconds"] = round(
                self.stats["total_delay_seconds"] + decision.delay_seconds, 2)
            await sleep(decision.delay_seconds)

        now = self._clock()
        self._last_sent_at[chat_id] = now
        self._last_text[chat_id] = (decision.text, now)
        self._pending_until.pop(chat_id, None)
        self._sent_times.append(now)
        self._sent_log.append(now)          # 每日上限按它统计（_sent_times 只留最近一小时）
        self.stats["allowed"] += 1
        return decision

    def note_pending(self, chat_id: str, seconds: float) -> None:
        """登记「该会话在 N 秒内不要再发」（例如对手动回复后的冷却）。"""
        self._pending_until[chat_id] = max(self._pending_until.get(chat_id, 0.0),
                                           self._clock() + max(0.0, seconds))

    def note_failed(self, chat_id: str) -> None:
        """发送实际失败时回滚这次「已发送」记账。

        帧发出前 `acquire()` 就已记账（限速/去重都按它算）。若不回滚：
        失败后重发同一条回复会被 300 秒重复文本窗口拦掉 ——
        「发失败 → 重发被自己挡住 → 买家永远收不到」。
        """
        if self._sent_log:
            self._sent_log.pop()
        if self._sent_times:
            self._sent_times.pop()
        self._last_sent_at.pop(chat_id, None)
        self._last_text.pop(chat_id, None)
        self.stats["allowed"] = max(0, self.stats.get("allowed", 0) - 1)
        self.stats["rolled_back"] = self.stats.get("rolled_back", 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        now = self._clock()
        last_minute, last_hour = self._rate_counts(now)
        return {
            **self.stats,
            "sent_last_minute": last_minute,
            "sent_last_hour": last_hour,
            "limits": {"per_minute": self.max_per_minute, "per_hour": self.max_per_hour,
                       "min_interval_per_chat": self.min_interval_per_chat,
                       "typing_simulation": self.typing_simulation,
                       "quiet_hours": self.quiet_hours},
        }


def build_policy(settings: Any) -> OutboundPolicy:
    """按通道配置构造出站策略（字段缺失时用安全默认值）。"""
    return OutboundPolicy(
        min_interval_per_chat=float(getattr(settings, "min_interval_per_chat", 3.0)),
        max_wait_seconds=float(getattr(settings, "max_wait_seconds", 30.0)),
        max_per_minute=int(getattr(settings, "max_per_minute", 20)),
        max_per_hour=int(getattr(settings, "max_per_hour", 300)),
        duplicate_window_seconds=float(getattr(settings, "duplicate_window_seconds", 300.0)),
        typing_simulation=bool(getattr(settings, "typing_simulation", True)),
        typing_max_delay=float(getattr(settings, "typing_max_delay", 10.0)),
        quiet_hours=getattr(settings, "quiet_hours", "") or None,
        allowlist=_split_list(getattr(settings, "send_allowlist", "")),
        max_per_day=int(getattr(settings, "max_per_day", 0) or 0),
    )


def _split_list(spec: Any) -> list:
    """把 `a,b,c` 形式的配置拆成列表（同时容忍 list 传入）。"""
    if not spec:
        return []
    if isinstance(spec, (list, tuple, set)):
        return [str(x).strip() for x in spec if str(x).strip()]
    return [part.strip() for part in str(spec).replace(";", ",").split(",") if part.strip()]
