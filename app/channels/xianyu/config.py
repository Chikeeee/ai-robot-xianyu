# -*- coding: utf-8 -*-
r"""闲鱼通道配置：全部从环境变量 / `.env` 读取（与 app/config.py 同一套约定）。

默认**关闭**（`XIANYU_ENABLED=false`）：AI-Robot 主服务不会去连闲鱼，
只有显式打开才会在 lifespan 里拉起值守引擎——避免「装着部署、顺手连上平台」这种意外。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent


def _env(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return (value if value is not None else default).strip()


def _flag(key: str, default: bool) -> bool:
    raw = _env(key)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


class XianyuSettings:
    """闲鱼通道设置（模块导入时读一次，和 app.config.Settings 一致）。"""

    enabled: bool = _flag("XIANYU_ENABLED", False)
    shadow_mode: bool = _flag("XIANYU_SHADOW_MODE", True)

    credentials_path: Path = Path(_env("XIANYU_CREDENTIALS", str(BASE_DIR / "secrets" / "xianyu_credentials.json")))
    db_path: Path = Path(_env("XIANYU_DB", str(BASE_DIR / "data" / "xianyu.db")))
    device_store: Path = Path(_env("XIANYU_DEVICE_STORE", str(BASE_DIR / "data" / "xianyu_device.json")))

    ws_url: str = _env("XIANYU_WS_URL", "wss://wss-goofish.dingtalk.com/")
    toggle_keywords: str = _env("XIANYU_TOGGLE_KEYWORDS", "。")
    manual_timeout: int = int(_env("XIANYU_MANUAL_TIMEOUT", "3600"))
    # 回复引擎：specialists（移植的闲鱼专家层，默认）| airobot（AI-Robot 原生编排）
    reply_engine: str = _env("XIANYU_REPLY_ENGINE", "specialists")

    heartbeat_interval: int = int(_env("XIANYU_HEARTBEAT_INTERVAL", "15"))
    heartbeat_timeout: int = int(_env("XIANYU_HEARTBEAT_TIMEOUT", "5"))
    token_refresh_interval: int = int(_env("XIANYU_TOKEN_REFRESH_INTERVAL", "3600"))
    reconnect_initial: float = float(_env("XIANYU_RECONNECT_INITIAL", "1"))
    reconnect_max: float = float(_env("XIANYU_RECONNECT_MAX", "60"))
    max_history: int = int(_env("XIANYU_MAX_HISTORY", "100"))
    message_expire_ms: int = int(_env("XIANYU_MESSAGE_EXPIRE_MS", "300000"))

    # 出站策略（限速 / 拟人化 / 静默时段）——见 outbound.py
    min_interval_per_chat: float = float(_env("XIANYU_MIN_INTERVAL_PER_CHAT", "3"))
    max_wait_seconds: float = float(_env("XIANYU_MAX_WAIT_SECONDS", "30"))
    max_per_minute: int = int(_env("XIANYU_MAX_PER_MINUTE", "20"))
    max_per_hour: int = int(_env("XIANYU_MAX_PER_HOUR", "300"))
    duplicate_window_seconds: float = float(_env("XIANYU_DUPLICATE_WINDOW_SECONDS", "300"))
    typing_simulation: bool = _flag("XIANYU_TYPING_SIMULATION", True)
    typing_max_delay: float = float(_env("XIANYU_TYPING_MAX_DELAY", "10"))
    quiet_hours: str = _env("XIANYU_QUIET_HOURS", "")
    # 灰度白名单：非空时**只**给这些会话发送（P4 第一次真发送用它把影响面锁死在一个会话里）
    send_allowlist: str = _env("XIANYU_SEND_ALLOWLIST", "")
    # 每日发送上限（0=不限）：给自动发送一个硬天花板
    max_per_day: int = int(_env("XIANYU_MAX_PER_DAY", "0"))

    # 原始帧诊断（排查「买家消息没动静」用；留空=关闭）
    frame_dump: str = _env("XIANYU_FRAME_DUMP", "")
    # 同步游标：now=上游做法（不拉历史）| zero=拉全量（仅排查，会处理历史消息）
    sync_pts: str = _env("XIANYU_SYNC_PTS", "now")
    # DNS 兜底：本机 ISP DNS 对 IM 域名只回 AAAA 时，用它强制解析到 A 记录 IP（进程内，仅改地址不改 SNI）
    dns_override: str = _env("XIANYU_DNS_OVERRIDE", "")

    # 告警：webhook 留空则只在事件流水与控制台里体现（不外发）
    alert_webhook: str = _env("XIANYU_ALERT_WEBHOOK", "")
    alert_style: str = _env("XIANYU_ALERT_STYLE", "generic")   # generic | feishu | wecom
    alert_cooldown: int = int(_env("XIANYU_ALERT_COOLDOWN", "600"))
    alert_patrol_interval: int = int(_env("XIANYU_ALERT_PATROL_INTERVAL", "30"))

    def summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "shadow_mode": self.shadow_mode,
            "ws_url": self.ws_url,
            "credentials": str(self.credentials_path),
            "credentials_exists": self.credentials_path.exists(),
            "db_path": str(self.db_path),
            "reply_engine": self.reply_engine,
            "toggle_keywords_len": len(self.toggle_keywords),
            "manual_timeout": self.manual_timeout,
            "heartbeat_interval": self.heartbeat_interval,
            "token_refresh_interval": self.token_refresh_interval,
            "alert_webhook": bool(self.alert_webhook),
            "alert_style": self.alert_style,
            "outbound": {"min_interval_per_chat": self.min_interval_per_chat,
                         "max_per_minute": self.max_per_minute,
                         "max_per_hour": self.max_per_hour,
                         "max_per_day": self.max_per_day,
                         "allowlist": self.send_allowlist or None,
                         "typing_simulation": self.typing_simulation,
                         "quiet_hours": self.quiet_hours or None},
        }


settings = XianyuSettings()
